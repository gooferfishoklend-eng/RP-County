from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.ai import GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import ACTION_KINDS
from bot.handlers.common import finish_turn, is_private, player_context, send_dm

router = Router()

MAX_TEXT = 1000

PUBLIC_TEMPLATES = {
    "reform": "🏛 {flag} <b>{name}</b> объявляет реформу:\n{text}",
    "foreign": "🌐 {flag} <b>{name}</b> — внешнеполитическое заявление:\n{text}",
    "sanction": "🚫 {flag} <b>{name}</b> вводит санкции против <b>{target}</b>:\n{text}",
    "war": "⚔️ {flag} <b>{name}</b> ОБЪЯВЛЯЕТ ВОЙНУ стране <b>{target}</b>!\nЦель: {text}",
}


def parse_target(args: str | None) -> tuple[str, str]:
    target, _, text = (args or "").partition("|")
    return target.strip(), text.strip()


async def add_action(
    message: Message, bot: Bot, db: Database, settings: Settings, kind: str, text: str, target: str | None = None
) -> dict | None:
    ctx = await player_context(message, db)
    if not ctx:
        return None
    game, country = ctx
    if not text:
        await message.answer("Опишите действие текстом после команды.")
        return None
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return None
    done = await db.list_actions(game["chat_id"], game["turn"], country["id"])
    if len(done) >= settings.max_actions_per_turn:
        await message.answer(f"Лимит приказов на ход — {settings.max_actions_per_turn}. Отмените лишнее через /undo или жмите /ready.")
        return None

    await db.add_action(game["chat_id"], game["turn"], country["id"], kind, text, target)

    if kind == "secret":
        await message.answer(
            f"🕶 Тайный приказ принят ({len(done) + 1}/{settings.max_actions_per_turn}). "
            "Об исполнении доложат в конце хода. Помните: чем масштабнее операция, тем выше риск утечки."
        )
        return country

    announcement = PUBLIC_TEMPLATES[kind].format(
        flag=country["flag"], name=escape(country["name"]), target=escape(target or ""), text=escape(text)
    )
    if is_private(message):
        await bot.send_message(game["chat_id"], announcement)
        await message.answer(f"✅ Приказ принят и объявлен в группе ({len(done) + 1}/{settings.max_actions_per_turn}).")
    else:
        await message.answer(announcement + f"\n\n<i>Приказ {len(done) + 1}/{settings.max_actions_per_turn}</i>")
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
        await message.answer(
            f"🤫 Тайные приказы отдаются только в личке: @{me.username}. "
            "Если бот админ группы, ваше сообщение уже удалено; если нет — удалите его сами."
        )
        return
    await add_action(message, bot, db, settings, "secret", (command.args or "").strip())


async def _hostile(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings, kind: str):
    target, text = parse_target(command.args)
    if not target:
        await message.answer(f"Формат: /{kind} <i>страна</i> | <i>{'цель' if kind == 'war' else 'причина'}</i>")
        return
    country = await add_action(message, bot, db, settings, kind, text or "без объяснения причин", target)
    if not country:
        return
    other = await db.get_country_by_name(country["chat_id"], target)
    if other and other["id"] != country["id"]:
        if kind == "war":
            await db.change_relation(country["chat_id"], country["id"], other["id"], -40, "war")
        else:
            await db.change_relation(country["chat_id"], country["id"], other["id"], -15, "tension")


@router.message(Command("war"))
async def cmd_war(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    await _hostile(message, command, bot, db, settings, "war")


@router.message(Command("sanction"))
async def cmd_sanction(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    await _hostile(message, command, bot, db, settings, "sanction")


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
    countries = await db.list_countries(chat_id)
    ready = sum(1 for c in countries if c["ready_turn"] == game["turn"])
    text = f"✅ {country['flag']} {escape(country['name'])} завершает ход ({ready}/{len(countries)})."
    await bot.send_message(chat_id, text)
    if is_private(message):
        await message.answer("Ход завершён. Ждём остальных лидеров.")
    if ready >= len(countries):
        await finish_turn(bot, db, gm, settings, chat_id)
