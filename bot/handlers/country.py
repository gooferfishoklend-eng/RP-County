from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import territory
from bot.geo import load_world
from bot.handlers.common import find_country, game_chat_id, is_private, player_context, send_map
from bot.stats import BOUNDED_KEYS, turn_label
from bot.texts import STATUS_RU, country_card, ranking

router = Router()

MAP_MODES = {"события": "events", "events": "events", "эпидемии": "events", "болезни": "events"}


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
    country = await find_country(db, chat_id, name)
    if not country:
        await message.answer(f"🤔 Не нашёл на карте страну «{escape(name)}». Попробуйте другое название.")
        return
    if country["user_id"]:
        await message.answer(f"{country['flag']} {escape(country['name'])} уже занята игроком {escape(country['player_name'] or '')}.")
        return

    wait = await message.answer(f"🛰 Собираю разведданные: {country['flag']} {escape(country['name'])}…")
    try:
        profile = await gm.country_profile(country, settings.start_year)
    except AIError:
        profile = {**country, "description": ""}
    for key in BOUNDED_KEYS:
        profile[key] = int(max(0, min(100, round(float(profile[key])))))
    profile["gdp"] = max(1.0, round(float(profile["gdp"]), 1))
    profile["population"] = max(0.01, round(float(profile["population"]), 2))
    profile["budget"] = round(float(profile["budget"]), 1)

    if (await db.get_country(country["id"]))["user_id"]:
        await wait.edit_text("Эту страну только что занял другой игрок.")
        return
    await db.assign_player(country["id"], user.id, user.full_name, profile)
    await db.set_active_chat(user.id, chat_id)
    country = await db.get_country(country["id"])

    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.button(text="🔒 Открыть канал секретной связи", url=f"https://t.me/{me.username}?start=play_{chat_id}")
    await wait.edit_text(
        f"🎖 Новый лидер страны — {escape(user.full_name)}!\n\n{country_card(country)}\n\n"
        f"<i>{escape(profile.get('description') or '')}</i>\n\n"
        "Нажмите кнопку ниже, чтобы получать секретные доклады, отдавать тайные приказы и командовать генералами в личке.",
        reply_markup=kb.as_markup(),
    )
    await send_map(bot, db, chat_id, chat_id, f"{country['name']} и соседи", focus=country["id"])


@router.message(Command("leave"))
async def cmd_leave(message: Message, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    _, country = ctx
    await db.release_player(country["id"])
    text = (f"🏳️ {escape(message.from_user.full_name)} уходит в отставку. "
            f"{country['flag']} {escape(country['name'])} переходит под управление ИИ и свободна для других игроков.")
    await message.answer(text)
    if is_private(message):
        await bot.send_message(country["chat_id"], text)


@router.message(Command("me"))
async def cmd_me(message: Message, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    owner, core = await db.cell_owners(game["chat_id"])
    by_id = {c["id"]: c for c in await db.list_countries(game["chat_id"])}
    t = territory(load_world(), owner, core, country, by_id)
    lines = [country_card(country), "",
             f"🗺 Провинций: <b>{t['provinces']}</b> (исконных: {t['core_provinces']})",
             f"🏛 Столица: {escape(t['capital'] or '—')}"]
    if t["occupied_by"]:
        lines.append("🔴 Оккупировано: " + escape(", ".join(f"{k} — {v}" for k, v in t["occupied_by"].items())))
    if t["occupying"]:
        lines.append("🟢 Под нашим контролем: " + escape(", ".join(f"{k} — {v}" for k, v in t["occupying"].items())))
    if t["neighbors"]:
        lines.append("🧭 Соседи: " + escape(", ".join(t["neighbors"])))
    await message.answer("\n".join(lines))


@router.message(Command("world"))
async def cmd_world(message: Message, db: Database):
    chat_id = await game_chat_id(message, db)
    if not chat_id:
        await message.answer("Сначала выберите игру: /play")
        return
    await message.answer(ranking(await db.list_players(chat_id)))


@router.message(Command("map"))
async def cmd_map(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    chat_id = await game_chat_id(message, db)
    game = await db.get_game(chat_id) if chat_id else None
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    arg = (command.args or "").strip()
    label = turn_label(game["turn"], settings.start_year)
    mode, focus, title = "political", None, f"Мир: {label}"
    if arg.casefold() in MAP_MODES:
        mode, title = "events", f"Мировые события: {label}"
    elif arg.casefold() in ("я", "моя", "мой", "me"):
        mine = await db.get_country_by_user(chat_id, message.from_user.id)
        if not mine:
            await message.answer("У вас нет страны.")
            return
        focus, title = mine["id"], f"{mine['name']}: {label}"
    elif arg:
        country = await find_country(db, chat_id, arg)
        if not country:
            await message.answer(f"Не нашёл страну «{escape(arg)}».")
            return
        focus, title = country["id"], f"{country['name']}: {label}"
    caption = "Номера на карте — id провинций для /transfer и /demand." if focus else \
        "Подробная карта страны: /map <i>страна</i> · события: /map события"
    await bot.send_chat_action(message.chat.id, "upload_photo")
    await send_map(bot, db, message.chat.id, chat_id, title, caption=caption, focus=focus, mode=mode)


@router.message(Command("relations"))
async def cmd_relations(message: Message, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, me = ctx
    names = {c["id"]: c for c in await db.list_countries(game["chat_id"])}
    rels = [r for r in await db.list_relations(game["chat_id"]) if me["id"] in (r["a_id"], r["b_id"])]
    if not rels:
        await message.answer("Отношения со всеми странами пока нейтральные.")
        return
    lines = [f"🤝 <b>Отношения: {me['flag']} {escape(me['name'])}</b>", ""]
    for r in sorted(rels, key=lambda r: -r["value"]):
        other = names[r["b_id"] if r["a_id"] == me["id"] else r["a_id"]]
        v = r["value"]
        icon = "⚔️" if r["status"] == "war" else "💚" if v >= 40 else "🟢" if v >= 10 else "⚪️" if v > -10 else "🟠" if v > -40 else "🔴"
        tag = "" if other["user_id"] else " <i>(ИИ)</i>"
        lines.append(f"{icon} {other['flag']} {escape(other['name'])}{tag}: {v:+d} · {STATUS_RU.get(r['status'], r['status'])}")
    await message.answer("\n".join(lines))
