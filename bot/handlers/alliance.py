from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.alliance import MAX_NAME, add_member, describe, find_alliance, join_blockers, npc_decides, remove_member
from bot.db import Database
from bot.handlers.common import find_country, game_chat_id, is_private, player_context

router = Router()

HELP = """🤝 <b>Союзы</b>
У союза есть название, устав, глава и участники. Страна может состоять только в одном союзе; союзники не могут воевать друг с другом.

<code>/alliance создать Название | устав</code> — основать союз (вы — глава)
<code>/alliance вступить Название</code> — подать заявку (решает глава союза)
<code>/alliance пригласить Страна</code> — глава приглашает страну
<code>/alliance исключить Страна</code> — глава исключает участника
<code>/alliance глава Страна</code> — передать главенство
<code>/alliance выйти</code> — покинуть союз
<code>/alliances</code> — все союзы мира"""


async def card(db: Database, alliance: dict) -> str:
    d = await describe(db, alliance)
    lines = [f"🤝 <b>{escape(alliance['name'])}</b>"]
    if alliance["charter"]:
        lines.append(f"<i>{escape(alliance['charter'])}</i>")
    for c in d["members"]:
        role = " — 👑 глава" if c["id"] == alliance["leader_id"] else ""
        who = escape(c["player_name"]) if c["user_id"] else "ИИ"
        lines.append(f"{c['flag']} {escape(c['name'])} ({who}){role}")
    return "\n".join(lines)


def decision_kb(request_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"al:{request_id}:yes")
    kb.button(text="❌ Отклонить", callback_data=f"al:{request_id}:no")
    return kb.as_markup()


@router.message(Command("alliances"))
async def cmd_alliances(message: Message, db: Database):
    chat_id = await game_chat_id(message, db)
    alliances = await db.list_alliances(chat_id) if chat_id else []
    if not alliances:
        await message.answer("Союзов пока нет. Основать: <code>/alliance создать Название | устав</code>")
        return
    await message.answer("\n\n".join([await card(db, a) for a in alliances]))


@router.message(Command("alliance"))
async def cmd_alliance(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    chat_id = game["chat_id"]
    verb, _, rest = (command.args or "").strip().partition(" ")
    verb, rest = verb.casefold(), rest.strip()
    mine = await db.alliance_of(chat_id, me["id"])

    if not verb:
        text = (await card(db, mine) + "\n\n" if mine else "Вы не состоите в союзе.\n\n") + HELP
        await message.answer(text)
        return

    async def announce(text: str, **kw):
        await bot.send_message(chat_id, text, **kw)
        if is_private(message):
            await message.answer("Готово, объявлено в группе.")

    if verb in ("создать", "create", "основать"):
        name, _, charter = rest.partition("|")
        name = name.strip()[:MAX_NAME]
        if not name:
            await message.answer("Формат: <code>/alliance создать Название | устав</code>")
            return
        if mine:
            await message.answer(f"Вы уже в союзе «{escape(mine['name'])}». Сначала /alliance выйти.")
            return
        if any(a["name"].casefold() == name.casefold() for a in await db.list_alliances(chat_id)):
            await message.answer("Союз с таким названием уже есть.")
            return
        aid = await db.create_alliance(chat_id, name, charter.strip()[:1000], me["id"], game["turn"])
        await announce(f"🤝 <b>Основан союз «{escape(name)}»</b>\n👑 Глава: {me['flag']} {escape(me['name'])}"
                       + (f"\nУстав: <i>{escape(charter.strip())}</i>" if charter.strip() else "")
                       + "\n\nВступить: <code>/alliance вступить " + escape(name) + "</code>")
        return

    if verb in ("выйти", "leave"):
        if not mine:
            await message.answer("Вы не состоите в союзе.")
            return
        status = await remove_member(db, chat_id, mine, me["id"])
        await announce(f"🚪 {me['flag']} {escape(me['name'])} выходит из союза «{escape(mine['name'])}». {escape(status)}")
        return

    if verb in ("вступить", "join"):
        alliance = await find_alliance(db, chat_id, rest, find_country) if rest else None
        if not alliance:
            await message.answer("Союз не найден. Список: /alliances")
            return
        if reason := await join_blockers(db, chat_id, alliance, me):
            await message.answer(escape(reason))
            return
        leader = await db.get_country(alliance["leader_id"])
        rid = await db.add_alliance_request(alliance["id"], me["id"], "join")
        if leader["user_id"]:
            await announce(f"📨 {me['flag']} <b>{escape(me['name'])}</b> просит принять её в союз «{escape(alliance['name'])}».\n"
                           f"Решает глава: {leader['flag']} {escape(leader['name'])}", reply_markup=decision_kb(rid))
            return
        await _npc_join_decision(message, bot, db, gm, chat_id, game["turn"], alliance, leader, me, rid, announce)
        return

    if not mine or mine["leader_id"] != me["id"]:
        await message.answer("Это может только глава союза. " + HELP)
        return
    target = await find_country(db, chat_id, rest) if rest else None
    if not target:
        await message.answer(f"Не нашёл страну «{escape(rest)}».")
        return

    if verb in ("пригласить", "invite"):
        if reason := await join_blockers(db, chat_id, mine, target):
            await message.answer(escape(reason))
            return
        rid = await db.add_alliance_request(mine["id"], target["id"], "invite")
        if target["user_id"]:
            await announce(f"📨 Союз «{escape(mine['name'])}» приглашает {target['flag']} <b>{escape(target['name'])}</b>.\n"
                           f"Решает: {escape(target['player_name'] or '')}", reply_markup=decision_kb(rid))
            return
        try:
            answer = await npc_decides(db, gm, chat_id, target, me,
                                       f"Приглашение вступить в союз «{mine['name']}» (глава — {me['name']}). Устав: {mine['charter']}")
        except AIError as e:
            await message.answer(f"⚠️ {escape(str(e))}")
            return
        await db.set_alliance_request(rid, "accepted" if answer["accepted"] else "rejected")
        if answer["accepted"]:
            await add_member(db, chat_id, mine, target, game["turn"])
        verdict = "✅ вступает в союз!" if answer["accepted"] else "❌ отказывается."
        await announce(f"{target['flag']} <b>{escape(target['name'])}</b> (ИИ) {verdict}\n<i>{escape(answer['reply'])}</i>")
        return

    if verb in ("исключить", "kick"):
        if target["id"] == me["id"] or target["id"] not in await db.alliance_members(mine["id"]):
            await message.answer("Такой страны нет среди участников (себя исключить нельзя — /alliance выйти).")
            return
        await remove_member(db, chat_id, mine, target["id"])
        await announce(f"⛔️ {target['flag']} {escape(target['name'])} исключена из союза «{escape(mine['name'])}».")
        return

    if verb in ("глава", "leader"):
        if target["id"] not in await db.alliance_members(mine["id"]):
            await message.answer("Главой можно сделать только участника союза.")
            return
        await db.update_alliance(mine["id"], leader_id=target["id"])
        await announce(f"👑 Новый глава союза «{escape(mine['name'])}»: {target['flag']} {escape(target['name'])}.")
        return

    await message.answer(HELP)


async def _npc_join_decision(message, bot, db, gm, chat_id, turn, alliance, leader, applicant, rid, announce):
    try:
        answer = await npc_decides(db, gm, chat_id, leader, applicant,
                                   f"Заявка {applicant['name']} на вступление в ваш союз «{alliance['name']}», где вы — глава.")
    except AIError as e:
        await message.answer(f"⚠️ {escape(str(e))}")
        return
    await db.set_alliance_request(rid, "accepted" if answer["accepted"] else "rejected")
    if answer["accepted"]:
        await add_member(db, chat_id, alliance, applicant, turn)
    verdict = "✅ принята в союз" if answer["accepted"] else "❌ получает отказ"
    await announce(f"{applicant['flag']} <b>{escape(applicant['name'])}</b> {verdict} «{escape(alliance['name'])}».\n"
                   f"👑 {leader['flag']} {escape(leader['name'])} (ИИ): <i>{escape(answer['reply'])}</i>")


@router.callback_query(F.data.startswith("al:"))
async def cb_alliance(call: CallbackQuery, db: Database):
    _, rid, choice = call.data.split(":")
    req = await db.get_alliance_request(int(rid))
    if not req or req["status"] != "pending":
        await call.answer("Решение уже принято", show_alert=True)
        return
    alliance = await db.get_alliance(req["alliance_id"])
    country = await db.get_country(req["country_id"])
    if not alliance or alliance["status"] != "active" or not country or not country["alive"]:
        await db.set_alliance_request(req["id"], "cancelled")
        await call.answer("Союз или страна больше не существует", show_alert=True)
        return
    decider = await db.get_country(alliance["leader_id"]) if req["kind"] == "join" else country
    if call.from_user.id != decider["user_id"]:
        await call.answer(f"Решает лидер страны {decider['name']}", show_alert=True)
        return
    chat_id = alliance["chat_id"]
    if choice != "yes":
        await db.set_alliance_request(req["id"], "rejected")
        await call.message.edit_text(f"❌ {country['flag']} {escape(country['name'])} — в союз «{escape(alliance['name'])}» "
                                     "не вступает.")
        await call.answer()
        return
    if reason := await join_blockers(db, chat_id, alliance, country):
        await db.set_alliance_request(req["id"], "cancelled")
        await call.answer(reason, show_alert=True)
        return
    game = await db.get_game(chat_id)
    await add_member(db, chat_id, alliance, country, game["turn"])
    await db.set_alliance_request(req["id"], "accepted")
    await call.message.edit_text(f"🤝 {country['flag']} <b>{escape(country['name'])}</b> вступает в союз "
                                 f"«{escape(alliance['name'])}»!\n\n" + await card(db, alliance))
    await call.answer()
