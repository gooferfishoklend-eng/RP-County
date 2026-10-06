import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat
from aiogram.types import BufferedInputFile, Message

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import chat_lock, map_view, resolve_turn
from bot.geo import load_world
from bot.mapdraw import render_map
from bot.stats import turn_label
from bot.texts import ranking, split_message, stat_changes

log = logging.getLogger(__name__)

_render_lock = asyncio.Lock()


def is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def game_chat_id(message: Message, db: Database) -> int | None:
    if is_private(message):
        return await db.get_active_chat(message.from_user.id)
    return message.chat.id


async def heal_migrations(bot: Bot, db: Database) -> list[tuple[int, int]]:
    """Find games stored under a basic-group id whose group Telegram has upgraded to a supergroup, and move them."""
    moved = []
    for old_id in await db.basic_group_game_ids():
        try:
            await bot.send_chat_action(old_id, "typing")
        except TelegramMigrateToChat as e:
            if await db.migrate_chat(old_id, e.migrate_to_chat_id):
                log.info("game chat %s migrated to %s", old_id, e.migrate_to_chat_id)
                moved.append((old_id, e.migrate_to_chat_id))
        except TelegramAPIError:
            continue
    return moved


async def get_game_healed(bot: Bot, db: Database, chat_id: int) -> dict | None:
    game = await db.get_game(chat_id)
    if game is None and chat_id <= -1000000000000:
        await heal_migrations(bot, db)
        game = await db.get_game(chat_id)
    return game


async def player_context(message: Message, db: Database) -> tuple[dict, dict] | None:
    """Find the active game and the sender's country, replying with a hint if missing."""
    user_id = message.from_user.id
    chat_id = await game_chat_id(message, db)
    if chat_id is None:
        await message.answer("Вы ещё не играете. Возьмите страну в группе командой /take, затем выберите игру через /play.")
        return None
    game = await get_game_healed(message.bot, db, chat_id)
    if not game or game["status"] != "active":
        await message.answer("В этой группе нет активной игры. Админ может начать её командой /newgame.")
        return None
    country = await db.get_country_by_user(chat_id, user_id)
    if not country:
        await message.answer("У вас нет страны в этой игре. Возьмите её командой /take <i>название</i>.")
        return None
    return game, country


async def find_country(db: Database, chat_id: int, name: str) -> dict | None:
    code = load_world().find_country(name)
    if not code:
        return None
    country = await db.get_country_by_code(chat_id, code)
    return country if country and country["alive"] else None


async def send_dm(bot: Bot, user_id: int, text: str) -> bool:
    try:
        for part in split_message(text):
            await bot.send_message(user_id, part)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    for part in split_message(text):
        await bot.send_message(chat_id, part)


async def render_png(db: Database, chat_id: int, title: str, focus: int | None = None, mode: str = "political") -> bytes:
    view = await map_view(db, chat_id, title)
    async with _render_lock:
        return await asyncio.to_thread(render_map, load_world(), view, focus, mode)


async def send_map(bot: Bot, db: Database, target_chat: int, game_chat: int, title: str, caption: str = "",
                   focus: int | None = None, mode: str = "political") -> None:
    png = await render_png(db, game_chat, title, focus, mode)
    await bot.send_photo(target_chat, BufferedInputFile(png, filename="map.png"), caption=caption[:1000] or None)


async def finish_turn(bot: Bot, db: Database, gm: GameMaster, settings: Settings, chat_id: int) -> None:
    lock = chat_lock(chat_id)
    if lock.locked():
        return
    async with lock:
        game = await db.get_game(chat_id)
        if not game or game["status"] != "active":
            return
        label = turn_label(game["turn"], settings.start_year)
        status = await bot.send_message(chat_id, f"⏳ Генералы планируют операции, ведущий подводит итоги: <b>{label}</b>…")
        try:
            outcome = await resolve_turn(db, gm, chat_id)
        except AIError as e:
            await status.edit_text(f"⚠️ Не удалось завершить ход: {escape(str(e))}\nПопробуйте /endturn ещё раз.")
            return
        except Exception:
            log.exception("turn resolution failed")
            await status.edit_text("⚠️ Внутренняя ошибка при подсчёте хода. Попробуйте /endturn ещё раз.")
            return

        parts = [f"📰 <b>Итоги: {label}</b>", f"<b>{escape(outcome.headline)}</b>", "", escape(outcome.world_news)]
        if outcome.world_event:
            parts += ["", f"🌐 <b>Главное событие:</b> {escape(outcome.world_event)}"]
        if outcome.war_lines:
            parts += ["", "<b>⚔️ Сводка с фронтов:</b>", *(f"• {escape(x)}" for x in outcome.war_lines)]
        if outcome.support_lines:
            parts += ["", "<b>📦 Поддержка стран:</b>", *(f"• {escape(x)}" for x in outcome.support_lines)]
        if outcome.event_lines:
            parts += ["", "<b>🌍 Мировые события:</b>", *(f"• {escape(x)}" for x in outcome.event_lines)]
        if outcome.public_lines:
            parts += ["", "<b>Страны игроков:</b>", *(f"• {escape(x)}" for x in outcome.public_lines)]
        for name in outcome.collapses:
            parts.append(f"\n🔥 <b>{escape(name)}: правительство пало!</b> Массовые протесты привели к смене власти.")
        for c in outcome.eliminated:
            parts.append(f"\n🏳️ <b>{escape(c['name'])} разгромлена и прекратила существование.</b>")
        await status.delete()
        await send_long(bot, chat_id, "\n".join(parts))

        if outcome.npc_messages:
            voices = ["💬 <b>Голоса мира</b>", ""]
            voices += [f"{c['flag']} <b>{escape(c['name'])}:</b> {escape(text)}" for c, text in outcome.npc_messages]
            await send_long(bot, chat_id, "\n".join(voices))

        new_label = turn_label(game["turn"] + 1, settings.start_year)
        try:
            await send_map(bot, db, chat_id, chat_id, f"Мир: {new_label}", caption="🗺 Карта мира после хода. Подробнее: /map <i>страна</i>")
        except Exception:
            log.exception("map render failed")

        players = await db.list_players(chat_id)
        await send_long(bot, chat_id, ranking(players))

        unreachable = []
        for c in players:
            report = outcome.private_reports.get(c["id"])
            if report is None:
                continue
            text = (
                f"🔒 <b>Секретный доклад · {c['flag']} {escape(c['name'])}</b>\n<i>{label}</i>\n\n"
                f"{escape(report)}\n\n<b>Изменения показателей:</b>\n{stat_changes(outcome.changes[c['id']])}"
            )
            if not await send_dm(bot, c["user_id"], text):
                unreachable.append(escape(c["player_name"] or c["name"]))
        for c in outcome.eliminated:
            if c["user_id"]:
                await send_dm(bot, c["user_id"], "🏳️ Ваша страна разгромлена. Вы можете взять другую: /take <i>страна</i>")
        if unreachable:
            await bot.send_message(chat_id, "📭 Не смог доставить секретные доклады: " + ", ".join(unreachable)
                                   + ". Напишите мне в личку /start.")
        await bot.send_message(chat_id, f"▶️ Начался новый ход: <b>{new_label}</b>. Отдавайте приказы!")
