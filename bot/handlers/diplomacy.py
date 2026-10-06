import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.db import Database
from bot.game import apply_treaty, npc_answer, npc_context
from bot.geo import load_world
from bot.handlers.actions import MAX_TEXT, parse_parts
from bot.handlers.common import find_country, is_private, player_context, send_dm

router = Router()

VOTE_RU = {"yes": "✅ За", "no": "❌ Против", "abstain": "⚪️ Воздержаться"}
KIND_RU = {"general": "📜 Договор", "peace": "🕊 Мирный договор", "transfer": "🗺 Территориальный договор"}


def _cells(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text)][:30]


def treaty_text(t: dict, a: dict, b: dict) -> str:
    world = load_world()
    lines = [f"{KIND_RU.get(t['kind'], '📜 Договор')} №{t['id']}",
             f"{a['flag']} <b>{escape(a['name'])}</b> ⟷ {b['flag']} <b>{escape(b['name'])}</b>", ""]
    if t["kind"] == "peace":
        mode = "по текущей линии фронта (оккупированное остаётся за занявшим)" if t["peace_mode"] == "front" \
            else "с возвратом к довоенным границам"
        lines.append(f"Условия: мир {mode}.")
    for cell, from_id, to_id in t["transfers"]:
        giver, taker = (a, b) if from_id == a["id"] else (b, a)
        lines.append(f"• провинция {cell} «{escape(world.cells[cell].label)}»: {escape(giver['name'])} → {escape(taker['name'])}")
    if t["text"]:
        lines.append(f"«{escape(t['text'])}»")
    lines += ["", f"✍️ {escape(a['name'])}: {'подписано ✅' if t['signed_a'] else 'ожидается ⌛'}",
              f"✍️ {escape(b['name'])}: {'подписано ✅' if t['signed_b'] else 'ожидается ⌛'}"]
    return "\n".join(lines)


def treaty_kb(treaty_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Подписать", callback_data=f"tr:{treaty_id}:sign")
    kb.button(text="❌ Отклонить", callback_data=f"tr:{treaty_id}:reject")
    return kb.as_markup()


async def _create_treaty(message: Message, bot: Bot, db: Database, gm: GameMaster, game: dict, me: dict, other: dict,
                         kind: str, text: str, peace_mode: str | None = None, transfers: list | None = None):
    chat_id = game["chat_id"]
    tid = await db.add_treaty(chat_id, game["turn"], kind, me["id"], other["id"], text, peace_mode, transfers)
    treaty = await db.get_treaty(tid)

    if other["user_id"]:
        await bot.send_message(chat_id, treaty_text(treaty, me, other) +
                               f"\n\nПодписывает лидер: {escape(other['player_name'] or '')}", reply_markup=treaty_kb(tid))
        if is_private(message):
            await message.answer("📨 Договор отправлен в группу на подпись.")
        return

    wait = await message.answer(f"📨 Послы отправились в {other['flag']} {escape(other['name'])}…")
    description = _describe(treaty, me, other)
    try:
        context = await npc_context(db, chat_id, other, me)
        answer = await gm.npc_diplomacy(me, other, description, context)
    except AIError as e:
        await wait.edit_text(f"⚠️ {escape(str(e))}")
        await db.set_treaty_status(tid, "cancelled")
        return
    await wait.delete()
    await db.set_notes(other["id"], answer["notes"])
    trust = max(-15, min(15, int(answer["trust_delta"])))
    if trust:
        await db.change_relation(chat_id, other["id"], me["id"], trust)
    await db.remember(chat_id, game["turn"], other["id"], me["id"], "treaty", me["name"], f"Предложили договор: {description}")
    await db.remember(chat_id, game["turn"], other["id"], me["id"], "treaty", other["name"],
                      ("Подписали. " if answer["accepted"] else "Отказали. ") + answer["reply"])
    if answer["accepted"]:
        await db.sign_treaty(tid, "b")
        await db.set_treaty_status(tid, "signed")
        effects = await apply_treaty(db, await db.get_treaty(tid), gm)
        verdict = "🤝 <b>Подписано обеими сторонами!</b>\n" + "\n".join(escape(e) for e in effects)
    else:
        await db.set_treaty_status(tid, "rejected")
        verdict = "✋ <b>Отказ.</b>"
    treaty = await db.get_treaty(tid)
    await bot.send_message(chat_id, f"{treaty_text(treaty, me, other)}\n\n<i>{other['flag']} {escape(other['name'])} "
                                    f"(ИИ):</i> {escape(answer['reply'])}\n\n{verdict}")
    if is_private(message):
        await message.answer("Ответ опубликован в группе.")


def _describe(t: dict, a: dict, b: dict) -> str:
    world = load_world()
    parts = [{"general": "Договор", "peace": "Мирный договор", "transfer": "Передача территорий"}[t["kind"]]]
    if t["kind"] == "peace":
        parts.append("по линии фронта" if t["peace_mode"] == "front" else "с возвратом к довоенным границам")
    for cell, from_id, to_id in t["transfers"]:
        parts.append(f"провинция «{world.cells[cell].label}» переходит от {a['name'] if from_id == a['id'] else b['name']} "
                     f"к {a['name'] if to_id == a['id'] else b['name']}")
    if t["text"]:
        parts.append(f"условия: {t['text']}")
    return "; ".join(parts)


async def _parties(message: Message, db: Database, target_name: str) -> tuple[dict, dict, dict] | None:
    ctx = await player_context(message, db)
    if not ctx:
        return None
    game, me = ctx
    if not target_name:
        return None
    other = await find_country(db, game["chat_id"], target_name)
    if not other:
        await message.answer(f"Не нашёл страну «{escape(target_name)}».")
        return None
    if other["id"] == me["id"]:
        await message.answer("Договор с самим собой — сильный ход, но нет.")
        return None
    return game, me, other


@router.message(Command("propose"))
async def cmd_propose(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    target, text = parse_parts(command.args)
    if not target or not text:
        await message.answer("Формат: /propose <i>страна</i> | <i>условия</i>\n"
                             "Например: <code>/propose Германия | торговый союз и общий рынок</code>")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return
    if parties := await _parties(message, db, target):
        await _create_treaty(message, bot, db, gm, *parties, "general", text)


@router.message(Command("peace"))
async def cmd_peace(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    target, mode_raw, text = parse_parts(command.args, 3)
    if not target:
        await message.answer("Формат: /peace <i>страна</i> | <i>фронт</i> или <i>довоенные</i> | <i>доп. условия</i>\n"
                             "«фронт» — каждый оставляет то, что занял; «довоенные» — все оккупированные провинции возвращаются.")
        return
    parties = await _parties(message, db, target)
    if not parties:
        return
    game, me, other = parties
    if (await db.get_relation(game["chat_id"], me["id"], other["id"]))["status"] != "war":
        await message.answer("Вы не воюете с этой страной.")
        return
    mode = "status_quo" if mode_raw.casefold().startswith(("довоен", "статус", "возв")) else "front"
    await _create_treaty(message, bot, db, gm, game, me, other, "peace", text[:MAX_TEXT], peace_mode=mode)


async def _transfer(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster, give: bool):
    target, cells_raw, text = parse_parts(command.args, 3)
    cmd = "transfer" if give else "demand"
    if not target or not cells_raw:
        await message.answer(f"Формат: /{cmd} <i>страна</i> | <i>номера провинций через запятую</i> | <i>комментарий</i>\n"
                             "Номера провинций видно на карте: /map <i>страна</i>")
        return
    parties = await _parties(message, db, target)
    if not parties:
        return
    game, me, other = parties
    owner, _ = await db.cell_owners(game["chat_id"])
    giver, taker = (me, other) if give else (other, me)
    cells = _cells(cells_raw)
    wrong = [c for c in cells if owner.get(c) != giver["id"]]
    if not cells or wrong:
        await message.answer(f"Провинции {', '.join(map(str, wrong)) or cells_raw} не принадлежат стране {escape(giver['name'])}. "
                             f"Проверьте номера на карте: /map {escape(giver['name'])}")
        return
    transfers = [[c, giver["id"], taker["id"]] for c in dict.fromkeys(cells)]
    await _create_treaty(message, bot, db, gm, game, me, other, "transfer", text[:MAX_TEXT], transfers=transfers)


@router.message(Command("transfer"))
async def cmd_transfer(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    await _transfer(message, command, bot, db, gm, give=True)


@router.message(Command("demand"))
async def cmd_demand(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    await _transfer(message, command, bot, db, gm, give=False)


@router.callback_query(F.data.startswith("tr:"))
async def cb_treaty(call: CallbackQuery, db: Database, gm: GameMaster):
    _, tid, action = call.data.split(":")
    treaty = await db.get_treaty(int(tid))
    if not treaty or treaty["status"] != "pending":
        await call.answer("Договор уже закрыт", show_alert=True)
        return
    a, b = await db.get_country(treaty["a_id"]), await db.get_country(treaty["b_id"])
    if not a or not b or not a["alive"] or not b["alive"]:
        await db.set_treaty_status(treaty["id"], "cancelled")
        await call.answer("Одна из сторон больше не существует", show_alert=True)
        return
    side = "a" if call.from_user.id == a["user_id"] else "b" if call.from_user.id == b["user_id"] else None
    if side is None:
        await call.answer("Подписывать могут только лидеры стран-участниц", show_alert=True)
        return
    if action == "reject":
        await db.set_treaty_status(treaty["id"], "rejected")
        await call.message.edit_text(treaty_text(treaty, a, b) + f"\n\n✋ <b>Отклонено</b> стороной "
                                     f"{escape((a if side == 'a' else b)['name'])}.")
        await call.answer()
        return
    await db.sign_treaty(treaty["id"], side)
    treaty = await db.get_treaty(treaty["id"])
    if treaty["signed_a"] and treaty["signed_b"]:
        await db.set_treaty_status(treaty["id"], "signed")
        effects = await apply_treaty(db, treaty, gm)
        await call.message.edit_text(treaty_text(treaty, a, b) + "\n\n🤝 <b>Подписано обеими сторонами!</b>\n"
                                     + "\n".join(escape(e) for e in effects))
    else:
        await call.message.edit_text(treaty_text(treaty, a, b), reply_markup=treaty_kb(treaty["id"]))
    await call.answer("Подпись поставлена")


@router.message(Command("say"))
async def cmd_say(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    target, text = parse_parts(command.args)
    if not target or not text:
        await message.answer("Формат: /say <i>страна</i> | <i>сообщение</i> — публичное обращение к другой стране.")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return
    parties = await _parties(message, db, target)
    if not parties:
        return
    game, me, other = parties
    await bot.send_message(game["chat_id"], f"✉️ {me['flag']} <b>{escape(me['name'])}</b> → {other['flag']} "
                                            f"<b>{escape(other['name'])}</b>:\n{escape(text)}")
    if is_private(message):
        await message.answer("Сообщение опубликовано в группе.")
    if other["user_id"] or not game["npc_chat"]:
        return
    try:
        reply, support = await npc_answer(db, gm, game["chat_id"], other, me, text, private=False)
    except AIError:
        return
    msg = (f"↩️ {other['flag']} <b>{escape(other['name'])}</b> → {me['flag']} <b>{escape(me['name'])}</b>:\n"
           f"{escape(reply)}")
    if support:
        msg += f"\n\n📦 <b>Поддержка:</b> {escape(support)}"
    await bot.send_message(game["chat_id"], msg)


@router.message(Command("talk"))
async def cmd_talk(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    if not is_private(message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        me = await bot.me()
        await message.answer(f"🤫 Тайные переговоры ведутся в личке: @{me.username} — /talk <i>страна</i> | <i>текст</i>")
        return
    target, text = parse_parts(command.args)
    if not target or not text:
        await message.answer("Формат: /talk <i>страна</i> | <i>сообщение</i> — тайные переговоры. Другие игроки их не видят. "
                             "Страна-ИИ запомнит разговор, обещания и может оказать поддержку.")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return
    parties = await _parties(message, db, target)
    if not parties:
        return
    game, me, other = parties
    if other["user_id"]:
        delivered = await send_dm(bot, other["user_id"], f"🔒 <b>Тайное послание от {me['flag']} {escape(me['name'])}</b>\n\n"
                                                         f"{escape(text)}\n\nОтветить: <code>/talk {escape(me['name'])} | …</code>")
        await message.answer("🔒 Послание доставлено." if delivered else
                             "📭 Лидер этой страны ещё не открыл личку с ботом — послание не доставлено.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        reply, support = await npc_answer(db, gm, game["chat_id"], other, me, text, private=True)
    except AIError as e:
        await message.answer(f"⚠️ {escape(str(e))}")
        return
    msg = f"🔒 {other['flag']} <b>{escape(other['name'])}</b> (тайно):\n{escape(reply)}"
    if support:
        msg += f"\n\n📦 <b>Тайная поддержка:</b> {escape(support)}"
    await message.answer(msg)


def _resolution_text(author: dict, text: str, tally: dict) -> str:
    return (f"🇺🇳 <b>Резолюция Генассамблеи ООН</b>\nВносит: {author['flag']} {escape(author['name'])}\n\n"
            f"«{escape(text)}»\n\nЗа: {tally['yes']} · Против: {tally['no']} · Воздержались: {tally['abstain']}\n"
            "<i>Голосуют лидеры стран-игроков, голос стран-ИИ учтёт ведущий. Закроется в конце хода.</i>")


def _vote_kb(rid: int):
    kb = InlineKeyboardBuilder()
    for key, title in VOTE_RU.items():
        kb.button(text=title, callback_data=f"un:{rid}:{key}")
    return kb.as_markup()


@router.message(Command("un"))
async def cmd_un(message: Message, command: CommandObject, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /un <i>текст резолюции</i>\nНапример: <code>/un Глобальный карантин против вируса</code>")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return
    rid = await db.add_resolution(game["chat_id"], game["turn"], me["id"], text)
    await db.cast_vote(rid, me["id"], "yes")
    await bot.send_message(game["chat_id"], _resolution_text(me, text, await db.tally(rid)), reply_markup=_vote_kb(rid))
    if is_private(message):
        await message.answer("🇺🇳 Резолюция вынесена на голосование в группе.")


@router.callback_query(F.data.startswith("un:"))
async def cb_vote(call: CallbackQuery, db: Database):
    _, rid, vote = call.data.split(":")
    resolution = await db.get_resolution(int(rid))
    if not resolution or resolution["status"] != "open":
        await call.answer("Голосование закрыто", show_alert=True)
        return
    voter = await db.get_country_by_user(resolution["chat_id"], call.from_user.id)
    if not voter:
        await call.answer("Голосовать могут только лидеры стран", show_alert=True)
        return
    await db.cast_vote(resolution["id"], voter["id"], vote)
    author = await db.get_country(resolution["author_id"]) or {"flag": "🏳️", "name": "—"}
    tally = await db.tally(resolution["id"])
    await call.message.edit_text(_resolution_text(author, resolution["text"], tally), reply_markup=_vote_kb(resolution["id"]))
    await call.answer(f"{voter['name']}: {VOTE_RU[vote]}")
