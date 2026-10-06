"""Secret admin tools (used in DM with the bot) and history export."""

import re
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from bot.config import Settings
from bot.db import Database
from bot.export import build_history
from bot.game import end_war, ensure_general, fix_capital
from bot.ai import GameMaster
from bot.geo import load_world
from bot.handlers.common import find_country, game_chat_id, is_admin, is_private, send_dm
from bot.stats import BOUNDED_KEYS, STATS, clamp
from bot.texts import STATUS_RU, country_card

router = Router()

STAT_ALIASES = {
    "ввп": "gdp", "gdp": "gdp", "экономика": "gdp",
    "казна": "budget", "бюджет": "budget", "budget": "budget", "деньги": "budget",
    "население": "population", "population": "population",
    "стабильность": "stability", "stability": "stability",
    "рейтинг": "approval", "approval": "approval", "поддержка": "approval",
    "армия": "military", "military": "military", "армию": "military",
    "технологии": "tech", "техно": "tech", "tech": "tech",
    "коррупция": "corruption", "corruption": "corruption", "коррупцию": "corruption",
    "влияние": "influence", "influence": "influence",
}
STATUS_ALIASES = {"мир": "peace", "peace": "peace", "война": "war", "war": "war", "союз": "alliance",
                  "alliance": "alliance", "напряжённость": "tension", "напряженность": "tension", "tension": "tension"}

ADMIN_HELP = """🛠 <b>Тайная админ-панель</b> (команды работают только здесь, в личке; игроки их не видят)

<code>/set страна показатель значение</code>
  показатели: ввп, казна, население, стабильность, рейтинг, армия, технологии, коррупция, влияние
  значение: <code>500</code> — установить, <code>+50</code> / <code>-10</code> — изменить, <code>+20%</code> — в процентах
  примеры: <code>/set Россия коррупция 20</code> · <code>/set Китай ввп +10%</code> · <code>/set Украина армия +15</code>

<code>/setrel страна | страна | мир/война/союз/напряжённость | [доверие -100..100]</code>
  пример: <code>/setrel Россия | Украина | мир</code>

<code>/setowner номера провинций | страна</code> — передать провинции (номера видно на /map страна)

<code>/look страна</code> — все показатели и тайная записная книжка страны-ИИ
<code>/export</code> — история игры файлом (здесь — вместе с вашими тайными приказами)"""


async def _admin_game(message: Message, bot: Bot, db: Database) -> int | None:
    """Admin tools work only in DM, for the game the admin last opened with /admin."""
    if not is_private(message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer("🤫 Админ-команды пишите боту в личку. Сначала отправьте /admin в игровой группе.")
        return None
    chat_id = await db.get_active_chat(message.from_user.id)
    game = await db.get_game(chat_id) if chat_id else None
    if not game:
        await message.answer("Сначала отправьте /admin в игровой группе — я привяжу панель к этой игре.")
        return None
    try:
        if not await is_admin(bot, chat_id, message.from_user.id):
            await message.answer("Вы не администратор игровой группы.")
            return None
    except TelegramBadRequest:
        await message.answer("Не вижу игровую группу.")
        return None
    return chat_id


@router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot, db: Database):
    if is_private(message):
        if await _admin_game(message, bot, db):
            await message.answer(ADMIN_HELP)
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    if not await db.get_game(message.chat.id) or not await is_admin(bot, message.chat.id, message.from_user.id):
        return
    await db.set_active_chat(message.from_user.id, message.chat.id)
    if not await send_dm(bot, message.from_user.id, ADMIN_HELP):
        me = await bot.me()
        await message.answer(f"Откройте личку с ботом (@{me.username}), нажмите Start и повторите /admin.")


def _parse_value(raw: str, current: float) -> float | None:
    m = re.fullmatch(r"([+-]?)(\d+(?:[.,]\d+)?)(%?)", raw.strip())
    if not m:
        return None
    sign, num, pct = m.groups()
    num = float(num.replace(",", "."))
    delta = current * num / 100 if pct else num
    if sign == "+":
        return current + delta
    if sign == "-":
        return current - delta
    return current * num / 100 if pct else num


@router.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject, bot: Bot, db: Database):
    chat_id = await _admin_game(message, bot, db)
    if not chat_id:
        return
    tokens = (command.args or "").split()
    if len(tokens) < 3:
        await message.answer(ADMIN_HELP)
        return
    raw_value, stat_raw, country_name = tokens[-1], tokens[-2].casefold(), " ".join(tokens[:-2])
    stat = STAT_ALIASES.get(stat_raw)
    if not stat:
        await message.answer(f"Не знаю показатель «{escape(stat_raw)}». Доступны: ввп, казна, население, стабильность, "
                             "рейтинг, армия, технологии, коррупция, влияние.")
        return
    country = await find_country(db, chat_id, country_name)
    if not country:
        await message.answer(f"Не нашёл страну «{escape(country_name)}».")
        return
    value = _parse_value(raw_value, country[stat])
    if value is None:
        await message.answer("Значение: число (<code>50</code>), изменение (<code>+5</code>, <code>-10</code>) или процент (<code>+20%</code>).")
        return
    if stat in BOUNDED_KEYS:
        value = int(clamp(round(value), 0, 100))
    elif stat in ("gdp", "population"):
        value = round(max(0.01, value), 2)
    else:
        value = round(value, 1)
    await db.update_country_stats(country["id"], {stat: value})
    title = next(s.title for s in STATS if s.key == stat)
    await message.answer(f"✅ {country['flag']} {escape(country['name'])}: {title} {country[stat]:g} → <b>{value:g}</b>\n"
                         "<i>Изменение тайное — игроки увидят только новые цифры.</i>")


@router.message(Command("setrel"))
async def cmd_setrel(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster):
    chat_id = await _admin_game(message, bot, db)
    if not chat_id:
        return
    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) < 3:
        await message.answer("Формат: <code>/setrel страна | страна | мир/война/союз/напряжённость | [доверие]</code>")
        return
    a, b = await find_country(db, chat_id, parts[0]), await find_country(db, chat_id, parts[1])
    status = STATUS_ALIASES.get(parts[2].casefold())
    if not a or not b or a["id"] == b["id"] or not status:
        await message.answer("Проверьте названия стран и статус (мир, война, союз, напряжённость).")
        return
    rel = await db.get_relation(chat_id, a["id"], b["id"])
    if status == "war":
        await db.change_relation(chat_id, a["id"], b["id"], 0, "war")
        for side, enemy in ((a, b), (b, a)):
            await ensure_general(db, gm, chat_id, side, enemy["id"])
    elif rel["status"] == "war":
        await end_war(db, chat_id, a["id"], b["id"], status, 0)
    else:
        await db.change_relation(chat_id, a["id"], b["id"], 0, status)
    if len(parts) >= 4 and re.fullmatch(r"-?\d+", parts[3]):
        rel = await db.get_relation(chat_id, a["id"], b["id"])
        await db.change_relation(chat_id, a["id"], b["id"], int(parts[3]) - rel["value"])
    rel = await db.get_relation(chat_id, a["id"], b["id"])
    await message.answer(f"✅ {a['flag']} {escape(a['name'])} — {b['flag']} {escape(b['name'])}: "
                         f"<b>{STATUS_RU.get(rel['status'], rel['status'])}</b> ({rel['value']:+d})")


@router.message(Command("setowner"))
async def cmd_setowner(message: Message, command: CommandObject, bot: Bot, db: Database):
    chat_id = await _admin_game(message, bot, db)
    if not chat_id:
        return
    cells_raw, _, name = (command.args or "").partition("|")
    country = await find_country(db, chat_id, name.strip()) if name.strip() else None
    cells = [int(x) for x in re.findall(r"\d+", cells_raw)][:100]
    world = load_world()
    owner, core = await db.cell_owners(chat_id)
    cells = [c for c in cells if c in owner]
    if not country or not cells:
        await message.answer("Формат: <code>/setowner 3676, 3677 | Россия</code> (номера провинций — на /map страна)")
        return
    losers = {owner[c] for c in cells} - {country["id"]}
    for c in cells:
        await db.set_cell_owner(chat_id, c, country["id"], country["id"])
        owner[c] = core[c] = country["id"]
    for cid in losers | {country["id"]}:
        c = await db.get_country(cid)
        if c and c["alive"]:
            await fix_capital(db, world, chat_id, c, owner, core)
    await message.answer(f"✅ {len(cells)} провинций теперь у {country['flag']} {escape(country['name'])}: "
                         + escape(", ".join(world.cells[c].label for c in cells[:15])))


@router.message(Command("look"))
async def cmd_look(message: Message, command: CommandObject, bot: Bot, db: Database):
    chat_id = await _admin_game(message, bot, db)
    if not chat_id:
        return
    country = await find_country(db, chat_id, (command.args or "").strip())
    if not country:
        await message.answer("Формат: <code>/look страна</code>")
        return
    notes = country.get("notes") or "— пусто —"
    await message.answer(f"{country_card(country)}\n\n📓 <b>Записная книжка (память ИИ):</b>\n{escape(notes)}")


@router.message(Command("export"))
async def cmd_export(message: Message, bot: Bot, db: Database, settings: Settings):
    chat_id = await game_chat_id(message, db)
    game = await db.get_game(chat_id) if chat_id else None
    if not game:
        await message.answer("Игры не найдено.")
        return
    viewer = None
    if is_private(message):
        mine = await db.get_country_by_user(chat_id, message.from_user.id)
        viewer = mine["id"] if mine else None
    text = await build_history(db, chat_id, settings.start_year, viewer)
    caption = "📜 История игры" + (" (с вашими тайными приказами)" if viewer else "")
    await bot.send_document(message.chat.id, BufferedInputFile(text.encode("utf-8"), filename="history.txt"), caption=caption)
