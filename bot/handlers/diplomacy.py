from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.db import Database
from bot.game import world_snapshot
from bot.handlers.actions import MAX_TEXT, parse_target
from bot.handlers.common import is_private, player_context

router = Router()

VOTE_RU = {"yes": "✅ За", "no": "❌ Против", "abstain": "⚪️ Воздержаться"}


@router.message(Command("propose"))
async def cmd_propose(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    chat_id = game["chat_id"]
    target, text = parse_target(command.args)
    if not target or not text:
        await message.answer("Формат: /propose <i>страна</i> | <i>предложение</i>\nНапример: <code>/propose Германия | торговое соглашение и снижение пошлин</code>")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return

    other = await db.get_country_by_name(chat_id, target)
    if other and other["id"] == me["id"]:
        await message.answer("Договор с самим собой — сильный ход, но нет.")
        return

    if other:
        pid = await db.add_proposal(chat_id, game["turn"], me["id"], other["id"], text)
        kb = InlineKeyboardBuilder()
        kb.button(text="🤝 Принять", callback_data=f"prop:{pid}:yes")
        kb.button(text="✋ Отклонить", callback_data=f"prop:{pid}:no")
        await bot.send_message(
            chat_id,
            f"📜 {me['flag']} <b>{escape(me['name'])}</b> предлагает {other['flag']} <b>{escape(other['name'])}</b>:\n"
            f"«{escape(text)}»\n\nОтвечает лидер: {escape(other['player_name'] or '')}",
            reply_markup=kb.as_markup(),
        )
        if is_private(message):
            await message.answer("📨 Предложение отправлено в группу.")
        return

    wait = await message.answer(f"📨 Послы отправились в «{escape(target)}»…")
    try:
        world = await world_snapshot(db, chat_id)
        answer = await gm.npc_diplomacy(me, target, text, world)
    except AIError as e:
        await wait.edit_text(f"⚠️ {escape(str(e))}")
        return
    verdict = "🤝 <b>Согласие!</b>" if answer["accepted"] else "✋ <b>Отказ.</b>"
    result = (
        f"📜 {me['flag']} <b>{escape(me['name'])}</b> → <b>{escape(target)}</b> (страна под управлением ИИ)\n"
        f"Предложение: «{escape(text)}»\n\n{verdict}\n{escape(answer['reply'])}"
    )
    await wait.delete()
    if answer["accepted"]:
        await db.add_action(chat_id, game["turn"], me["id"], "npc_deal", text, target)
    await bot.send_message(chat_id, result)
    if is_private(message):
        await message.answer("Ответ опубликован в группе.")


@router.callback_query(F.data.startswith("prop:"))
async def cb_proposal(call: CallbackQuery, db: Database):
    _, pid, choice = call.data.split(":")
    proposal = await db.get_proposal(int(pid))
    if not proposal or proposal["status"] != "pending":
        await call.answer("Предложение уже рассмотрено", show_alert=True)
        return
    target = await db.get_country(proposal["to_id"])
    author = await db.get_country(proposal["from_id"])
    if not target or not author:
        await call.answer("Одна из стран больше не в игре", show_alert=True)
        return
    if call.from_user.id != target["user_id"]:
        await call.answer(f"Решение принимает лидер страны {target['name']}", show_alert=True)
        return
    game = await db.get_game(proposal["chat_id"])
    accepted = choice == "yes"
    await db.set_proposal_status(proposal["id"], "accepted" if accepted else "declined")
    if accepted:
        await db.change_relation(proposal["chat_id"], author["id"], target["id"], 15)
        for country_id, partner in ((author["id"], target["name"]), (target["id"], author["name"])):
            await db.add_action(proposal["chat_id"], game["turn"], country_id, "treaty", proposal["text"], partner)
        verdict = "🤝 <b>Договор заключён!</b>"
    else:
        await db.change_relation(proposal["chat_id"], author["id"], target["id"], -3)
        verdict = "✋ <b>Предложение отклонено.</b>"
    await call.message.edit_text(
        f"📜 {author['flag']} <b>{escape(author['name'])}</b> ↔ {target['flag']} <b>{escape(target['name'])}</b>\n"
        f"«{escape(proposal['text'])}»\n\n{verdict}"
    )
    await call.answer()


def _resolution_text(author: dict, text: str, tally: dict) -> str:
    return (
        f"🇺🇳 <b>Резолюция Генассамблеи ООН</b>\nВносит: {author['flag']} {escape(author['name'])}\n\n"
        f"«{escape(text)}»\n\n"
        f"За: {tally['yes']} · Против: {tally['no']} · Воздержались: {tally['abstain']}\n"
        "<i>Голосование закроется в конце хода.</i>"
    )


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
        await message.answer("Формат: /un <i>текст резолюции</i>\nНапример: <code>/un Ввести глобальный запрет на боевых роботов</code>")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно — максимум {MAX_TEXT} символов.")
        return
    rid = await db.add_resolution(game["chat_id"], game["turn"], me["id"], text)
    await db.cast_vote(rid, me["id"], "yes")
    tally = await db.tally(rid)
    await bot.send_message(game["chat_id"], _resolution_text(me, text, tally), reply_markup=_vote_kb(rid))
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
