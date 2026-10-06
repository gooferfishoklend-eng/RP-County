from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import GameMaster
from bot.config import Settings
from bot.db import Database
from bot.handlers.common import finish_turn, is_admin, is_private
from bot.stats import turn_label
from bot.texts import HELP, ranking

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database):
    if is_private(message) and command.args and command.args.startswith("play_"):
        try:
            chat_id = int(command.args.removeprefix("play_"))
        except ValueError:
            chat_id = None
        if chat_id and await db.get_country_by_user(chat_id, message.from_user.id):
            await db.set_active_chat(message.from_user.id, chat_id)
            await message.answer(
                "✅ Связь с правительством установлена. Сюда будут приходить секретные доклады.\n"
                "Здесь можно отдавать тайные приказы: /secret <i>текст</i>, и спрашивать советника: /advisor <i>вопрос</i>."
            )
            return
    await message.answer(HELP)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("newgame"), ~F.chat.type.in_({"private"}))
async def cmd_newgame(message: Message, bot: Bot, db: Database):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Начать новую игру может только администратор группы.")
        return
    game = await db.get_game(message.chat.id)
    if game and game["status"] == "active" and await db.list_countries(message.chat.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Да, начать заново", callback_data="newgame:yes")
        kb.button(text="Отмена", callback_data="newgame:no")
        await message.answer("⚠️ Уже идёт игра. Начать новую? Весь прогресс будет удалён.", reply_markup=kb.as_markup())
        return
    await _start_game(message.chat.id, message.chat.title or "Мир", message.from_user.id, message, db)


@router.callback_query(F.data.startswith("newgame:"))
async def cb_newgame(call: CallbackQuery, bot: Bot, db: Database):
    if not await is_admin(bot, call.message.chat.id, call.from_user.id):
        await call.answer("Только для администраторов", show_alert=True)
        return
    await call.message.delete()
    if call.data == "newgame:yes":
        await _start_game(call.message.chat.id, call.message.chat.title or "Мир", call.from_user.id, call.message, db)
    await call.answer()


async def _start_game(chat_id: int, title: str, user_id: int, message: Message, db: Database):
    await db.create_game(chat_id, title, user_id)
    await message.answer(
        "🌍 <b>Новая игра началась!</b>\n\n"
        "Мир — наш, реальный и современный. Каждый игрок выбирает страну и становится её лидером.\n"
        "Берите страну: /take <i>название</i> (например, <code>/take Франция</code>).\n"
        "Все команды: /help"
    )


@router.message(Command("endgame"), ~F.chat.type.in_({"private"}))
async def cmd_endgame(message: Message, bot: Bot, db: Database):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Завершить игру может только администратор группы.")
        return
    game = await db.get_game(message.chat.id)
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    await db.set_game_status(message.chat.id, "ended")
    countries = await db.list_countries(message.chat.id)
    await message.answer("🏁 <b>Игра окончена!</b> Итоговый рейтинг:\n\n" + ranking(countries))


@router.message(Command("endturn"), ~F.chat.type.in_({"private"}))
async def cmd_endturn(message: Message, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Принудительно завершить ход может только администратор. Остальные — /ready.")
        return
    if not await db.list_countries(message.chat.id):
        await message.answer("В игре ещё нет стран.")
        return
    await finish_turn(bot, db, gm, settings, message.chat.id)


@router.message(Command("turn"))
async def cmd_turn(message: Message, db: Database, settings: Settings):
    chat_id = message.chat.id if not is_private(message) else await db.get_active_chat(message.from_user.id)
    game = await db.get_game(chat_id) if chat_id else None
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    countries = await db.list_countries(chat_id)
    actions = await db.list_actions(chat_id, game["turn"])
    counts: dict[int, int] = {}
    for a in actions:
        counts[a["country_id"]] = counts.get(a["country_id"], 0) + 1
    lines = [f"⏱ <b>{turn_label(game['turn'], settings.start_year)}</b> (ход {game['turn']})", ""]
    for c in countries:
        mark = "✅" if c["ready_turn"] == game["turn"] else "⌛"
        lines.append(f"{mark} {c['flag']} {escape(c['name'])} — приказов: {counts.get(c['id'], 0)}")
    await message.answer("\n".join(lines))


@router.message(Command("news"))
async def cmd_news(message: Message, db: Database, settings: Settings):
    chat_id = message.chat.id if not is_private(message) else await db.get_active_chat(message.from_user.id)
    history = await db.recent_chronicle(chat_id, limit=1) if chat_id else []
    if not history:
        await message.answer("Новостей пока нет — ещё не прошло ни одного хода.")
        return
    h = history[-1]
    await message.answer(
        f"📰 <b>{turn_label(h['turn'], settings.start_year)}</b>\n<b>{escape(h['headline'])}</b>\n\n"
        f"{escape(h['news'])[:3500]}\n\n🌐 {escape(h['event'] or '')}"
    )


@router.message(Command("play"), F.chat.type == "private")
async def cmd_play(message: Message, db: Database):
    games = await db.user_games(message.from_user.id)
    if not games:
        await message.answer("У вас нет стран ни в одной игре. Возьмите страну в группе: /take <i>название</i>.")
        return
    kb = InlineKeyboardBuilder()
    for g in games:
        kb.button(text=f"{g['flag']} {g['name']} · {g['game_title']}", callback_data=f"play:{g['chat_id']}")
    kb.adjust(1)
    await message.answer("Выберите игру, которой будете управлять из личных сообщений:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("play:"))
async def cb_play(call: CallbackQuery, db: Database):
    chat_id = int(call.data.split(":")[1])
    country = await db.get_country_by_user(chat_id, call.from_user.id)
    if not country:
        await call.answer("Страна не найдена", show_alert=True)
        return
    await db.set_active_chat(call.from_user.id, chat_id)
    await call.message.edit_text(f"✅ Активная игра: {country['flag']} <b>{escape(country['name'])}</b>")
    await call.answer()
