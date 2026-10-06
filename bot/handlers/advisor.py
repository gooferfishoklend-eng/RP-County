import time
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.ai import AIError, GameMaster
from bot.config import Settings
from bot.db import Database
from bot.game import world_snapshot
from bot.handlers.common import is_private, player_context, send_dm

router = Router()

_last_call: dict[int, float] = {}


@router.message(Command("advisor"))
async def cmd_advisor(message: Message, command: CommandObject, bot: Bot, db: Database, gm: GameMaster, settings: Settings):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    question = (command.args or "").strip() or "Какие главные угрозы и возможности у моей страны прямо сейчас?"
    if len(question) > 1000:
        await message.answer("Вопрос слишком длинный — максимум 1000 символов.")
        return

    user_id = message.from_user.id
    wait_left = settings.advisor_cooldown_sec - (time.monotonic() - _last_call.get(user_id, 0))
    if wait_left > 0:
        await message.answer(f"Советник занят, подождите {int(wait_left) + 1} сек.")
        return
    _last_call[user_id] = time.monotonic()

    if not is_private(message):
        await message.answer("🧠 Советник готовит закрытый доклад — ответ придёт в личку.")
    try:
        world = await world_snapshot(db, game["chat_id"])
        world["my_actions_this_turn"] = [
            {"kind": a["kind"], "target": a["target"], "text": a["text"]}
            for a in await db.list_actions(game["chat_id"], game["turn"], country["id"])
        ]
        answer = await gm.advise(country, world, question)
    except AIError as e:
        await message.answer(f"⚠️ {escape(str(e))}")
        return

    text = f"🧠 <b>Советник · {country['flag']} {escape(country['name'])}</b>\n\n{escape(answer)}"
    if is_private(message):
        await send_dm(bot, user_id, text)
    elif not await send_dm(bot, user_id, text):
        await message.answer("📭 Не могу написать вам в личку — откройте чат со мной и нажмите /start.")
