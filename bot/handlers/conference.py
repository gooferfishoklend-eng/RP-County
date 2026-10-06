import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatJoinRequest, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.conference import clean_terms, conference_context, execute_terms, participants, render_terms
from bot.config import Settings
from bot.db import Database
from bot.handlers.common import find_country, get_game_healed, is_admin, is_private, player_context, send_dm
from bot.texts import split_message

log = logging.getLogger(__name__)
router = Router()

MAX_PARTIES = 8
ROOM_TITLE = "🏛 Комната переговоров"
_npc_last: dict[tuple[int, int], float] = {}

TG_ERRORS = (TelegramBadRequest, TelegramForbiddenError)


async def room_say(bot: Bot, db: Database, conf_id: int, room_chat_id: int, text: str, *, kind: str = "system",
                   country_id: int | None = None, speaker: str = "бот", log_text: str | None = None, **kwargs) -> None:
    for part in split_message(text):
        msg = await bot.send_message(room_chat_id, part, **kwargs)
        kwargs.pop("reply_markup", None)
        await db.log_conference(conf_id, msg.message_id, kind, country_id, speaker, log_text or part)


async def close_room(bot: Bot, db: Database, conf: dict) -> None:
    room = conf["room_chat_id"]
    if conf.get("invite_link"):
        try:
            await bot.revoke_chat_invite_link(room, conf["invite_link"])
        except TG_ERRORS:
            pass
    ids = [r["message_id"] for r in await db.conference_log(conf["id"])]
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(room, ids[i:i + 100])
        except TG_ERRORS:
            log.warning("could not delete conference messages in %s", room)
    for m in await db.conference_members(conf["id"]):
        if not m["user_id"]:
            continue
        try:
            member = await bot.get_chat_member(room, m["user_id"])
            if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.LEFT,
                                 ChatMemberStatus.KICKED):
                continue
            await bot.ban_chat_member(room, m["user_id"])
            await bot.unban_chat_member(room, m["user_id"], only_if_banned=True)
        except TG_ERRORS:
            pass
    try:
        await bot.set_chat_title(room, ROOM_TITLE)
    except TG_ERRORS:
        pass
    await db.set_room_conference(room, None)


# --- room setup -------------------------------------------------------------

ROOM_RIGHTS = {
    "can_invite_users": "приглашать пользователей",
    "can_restrict_members": "блокировать участников",
    "can_delete_messages": "удалять сообщения",
}
LINK_RIGHTS = "change_info+delete_messages+restrict_members+invite_users"


async def missing_rights(bot: Bot, room_id: int) -> list[str]:
    me = await bot.get_chat_member(room_id, (await bot.me()).id)
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        return list(ROOM_RIGHTS.values())
    return [title for attr, title in ROOM_RIGHTS.items() if not getattr(me, attr, False)]


async def register_room(message: Message, bot: Bot, db: Database, game_chat: int) -> None:
    room_id = message.chat.id
    if game_chat == room_id:
        await message.answer(
            "Вы указали ID этой же группы. Комната переговоров — это <b>отдельная</b> группа.\n"
            "Откройте игровую группу (где запускали /newgame), напишите там /addroom и нажмите кнопку "
            "«Подключить комнату» — бот подключит её сам."
        )
        return
    game = await get_game_healed(bot, db, game_chat)
    if not game or game["status"] != "active":
        await message.answer(
            f"В группе <code>{game_chat}</code> нет активной игры.\n"
            "Откройте игровую группу (где запускали /newgame), напишите там /addroom и нажмите кнопку "
            "«Подключить комнату». Если игра ещё не начата — сначала /newgame."
        )
        return
    if await db.get_game(room_id):
        await message.answer("В этой группе идёт своя игра, её нельзя сделать комнатой. Создайте отдельную пустую группу.")
        return
    try:
        if not await is_admin(bot, game["chat_id"], message.from_user.id):
            await message.answer("Подключать комнаты может только администратор игровой группы.")
            return
    except TG_ERRORS:
        await message.answer("Не вижу игровую группу — я в ней состою?")
        return
    existing = await db.get_room(room_id)
    if existing and existing["game_chat_id"] == game["chat_id"]:
        missing = await missing_rights(bot, room_id)
        await message.answer("Эта комната уже подключена к игре. " + (
            "Все права на месте ✅" if not missing else "⚠️ Не хватает прав: " + ", ".join(missing) + "."))
        return
    await db.add_room(room_id, game["chat_id"], message.chat.title or "")
    try:
        await bot.set_chat_title(room_id, ROOM_TITLE)
    except TG_ERRORS:
        pass
    missing = await missing_rights(bot, room_id)
    text = "✅ Комната переговоров подключена. Здесь будут проходить мирные конференции."
    if missing:
        text += ("\n\n⚠️ Чтобы проводить конференции, мне нужны права администратора: " + ", ".join(missing)
                 + ". Выдайте их и напишите /room — я проверю.")
    await message.answer(text)
    await bot.send_message(game["chat_id"], "🏛 Подключена новая комната переговоров. Созвать конференцию: "
                                            "/conference <i>страна, страна</i> | <i>тема</i>")


@router.message(Command("addroom"), F.chat.type != "private")
async def cmd_addroom(message: Message, bot: Bot, db: Database):
    if await db.get_room(message.chat.id):
        await message.answer("Это комната переговоров. Команду /addroom пишут в <b>игровой группе</b>, где идёт игра.")
        return
    game = await get_game_healed(bot, db, message.chat.id)
    if not game or game["status"] != "active":
        await message.answer(
            "Здесь нет игры. Команду /addroom пишут в <b>игровой группе</b> — там, где запускали /newgame.\n"
            "Если эта группа должна стать комнатой переговоров — откройте игровую группу, напишите там /addroom "
            "и нажмите кнопку «Подключить комнату»."
        )
        return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Комнаты переговоров добавляют администраторы.")
        return
    rooms = await db.list_rooms(message.chat.id)
    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Подключить комнату",
              url=f"https://t.me/{me.username}?startgroup=room_{message.chat.id}&admin={LINK_RIGHTS}")
    await message.answer(
        "🏛 <b>Комната для мирных конференций</b>\n\n"
        "Боты Telegram не умеют сами создавать группы, поэтому комнату нужно подключить один раз:\n"
        "1. Нажмите кнопку ниже.\n"
        "2. Выберите пустую группу (или создайте новую). Telegram сразу предложит дать мне права администратора — "
        "подтвердите.\n"
        "3. Всё — я подключу комнату сам и отпишусь здесь.\n\n"
        f"<i>Вручную: добавьте меня в группу администратором и напишите там</i> <code>/room {message.chat.id}</code>\n\n"
        f"Можно подключить несколько комнат для параллельных конференций. Сейчас комнат: {len(rooms)}.",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("room"), F.chat.type != "private")
async def cmd_room(message: Message, command: CommandObject, bot: Bot, db: Database):
    arg = (command.args or "").strip()
    if not arg:
        if not await db.get_room(message.chat.id):
            await message.answer("Эта группа не подключена как комната. Напишите /addroom в игровой группе и нажмите кнопку.")
            return
        missing = await missing_rights(bot, message.chat.id)
        await message.answer("✅ Комната подключена, все права на месте." if not missing else
                             "⚠️ Не хватает прав администратора: " + ", ".join(missing) + ".")
        return
    try:
        game_chat = int(arg)
    except ValueError:
        await message.answer("Формат: <code>/room ID_игровой_группы</code>. Проще — нажать кнопку из /addroom в игровой группе.")
        return
    await register_room(message, bot, db, game_chat)


@router.message(Command("rooms"))
async def cmd_rooms(message: Message, db: Database):
    chat_id = message.chat.id if not is_private(message) else await db.get_active_chat(message.from_user.id)
    rooms = await db.list_rooms(chat_id) if chat_id else []
    if not rooms:
        await message.answer("Комнат переговоров нет. Админ может добавить: /addroom")
        return
    lines = ["🏛 <b>Комнаты переговоров</b>", ""]
    for r in rooms:
        if r["conference_id"]:
            conf = await db.get_conference(r["conference_id"])
            lines.append(f"🔴 занята: конференция №{conf['id']} — {escape(conf['topic'])}")
        else:
            lines.append("🟢 свободна")
    await message.answer("\n".join(lines))


# --- conference lifecycle ---------------------------------------------------

@router.message(Command("conference"))
async def cmd_conference(message: Message, command: CommandObject, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    chat_id = game["chat_id"]
    names_raw, _, topic = (command.args or "").partition("|")
    names = [n.strip() for n in names_raw.replace(";", ",").split(",") if n.strip()]
    if not names:
        await message.answer("Формат: /conference <i>страна, страна, …</i> | <i>тема</i>\n"
                             "Например: <code>/conference Россия, Турция | мирные переговоры по войне</code>")
        return
    parties = {me["id"]: me}
    for name in names:
        c = await find_country(db, chat_id, name)
        if not c:
            await message.answer(f"Не нашёл страну «{escape(name)}».")
            return
        parties[c["id"]] = c
    if len(parties) < 2:
        await message.answer("Нужен хотя бы один участник кроме вас.")
        return
    if len(parties) > MAX_PARTIES:
        await message.answer(f"Не больше {MAX_PARTIES} участников.")
        return

    room = next((r for r in await db.list_rooms(chat_id) if not r["conference_id"]), None)
    if not room:
        await message.answer("Все комнаты переговоров заняты или их нет. Админ может добавить комнату: /addroom")
        return
    topic = topic.strip()[:300] or "Мирная конференция"
    conf_id = await db.create_conference(chat_id, room["room_chat_id"], topic, me["id"], game["turn"],
                                         [(c["id"], c["user_id"]) for c in parties.values()])
    await db.set_room_conference(room["room_chat_id"], conf_id)
    title = "🕊 " + " — ".join(c["name"] for c in parties.values())
    try:
        await bot.set_chat_title(room["room_chat_id"], title[:128])
    except TG_ERRORS:
        pass
    try:
        link = await bot.create_chat_invite_link(room["room_chat_id"], name=f"Конференция {conf_id}",
                                                 creates_join_request=True)
    except TG_ERRORS:
        await db.set_room_conference(room["room_chat_id"], None)
        await db.update_conference(conf_id, status="closed")
        await message.answer("⚠️ Не могу создать ссылку в комнате — проверьте мои права администратора там.")
        return
    await db.update_conference(conf_id, invite_link=link.invite_link)

    roster = "\n".join(
        f"{c['flag']} {escape(c['name'])} — " + (escape(c['player_name'] or '') if c["user_id"] else "делегация ИИ")
        for c in parties.values())
    await room_say(bot, db, conf_id, room["room_chat_id"],
                   f"🕊 <b>Конференция №{conf_id}</b>\nТема: {escape(topic)}\n\n<b>Участники:</b>\n{roster}\n\n"
                   "Ведите переговоры обычными сообщениями. Делегации ИИ отвечают, когда к ним обращаются по названию страны.\n"
                   "Когда договоритесь — /draft: ИИ проанализирует переговоры и составит договор.\n"
                   "Закрыть без соглашения — /endconf.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🚪 Войти в зал переговоров", url=link.invite_link)
    await bot.send_message(
        chat_id,
        f"🕊 <b>{me['flag']} {escape(me['name'])} созывает конференцию №{conf_id}</b>\nТема: {escape(topic)}\n\n{roster}\n\n"
        "Участники входят по кнопке ниже — бот пускает только приглашённых лидеров.",
        reply_markup=kb.as_markup(),
    )
    for c in parties.values():
        if c["user_id"]:
            await send_dm(bot, c["user_id"], f"🕊 Вас пригласили на конференцию №{conf_id}: {escape(topic)}\n"
                                             f"Вход: {link.invite_link}")
    if is_private(message):
        await message.answer("Конференция созвана, приглашения разосланы.")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot, db: Database):
    room = await db.get_room(request.chat.id)
    if not room or not room["conference_id"]:
        await request.decline()
        return
    conf = await db.get_conference(room["conference_id"])
    member = next((m for m in await db.conference_members(conf["id"]) if m["user_id"] == request.from_user.id), None)
    if not member:
        await request.decline()
        return
    await request.approve()
    await db.set_member(conf["id"], member["country_id"], joined=1)
    country = await db.get_country(member["country_id"])
    await room_say(bot, db, conf["id"], request.chat.id,
                   f"🚪 Прибыла делегация: {country['flag']} <b>{escape(country['name'])}</b> "
                   f"({escape(country['player_name'] or '')})")


async def _room_conference(message: Message, db: Database) -> dict | None:
    room = await db.get_room(message.chat.id)
    if not room or not room["conference_id"]:
        return None
    conf = await db.get_conference(room["conference_id"])
    return conf if conf and conf["status"] == "open" else None


@router.message(Command("draft"), F.chat.type != "private")
async def cmd_draft(message: Message, bot: Bot, db: Database, gm: GameMaster):
    conf = await _room_conference(message, db)
    if not conf:
        return
    people = await participants(db, conf["id"])
    if message.from_user.id not in {p["user_id"] for p in people if p["user_id"]}:
        return
    await db.log_conference(conf["id"], message.message_id, "system", None, "команда", "/draft")
    context = await conference_context(db, conf)
    if not any(line["country_id"] for line in context["transcript"]):
        await room_say(bot, db, conf["id"], message.chat.id, "Сначала обсудите условия — стенограмма пуста.")
        return
    await room_say(bot, db, conf["id"], message.chat.id, "🧾 Секретариат анализирует переговоры и готовит проект договора…")
    try:
        terms = await gm.conference_draft(context)
    except AIError as e:
        await room_say(bot, db, conf["id"], message.chat.id, f"⚠️ {escape(str(e))}")
        return
    owner, _ = await db.cell_owners(conf["chat_id"])
    terms = clean_terms(terms, {p["id"] for p in people}, owner)
    version = conf["draft_version"] + 1
    await db.update_conference(conf["id"], draft=terms, draft_version=version)
    await db.reset_signatures(conf["id"])
    npc_ok = {pos["country_id"]: pos["accepts"] for pos in terms["npc_positions"]}
    for p in people:
        if not p["user_id"]:
            await db.set_member(conf["id"], p["id"], signed=int(npc_ok.get(p["id"], False)))
    names = {p["id"]: p["name"] for p in people}
    await room_say(bot, db, conf["id"], message.chat.id, render_terms(terms, names))
    await room_say(bot, db, conf["id"], message.chat.id, await _signature_status(db, conf["id"], version),
                   reply_markup=_sign_kb(conf["id"], version))


def _sign_kb(conf_id: int, version: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Подписать", callback_data=f"cf:{conf_id}:{version}:sign")
    kb.button(text="✋ Отказаться", callback_data=f"cf:{conf_id}:{version}:no")
    return kb.as_markup()


async def _signature_status(db: Database, conf_id: int, version: int) -> str:
    lines = [f"✍️ <b>Подписи под проектом v{version}</b>"]
    for m in await db.conference_members(conf_id):
        c = await db.get_country(m["country_id"])
        who = "" if m["user_id"] else " (ИИ)"
        lines.append(f"{'✅' if m['signed'] else '⌛'} {c['flag']} {escape(c['name'])}{who}")
    lines.append("\nДоговор вступит в силу, когда подпишут все. Чтобы изменить условия — продолжайте переговоры и снова /draft.")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("cf:"))
async def cb_sign(call: CallbackQuery, bot: Bot, db: Database, settings: Settings):
    _, conf_id, version, action = call.data.split(":")
    conf = await db.get_conference(int(conf_id))
    if not conf or conf["status"] != "open":
        await call.answer("Конференция уже закрыта", show_alert=True)
        return
    if int(version) != conf["draft_version"]:
        await call.answer("Это устаревший проект — подпишите последний", show_alert=True)
        return
    member = next((m for m in await db.conference_members(conf["id"]) if m["user_id"] == call.from_user.id), None)
    if not member:
        await call.answer("Подписывают только лидеры стран-участниц", show_alert=True)
        return
    country = await db.get_country(member["country_id"])
    if action == "no":
        await db.set_member(conf["id"], member["country_id"], signed=0)
        await call.answer("Вы отказались подписывать")
        await room_say(bot, db, conf["id"], conf["room_chat_id"],
                       f"✋ {country['flag']} {escape(country['name'])} отказывается подписывать этот проект.")
        return
    await db.set_member(conf["id"], member["country_id"], signed=1)
    await call.answer("Подпись поставлена")
    members = await db.conference_members(conf["id"])
    if not all(m["signed"] for m in members):
        try:
            await call.message.edit_text(await _signature_status(db, conf["id"], conf["draft_version"]),
                                         reply_markup=_sign_kb(conf["id"], conf["draft_version"]))
        except TG_ERRORS:
            pass
        return

    effects = await execute_terms(db, conf, conf["draft"])
    people = await participants(db, conf["id"])
    names = {p["id"]: p["name"] for p in people}
    terms = conf["draft"]
    summary = (f"🎉 <b>Договор подписан всеми сторонами!</b>\n\n<b>{escape(terms['title'])}</b>\n{escape(terms['summary'])}"
               "\n\n<b>Исполнено:</b>\n" + ("\n".join(f"• {escape(e)}" for e in effects) or "• условия приняты к исполнению"))
    if terms["clauses"]:
        summary += "\n\n<b>Обязательства сторон:</b>\n" + "\n".join(f"• {escape(c)}" for c in terms["clauses"])
    flags = " ".join(p["flag"] for p in people)
    await bot.send_message(conf["chat_id"], f"🕊 {flags} <b>Итоги конференции №{conf['id']}</b>\n\n" + summary)
    for p in people:
        if p["user_id"]:
            await send_dm(bot, p["user_id"], f"📜 Конференция №{conf['id']} завершена, договор вступил в силу.\n\n{summary}")
    await close_room(bot, db, {**conf, "status": "signed"})


@router.message(Command("endconf"))
async def cmd_endconf(message: Message, bot: Bot, db: Database):
    conf = await _room_conference(message, db) if not is_private(message) else None
    if conf is None:
        ctx = await player_context(message, db)
        if not ctx:
            return
        game, me = ctx
        confs = [c for c in await db.open_conferences(game["chat_id"]) if c["initiator_id"] == me["id"]]
        if not confs:
            await message.answer("У вас нет открытых конференций.")
            return
        conf = await db.get_conference(confs[0]["id"])
    initiator = await db.get_country(conf["initiator_id"])
    allowed = message.from_user.id == initiator["user_id"]
    if not allowed and not is_private(message):
        try:
            allowed = await is_admin(bot, conf["chat_id"], message.from_user.id)
        except TG_ERRORS:
            allowed = False
    if not allowed:
        await message.answer("Закрыть конференцию может её инициатор или админ игры.")
        return
    await db.update_conference(conf["id"], status="closed")
    await bot.send_message(conf["chat_id"], f"🚪 Конференция №{conf['id']} ({escape(conf['topic'])}) закрыта без соглашения.")
    await close_room(bot, db, conf)


# --- talks in the room ------------------------------------------------------

@router.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def on_room_message(message: Message, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    conf = await _room_conference(message, db)
    if not conf:
        return
    people = await participants(db, conf["id"])
    speaker = next((p for p in people if p["user_id"] == message.from_user.id), None)
    if not speaker:
        return
    text = message.text[:2000]
    await db.log_conference(conf["id"], message.message_id, "player", speaker["id"],
                            f"{speaker['name']} ({speaker['player_name']})", text)

    lowered = text.casefold()
    for npc in (p for p in people if not p["user_id"]):
        stem = npc["name"].casefold()[: max(4, len(npc["name"]) - 2)]
        replied_to_npc = bool(message.reply_to_message and message.reply_to_message.text
                              and message.reply_to_message.text.startswith(f"{npc['flag']} {npc['name']}"))
        if stem not in lowered and not replied_to_npc:
            continue
        key = (conf["id"], npc["id"])
        if time.monotonic() - _npc_last.get(key, 0) < settings.conference_npc_cooldown_sec:
            continue
        _npc_last[key] = time.monotonic()
        try:
            reply = await gm.conference_npc_reply(npc, await conference_context(db, conf))
        except AIError:
            continue
        await room_say(bot, db, conf["id"], message.chat.id, f"{npc['flag']} {escape(npc['name'])}: {escape(reply)}",
                       kind="npc", country_id=npc["id"], speaker=f"{npc['name']} (делегация ИИ)", log_text=reply)
