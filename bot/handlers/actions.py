from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.ai import GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import ACTION_KINDS, declare_war
from bot.support import KINDS as SUPPORT_KINDS, give_support, needs_amount, parse_kind
from bot.handlers.common import find_country, finish_turn, game_chat_id, is_private, player_context, send_dm

router = Router()

MAX_TEXT = 1000

PUBLIC_TEMPLATES = {
    "reform": "🏛 {flag} <b>{name}</b> объявляет реформу:\n{text}",
    "foreign": "🌐 {flag} <b>{name}</b> — внешнеполитическое заявление:\n{text}",
    "sanction": "🚫 {flag} <b>{name}</b> вводит санкции против <b>{target}</b>:\n{text}",
    "aid": "🤲 {flag} <b>{name}</b> направляет помощь стране <b>{target}</b>:\n{text}",
    "war": "⚔️ {flag} <b>{name}</b> — приказ армии на войне с <b>{target}</b>:\n{text}",
}


def parse_parts(args: str | None, n: int = 2) -> list[str]:
    parts = [p.strip() for p in (args or "").split("|", n - 1)]
    return parts + [""] * (n - len(parts))


async def add_action(message: Message, bot: Bot, db: Database, settings: Settings, kind: str, text: str,
                     target: str | None = None, ctx: tuple[dict, dict] | None = None) -> dict | None:
    ctx = ctx or await player_context(message, db)
    if not ctx:
        return None
    game, country = ctx
    if not text:
        await message.answer("Опишите действие текстом после команды.")
        return None
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return None
    done = [a for a in await db.list_actions(game["chat_id"], game["turn"], country["id"]) if a["kind"] in PUBLIC_TEMPLATES or a["kind"] == "secret"]
    if len(done) >= settings.max_actions_per_turn:
        await message.answer(f"Лимит приказов на ход — {settings.max_actions_per_turn}. Отмените лишнее через /undo или жмите /ready.")
        return None

    await db.add_action(game["chat_id"], game["turn"], country["id"], kind, text, target)
    counter = f"{len(done) + 1}/{settings.max_actions_per_turn}"

    if kind == "secret":
        await message.answer(f"🕶 Тайный приказ принят ({counter}). Об исполнении доложат в конце хода. "
                             "Помните: чем масштабнее операция, тем выше риск утечки.")
        return country

    announcement = PUBLIC_TEMPLATES[kind].format(
        flag=country["flag"], name=escape(country["name"]), target=escape(target or ""), text=escape(text)
    )
    if is_private(message):
        await bot.send_message(game["chat_id"], announcement)
        await message.answer(f"✅ Приказ принят и объявлен в группе ({counter}).")
    else:
        await message.answer(announcement + f"\n\n<i>Приказ {counter}</i>")
    return country


@router.message(Command("reform"))
async def cmd_reform(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    await add_action(message, bot, db, settings, "reform", (command.args or "").strip())


@router.message(Command("foreign"))
async def cmd_foreign(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    await add_action(message, bot, db, settings, "foreign", (command.args or "").strip())


@router.message(Command("secret"))
async def cmd_secret(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    if not is_private(message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        me = await bot.me()
        await message.answer(f"🤫 Тайные приказы отдаются только в личке: @{me.username}. "
                             "Если бот админ группы, ваше сообщение уже удалено; если нет — удалите его сами.")
        return
    await add_action(message, bot, db, settings, "secret", (command.args or "").strip())


async def _targeted(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings, kind: str,
                    hint: str) -> tuple[dict, dict, dict] | None:
    ctx = await player_context(message, db)
    if not ctx:
        return None
    game, me = ctx
    target_name, text = parse_parts(command.args)
    if not target_name:
        await message.answer(f"Формат: /{kind} <i>страна</i> | <i>{hint}</i>")
        return None
    target = await find_country(db, game["chat_id"], target_name)
    if not target:
        await message.answer(f"Не нашёл страну «{escape(target_name)}».")
        return None
    if target["id"] == me["id"]:
        await message.answer("Это ваша собственная страна.")
        return None
    country = await add_action(message, bot, db, settings, kind, text or hint, target["name"], ctx)
    return (game, country, target) if country else None


@router.message(Command("war"))
async def cmd_war(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    chat_id = await game_chat_id(message, db)
    target_name = parse_parts(command.args)[0]
    if chat_id and target_name:
        me = await db.get_country_by_user(chat_id, message.from_user.id)
        target = await find_country(db, chat_id, target_name)
        if me and target:
            ours, theirs = await db.alliance_of(chat_id, me["id"]), await db.alliance_of(chat_id, target["id"])
            if ours and theirs and ours["id"] == theirs["id"]:
                await message.answer(f"⛔️ {escape(target['name'])} — ваш союзник по «{escape(ours['name'])}». "
                                     "Чтобы воевать, сначала выйдите из союза или исключите её.")
                return
    res = await _targeted(message, command, bot, db, settings, "war", "цель войны / приказ армии")
    if not res:
        return
    game, me, target = res
    rel = await db.get_relation(game["chat_id"], me["id"], target["id"])
    if rel["status"] == "war":
        return
    created = await declare_war(db, gm, game["chat_id"], me, target)
    lines = [f"💥 <b>{me['flag']} {escape(me['name'])} ОБЪЯВЛЯЕТ ВОЙНУ {target['flag']} {escape(target['name'])}!</b>",
             "Армии выдвигаются к границе. Бои начнутся в конце хода."]
    for g in created:
        side = me if g["country_id"] == me["id"] else target
        lines.append(f"🎖 {side['flag']} Командование фронтом: {escape(g['rank'])} {escape(g['name'])} "
                     f"({escape(g['trait'])}, навык {g['skill']}/10)")
    bloc = await db.alliance_of(game["chat_id"], target["id"])
    if bloc:
        allies = [await db.get_country(cid) for cid in await db.alliance_members(bloc["id"]) if cid != target["id"]]
        if allies:
            lines.append(f"\n⚠️ {escape(target['name'])} — член союза «{escape(bloc['name'])}». Союзники: "
                         + ", ".join(f"{a['flag']} {escape(a['name'])}" for a in allies)
                         + ". Они могут вступить в войну (/war) или помочь (/support).")
    lines.append("\nКомандуйте генералами: /generals, /command · Карта фронта: /map " + escape(target["name"]))
    await bot.send_message(game["chat_id"], "\n".join(lines))


@router.message(Command("sanction"))
async def cmd_sanction(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    res = await _targeted(message, command, bot, db, settings, "sanction", "причина")
    if res:
        game, me, target = res
        if (await db.get_relation(game["chat_id"], me["id"], target["id"]))["status"] != "war":
            await db.change_relation(game["chat_id"], me["id"], target["id"], -15, "tension")


@router.message(Command("aid"))
async def cmd_aid(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    res = await _targeted(message, command, bot, db, settings, "aid", "какая помощь: врачи, вакцины, деньги, спасатели")
    if res:
        game, me, target = res
        await db.change_relation(game["chat_id"], me["id"], target["id"], 5)


@router.message(Command("support"))
async def cmd_support(message: Message, command: CommandObject, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    target_name, kind_raw, rest = parse_parts(command.args, 3)
    kind = parse_kind(kind_raw) if kind_raw else None
    if not target_name or not kind:
        await message.answer(
            "Формат: /support <i>страна</i> | <i>вид</i> | <i>сумма, млрд $</i> | <i>комментарий</i>\n"
            "Виды: <b>деньги</b>, <b>оружие</b>, <b>гуманитарка</b> (нужна сумма), <b>войска</b> (корпус на 2 хода), "
            "<b>разведка</b> (бонус к ударам на ход).\n"
            "Пример: <code>/support Украина | оружие | 5 | ПВО и снаряды</code>\n"
            "В личке боту — тайная поддержка, в группе — публичная."
        )
        return
    target = await find_country(db, game["chat_id"], target_name)
    if not target:
        await message.answer(f"Не нашёл страну «{escape(target_name)}».")
        return
    amount_raw, _, note = rest.partition("|")
    amount = 0.0
    if needs_amount(kind):
        try:
            amount = float(amount_raw.strip().replace(",", ".").split()[0])
        except (ValueError, IndexError):
            await message.answer("Укажите сумму в млрд $, например: <code>/support Украина | деньги | 3</code>")
            return
    else:
        note = rest
    secret = is_private(message)
    ok, effect = await give_support(db, game["chat_id"], game["turn"], me, target, kind, amount, note.strip(), secret)
    if not ok:
        await message.answer(f"⚠️ {escape(effect)}")
        return
    if not target["user_id"]:
        await db.remember(game["chat_id"], game["turn"], target["id"], me["id"], "support", me["name"],
                          f"Получили от них поддержку: {SUPPORT_KINDS[kind]} {amount:g} млрд $. {note.strip()}", secret)
    text = (f"📦 {me['flag']} <b>{escape(me['name'])}</b> → {target['flag']} <b>{escape(target['name'])}</b>: "
            f"{SUPPORT_KINDS[kind]}" + (f" на {amount:g} млрд $" if amount else "") + f"\nЭффект: {escape(effect)}"
            + (f"\n«{escape(note.strip())}»" if note.strip() else ""))
    if secret:
        await message.answer("🤫 Тайная поддержка оказана.\n" + text)
        if target["user_id"]:
            await send_dm(bot, target["user_id"], "🤫 Вам тайно оказали поддержку!\n" + text)
    else:
        await message.answer(text)


@router.message(Command("actions"))
async def cmd_actions(message: Message, db: Database, bot: Bot):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    actions = await db.list_actions(game["chat_id"], game["turn"], country["id"])
    has_secret = any(a["kind"] == "secret" for a in actions)
    lines = [f"📋 <b>Приказы {country['flag']} {escape(country['name'])} на этот ход</b>", ""]
    for i, a in enumerate(actions, 1):
        target = f" → {escape(a['target'])}" if a["target"] else ""
        lines.append(f"{i}. <b>{ACTION_KINDS.get(a['kind'], a['kind'])}</b>{target}: {escape(a['text'])}")
    if not actions:
        lines.append("Пока пусто.")
    text = "\n".join(lines)
    if has_secret and not is_private(message):
        if await send_dm(bot, message.from_user.id, text):
            await message.answer("📬 Список приказов (с тайными) отправлен вам в личку.")
        else:
            await message.answer("Среди приказов есть тайные — напишите мне в личку /start, чтобы их увидеть.")
        return
    await message.answer(text)


@router.message(Command("undo"))
async def cmd_undo(message: Message, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    removed = await db.delete_last_action(game["chat_id"], game["turn"], country["id"])
    if not removed:
        await message.answer("Отменять нечего.")
    elif removed["kind"] == "secret" and not is_private(message):
        await message.answer("↩️ Последний приказ отменён.")
    else:
        await message.answer(f"↩️ Отменён приказ: {escape(removed['text'][:200])}")


@router.message(Command("ready"))
async def cmd_ready(message: Message, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    chat_id = game["chat_id"]
    await db.set_ready(country["id"], game["turn"])
    players = await db.list_players(chat_id)
    ready = sum(1 for c in players if c["ready_turn"] == game["turn"])
    await bot.send_message(chat_id, f"✅ {country['flag']} {escape(country['name'])} завершает ход ({ready}/{len(players)}).")
    if is_private(message):
        await message.answer("Ход завершён. Ждём остальных лидеров.")
    if ready >= len(players):
        await finish_turn(bot, db, gm, settings, chat_id)
