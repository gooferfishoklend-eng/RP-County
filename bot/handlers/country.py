from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.handlers.common import is_private, player_context
from bot.stats import STAT_KEYS
from bot.texts import STATUS_RU, country_card, ranking

router = Router()


@router.message(Command("take"), F.chat.type != "private")
async def cmd_take(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    chat_id, user = message.chat.id, message.from_user
    game = await db.get_game(chat_id)
    if not game or game["status"] != "active":
        await message.answer("Сначала админ должен начать игру: /newgame")
        return
    if existing := await db.get_country_by_user(chat_id, user.id):
        await message.answer(f"Вы уже управляете страной {existing['flag']} {escape(existing['name'])}. Чтобы сменить — /leave.")
        return
    name = (command.args or "").strip()
    if not name or len(name) > 60:
        await message.answer("Укажите страну: /take <i>название</i>, например <code>/take Бразилия</code>")
        return
    if taken := await db.get_country_by_name(chat_id, name):
        await message.answer(f"{taken['flag']} {escape(taken['name'])} уже занята игроком {escape(taken['player_name'] or '')}.")
        return

    wait = await message.answer(f"🛰 Собираю разведданные о стране «{escape(name)}»…")
    try:
        data = await gm.create_country(name, settings.start_year)
    except AIError as e:
        await wait.edit_text(f"⚠️ {escape(str(e))}")
        return
    if not data["valid"]:
        await wait.edit_text(f"🤔 «{escape(name)}» не похоже на реальную современную страну. Попробуйте другое название.")
        return
    if taken := await db.get_country_by_name(chat_id, data["name"]):
        await wait.edit_text(f"{taken['flag']} {escape(taken['name'])} уже занята игроком {escape(taken['player_name'] or '')}.")
        return

    for key in STAT_KEYS:
        data[key] = float(data[key])
    for key in ("stability", "approval", "military", "tech", "corruption", "influence"):
        data[key] = int(max(0, min(100, round(data[key]))))
    data["gdp"] = max(1.0, round(data["gdp"], 1))
    data["population"] = max(0.01, round(data["population"], 2))
    data["budget"] = round(data["budget"], 1)

    country_id = await db.add_country(chat_id, user.id, user.full_name, data)
    await db.set_active_chat(user.id, chat_id)
    country = await db.get_country(country_id)

    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Открыть канал секретной связи", url=f"https://t.me/{me.username}?start=play_{chat_id}")
    await wait.edit_text(
        f"🎖 Новый лидер страны — {escape(user.full_name)}!\n\n{country_card(country)}\n\n<i>{escape(data['description'])}</i>\n\n"
        "Нажмите кнопку ниже, чтобы получать секретные доклады и отдавать тайные приказы в личке.",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("leave"))
async def cmd_leave(message: Message, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    _, country = ctx
    await db.delete_country(country["id"])
    text = f"🏳️ {escape(message.from_user.full_name)} уходит в отставку. {country['flag']} {escape(country['name'])} снова свободна."
    await message.answer(text)
    if is_private(message):
        await bot.send_message(country["chat_id"], text)


@router.message(Command("me"))
async def cmd_me(message: Message, db: Database):
    ctx = await player_context(message, db)
    if ctx:
        await message.answer(country_card(ctx[1]))


@router.message(Command("world"))
async def cmd_world(message: Message, db: Database):
    chat_id = message.chat.id if not is_private(message) else await db.get_active_chat(message.from_user.id)
    if not chat_id:
        await message.answer("Сначала выберите игру: /play")
        return
    await message.answer(ranking(await db.list_countries(chat_id)))


@router.message(Command("relations"))
async def cmd_relations(message: Message, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    others = [c for c in await db.list_countries(game["chat_id"]) if c["id"] != me["id"]]
    if not others:
        await message.answer("Других стран-игроков пока нет.")
        return
    lines = [f"🤝 <b>Отношения: {me['flag']} {escape(me['name'])}</b>", ""]
    for c in others:
        rel = await db.get_relation(game["chat_id"], me["id"], c["id"])
        v = rel["value"]
        icon = "💚" if v >= 40 else "🟢" if v >= 10 else "⚪️" if v > -10 else "🟠" if v > -40 else "🔴"
        lines.append(f"{icon} {c['flag']} {escape(c['name'])}: {v:+d} · {STATUS_RU.get(rel['status'], rel['status'])}")
    await message.answer("\n".join(lines))
