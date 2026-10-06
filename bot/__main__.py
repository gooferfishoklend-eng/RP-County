import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.ai import GameMaster
from bot.config import load_settings
from bot.db import Database
from bot.handlers import setup_routers

COMMANDS = [
    BotCommand(command="help", description="Правила и команды"),
    BotCommand(command="newgame", description="Начать новую игру (админ)"),
    BotCommand(command="take", description="Взять страну"),
    BotCommand(command="me", description="Моя страна"),
    BotCommand(command="world", description="Рейтинг держав"),
    BotCommand(command="reform", description="Внутренняя реформа"),
    BotCommand(command="secret", description="Тайный приказ (в ЛС)"),
    BotCommand(command="foreign", description="Внешняя политика"),
    BotCommand(command="propose", description="Предложить договор"),
    BotCommand(command="sanction", description="Ввести санкции"),
    BotCommand(command="war", description="Объявить войну"),
    BotCommand(command="un", description="Резолюция ООН"),
    BotCommand(command="advisor", description="Спросить советника"),
    BotCommand(command="actions", description="Мои приказы на ход"),
    BotCommand(command="ready", description="Завершить ход"),
    BotCommand(command="turn", description="Состояние хода"),
    BotCommand(command="news", description="Последние новости"),
    BotCommand(command="relations", description="Мои отношения"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    db = Database(settings.db_path)
    await db.connect()
    gm = GameMaster(settings.ai_model, settings.anthropic_api_key)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, gm=gm, settings=settings)
    dp.include_router(setup_routers())

    await bot.set_my_commands(COMMANDS)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
