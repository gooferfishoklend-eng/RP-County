from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import EVENT_ICONS, new_game
from bot.handlers.common import finish_turn, game_chat_id, get_game_healed, is_admin, is_private, send_map
from bot.handlers.conference import register_room
from bot.stats import turn_label
from bot.texts import HELP, ranking

router = Router()


@router.message(F.migrate_to_chat_id)
async def on_migrated_to(message: Message, db: Database):
    await db.migrate_chat(message.chat.id, message.migrate_to_chat_id)


@router.message(F.migrate_from_chat_id)
async def on_migrated_from(message: Message, db: Database):
    await db.migrate_chat(message.migrate_from_chat_id, message.chat.id)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, db: Database):
    if not is_private(message) and command.args and command.args.startswith("room_"):
        try:
            game_chat = int(command.args.removeprefix("room_"))
        except ValueError:
            return
        await register_room(message, bot, db, game_chat)
        return
    if is_private(message) and command.args and command.args.startswith("play_"):
        try:
            chat_id = int(command.args.removeprefix("play_"))
        except ValueError:
            chat_id = None
        if chat_id and await db.get_country_by_user(chat_id, message.from_user.id):
            await db.set_active_chat(message.from_user.id, chat_id)
            await message.answer(
                "✅ Связь с правительством установлена. Сюда будут приходить секретные доклады.\n"
                "Здесь можно отдавать тайные приказы: /secret <i>текст</i>, командовать генералами: /generals, "
                "и спрашивать советника: /advisor <i>вопрос</i>."
            )
            return
    await message.answer(HELP)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("newgame"), F.chat.type != "private")
async def cmd_newgame(message: Message, bot: Bot, db: Database):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Начать новую игру может только администратор группы.")
        return
    game = await get_game_healed(bot, db, message.chat.id)
    if game and (game["turn"] > 1 or await db.list_players(message.chat.id)):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Да, удалить и начать заново", callback_data="newgame:yes")
        kb.button(text="Отмена", callback_data="newgame:no")
        state = "Уже идёт игра" if game["status"] == "active" else "Здесь есть завершённая игра (её можно продолжить: /resumegame)"
        await message.answer(f"⚠️ {state}. Начать новую? <b>Весь прогресс будет удалён безвозвратно.</b>",
                             reply_markup=kb.as_markup())
        return
    await _start_game(message.chat.id, message.chat.title or "Мир", message.from_user.id, message, bot, db)


@router.callback_query(F.data.startswith("newgame:"))
async def cb_newgame(call: CallbackQuery, bot: Bot, db: Database):
    if not await is_admin(bot, call.message.chat.id, call.from_user.id):
        await call.answer("Только для администраторов", show_alert=True)
        return
    await call.message.delete()
    await call.answer()
    if call.data == "newgame:yes":
        await _start_game(call.message.chat.id, call.message.chat.title or "Мир", call.from_user.id, call.message, bot, db)


async def _start_game(chat_id: int, title: str, user_id: int, message: Message, bot: Bot, db: Database):
    wait = await message.answer("🌍 Создаю мир: 200 стран, 4000 провинций…")
    await new_game(db, chat_id, title, user_id)
    await wait.edit_text(
        "🌍 <b>Новая игра началась!</b>\n\n"
        "Мир — наш, реальный и современный. Каждая страна разбита на провинции. Все страны, которые не взяли игроки, "
        "живут своей жизнью под управлением ИИ.\n\n"
        "Берите страну: /take <i>название</i> (например, <code>/take Франция</code>).\n"
        "Карта: /map · Все команды: /help · Настройки (админы): /settings"
    )
    await send_map(bot, db, chat_id, chat_id, "Политическая карта мира")


@router.message(Command("settings"), F.chat.type != "private")
async def cmd_settings(message: Message, bot: Bot, db: Database):
    game = await db.get_game(message.chat.id)
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Настройки меняют только администраторы.")
        return
    await message.answer("⚙️ <b>Настройки игры</b>", reply_markup=_settings_kb(game))


def _settings_kb(game: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    on = lambda v: "✅ вкл" if v else "⛔️ выкл"  # noqa: E731
    kb.button(text=f"💬 Страны-НИП говорят в чате: {on(game['npc_chat'])}", callback_data="set:npc_chat")
    kb.button(text=f"🎲 Случайные события ИИ: {on(game['random_events'])}", callback_data="set:random_events")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(call: CallbackQuery, bot: Bot, db: Database):
    if not await is_admin(bot, call.message.chat.id, call.from_user.id):
        await call.answer("Только для администраторов", show_alert=True)
        return
    flag = call.data.split(":")[1]
    game = await db.get_game(call.message.chat.id)
    await db.set_game_flag(call.message.chat.id, flag, not game[flag])
    game = await db.get_game(call.message.chat.id)
    await call.message.edit_reply_markup(reply_markup=_settings_kb(game))
    await call.answer("Сохранено")


@router.message(Command("event"), F.chat.type != "private")
async def cmd_event(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Создавать события могут только администраторы.")
        return
    game = await db.get_game(message.chat.id)
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /event <i>описание</i>\nНапример: <code>/event В Юго-Восточной Азии вспышка нового гриппа</code>")
        return
    wait = await message.answer("🌐 Мир реагирует на событие…")
    countries = [{"id": c["id"], "name": c["name"]} for c in await db.list_countries(message.chat.id) if c["alive"]]
    try:
        ev = await gm.create_event(text[:1000], {"countries": countries})
    except AIError as e:
        await wait.edit_text(f"⚠️ {escape(str(e))}")
        return
    ids = {c["id"] for c in countries}
    affected = [cid for cid in ev["affected_country_ids"] if cid in ids]
    severity = max(0, min(100, int(ev["severity"])))
    await db.add_event(message.chat.id, game["turn"], ev["name"], ev["kind"], ev["description"], severity, affected)
    names = [c["name"] for c in countries if c["id"] in affected]
    await wait.edit_text(
        f"{EVENT_ICONS.get(ev['kind'], '🌐')} <b>{escape(ev['name'])}</b> (тяжесть {severity}/100)\n\n"
        f"{escape(ev['description'])}\n\n<b>Затронуты:</b> {escape(', '.join(names) or '—')}\n\n"
        "Страны могут реагировать: /reform, /aid <i>страна</i> | <i>помощь</i>, /foreign."
    )
    await send_map(bot, db, message.chat.id, message.chat.id, ev["name"], mode="events")


@router.message(Command("events"))
async def cmd_events(message: Message, db: Database):
    chat_id = await game_chat_id(message, db)
    events = await db.active_events(chat_id) if chat_id else []
    if not events:
        await message.answer("Сейчас в мире спокойно — активных событий нет.")
        return
    names = {c["id"]: c["name"] for c in await db.list_countries(chat_id)}
    lines = ["🌍 <b>Активные мировые события</b>", ""]
    for e in events:
        affected = ", ".join(names.get(i, "?") for i in e["affected"][:12])
        lines.append(f"{EVENT_ICONS.get(e['kind'], '🌐')} <b>{escape(e['name'])}</b> — тяжесть {e['severity']}/100\n"
                     f"{escape(e['description'][:400])}\n<i>Затронуты: {escape(affected or '—')}</i>\n")
    lines.append("Карта событий: <code>/map события</code>")
    await message.answer("\n".join(lines))


@router.message(Command("endgame"), F.chat.type != "private")
async def cmd_endgame(message: Message, bot: Bot, db: Database):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Завершить игру может только администратор группы.")
        return
    game = await db.get_game(message.chat.id)
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🏁 Да, завершить", callback_data="endgame:yes")
    kb.button(text="Отмена", callback_data="endgame:no")
    await message.answer("Завершить игру и подвести итоги? (Потом её можно будет продолжить командой /resumegame.)",
                         reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("endgame:"))
async def cb_endgame(call: CallbackQuery, bot: Bot, db: Database):
    if not await is_admin(bot, call.message.chat.id, call.from_user.id):
        await call.answer("Только для администраторов", show_alert=True)
        return
    await call.answer()
    if call.data != "endgame:yes":
        await call.message.edit_text("Игра продолжается.")
        return
    game = await db.get_game(call.message.chat.id)
    if not game or game["status"] != "active":
        await call.message.edit_text("Активной игры нет.")
        return
    await db.set_game_status(call.message.chat.id, "ended")
    await call.message.edit_text("🏁 <b>Игра окончена!</b> Итоговый рейтинг:\n\n" + ranking(await db.list_players(call.message.chat.id))
                                 + "\n\nПередумали? /resumegame — продолжить с того же места.")


@router.message(Command("resumegame"), F.chat.type != "private")
async def cmd_resumegame(message: Message, bot: Bot, db: Database, settings: Settings):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Продолжить игру может только администратор группы.")
        return
    game = await get_game_healed(bot, db, message.chat.id)
    if not game:
        await message.answer("В этой группе нет сохранённой игры.")
        return
    if game["status"] == "active":
        await message.answer("Игра и так идёт.")
        return
    await db.set_game_status(message.chat.id, "active")
    await message.answer(f"▶️ <b>Игра продолжается!</b> {turn_label(game['turn'], settings.start_year)}, все страны, карта, "
                         "войны и договоры на месте. Отдавайте приказы!")


@router.message(Command("endturn"), F.chat.type != "private")
async def cmd_endturn(message: Message, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Принудительно завершить ход может только администратор. Остальные — /ready.")
        return
    if not await db.list_players(message.chat.id):
        await message.answer("В игре ещё нет игроков.")
        return
    await finish_turn(bot, db, gm, settings, message.chat.id)


@router.message(Command("turn"))
async def cmd_turn(message: Message, db: Database, settings: Settings):
    chat_id = await game_chat_id(message, db)
    game = await db.get_game(chat_id) if chat_id else None
    if not game or game["status"] != "active":
        await message.answer("Активной игры нет.")
        return
    players = await db.list_players(chat_id)
    actions = await db.list_actions(chat_id, game["turn"])
    counts: dict[int, int] = {}
    for a in actions:
        counts[a["country_id"]] = counts.get(a["country_id"], 0) + 1
    lines = [f"⏱ <b>{turn_label(game['turn'], settings.start_year)}</b> (ход {game['turn']})", ""]
    for c in players:
        mark = "✅" if c["ready_turn"] == game["turn"] else "⌛"
        lines.append(f"{mark} {c['flag']} {escape(c['name'])} — приказов: {counts.get(c['id'], 0)}")
    wars = await db.wars(chat_id)
    if wars:
        names = {c["id"]: c["name"] for c in await db.list_countries(chat_id)}
        lines += ["", "⚔️ <b>Войны:</b>"] + [f"• {escape(names[a])} — {escape(names[b])}" for a, b in wars]
    await message.answer("\n".join(lines))


@router.message(Command("news"))
async def cmd_news(message: Message, db: Database, settings: Settings):
    chat_id = await game_chat_id(message, db)
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
