import logging
from html import escape

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import chat_lock, resolve_turn
from bot.stats import turn_label
from bot.texts import ranking, split_message, stat_changes

log = logging.getLogger(__name__)


def is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def player_context(message: Message, db: Database) -> tuple[dict, dict] | None:
    """Find the active game and the sender's country, replying with a hint if missing."""
    user_id = message.from_user.id
    if is_private(message):
        chat_id = await db.get_active_chat(user_id)
        if chat_id is None:
            await message.answer("Вы ещё не играете. Возьмите страну в группе командой /take, затем выберите игру через /play.")
            return None
    else:
        chat_id = message.chat.id

    game = await db.get_game(chat_id)
    if not game or game["status"] != "active":
        await message.answer("В этой группе нет активной игры. Админ может начать её командой /newgame.")
        return None
    country = await db.get_country_by_user(chat_id, user_id)
    if not country:
        await message.answer("У вас нет страны в этой игре. Возьмите её командой /take <i>название</i>.")
        return None
    return game, country


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


async def finish_turn(bot: Bot, db: Database, gm: GameMaster, settings: Settings, chat_id: int) -> None:
    lock = chat_lock(chat_id)
    if lock.locked():
        return
    async with lock:
        game = await db.get_game(chat_id)
        if not game or game["status"] != "active":
            return
        label = turn_label(game["turn"], settings.start_year)
        status = await bot.send_message(chat_id, f"⏳ Ведущий подводит итоги: <b>{label}</b>…")
        try:
            outcome = await resolve_turn(db, gm, chat_id)
        except AIError as e:
            await status.edit_text(f"⚠️ Не удалось завершить ход: {escape(str(e))}\nПопробуйте /endturn ещё раз.")
            return
        except Exception:
            log.exception("turn resolution failed")
            await status.edit_text("⚠️ Внутренняя ошибка при подсчёте хода. Попробуйте /endturn ещё раз.")
            return

        parts = [
            f"📰 <b>Итоги: {label}</b>",
            f"<b>{escape(outcome.headline)}</b>",
            "",
            escape(outcome.world_news),
            "",
            f"🌐 <b>Мировое событие:</b> {escape(outcome.world_event)}",
            "",
            "<b>Страны:</b>",
            *(f"• {escape(line)}" for line in outcome.public_lines),
        ]
        for name in outcome.collapses:
            parts.append(f"\n🔥 <b>{escape(name)}: правительство пало!</b> Массовые протесты привели к смене власти.")
        await status.delete()
        await send_long(bot, chat_id, "\n".join(parts))

        countries = await db.list_countries(chat_id)
        await send_long(bot, chat_id, ranking(countries))

        unreachable = []
        for c in countries:
            report = outcome.private_reports.get(c["id"])
            if report is None:
                continue
            text = (
                f"🔒 <b>Секретный доклад · {c['flag']} {escape(c['name'])}</b>\n<i>{label}</i>\n\n"
                f"{escape(report)}\n\n<b>Изменения показателей:</b>\n{stat_changes(outcome.changes[c['id']])}"
            )
            if not await send_dm(bot, c["user_id"], text):
                unreachable.append(escape(c["player_name"] or c["name"]))
        if unreachable:
            await bot.send_message(
                chat_id,
                "📭 Не смог доставить секретные доклады: " + ", ".join(unreachable)
                + ". Напишите мне в личку /start.",
            )
        new_label = turn_label(game["turn"] + 1, settings.start_year)
        await bot.send_message(chat_id, f"▶️ Начался новый ход: <b>{new_label}</b>. Отдавайте приказы!")
