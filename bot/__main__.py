import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.ai import GameMaster
from bot.config import load_settings
from bot.db import Database
from bot.game import repair_signed_peace
from bot.geo import load_world
from bot.llm import make_backend
from bot.handlers import setup_routers
from bot.handlers.common import heal_migrations

COMMANDS = [
    BotCommand(command="help", description="Правила и команды"),
    BotCommand(command="newgame", description="Начать новую игру (админ)"),
    BotCommand(command="take", description="Взять страну"),
    BotCommand(command="map", description="Карта мира"),
    BotCommand(command="me", description="Моя страна"),
    BotCommand(command="world", description="Рейтинг держав"),
    BotCommand(command="reform", description="Внутренняя реформа"),
    BotCommand(command="secret", description="Тайный приказ (в ЛС)"),
    BotCommand(command="foreign", description="Внешняя политика"),
    BotCommand(command="say", description="Обратиться к стране"),
    BotCommand(command="propose", description="Предложить договор"),
    BotCommand(command="peace", description="Мирный договор"),
    BotCommand(command="conference", description="Созвать мирную конференцию"),
    BotCommand(command="alliance", description="Союзы: создать, вступить, пригласить"),
    BotCommand(command="alliances", description="Все союзы мира"),
    BotCommand(command="draft", description="Составить договор (в зале конференции)"),
    BotCommand(command="transfer", description="Передать провинции"),
    BotCommand(command="demand", description="Потребовать провинции"),
    BotCommand(command="aid", description="Помощь стране"),
    BotCommand(command="support", description="Поддержать страну (деньги, оружие, войска)"),
    BotCommand(command="talk", description="Тайные переговоры (в ЛС)"),
    BotCommand(command="export", description="Вся история игры файлом"),
    BotCommand(command="sanction", description="Ввести санкции"),
    BotCommand(command="war", description="Объявить войну"),
    BotCommand(command="generals", description="Мои генералы"),
    BotCommand(command="command", description="Приказ генералу"),
    BotCommand(command="events", description="Мировые события"),
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

    logging.info("loading world map…")
    await asyncio.to_thread(load_world)

    db = Database(settings.db_path)
    await db.connect()
    if db.migrated_from == 3:
        for chat_id, a, b in await repair_signed_peace(db):
            logging.info("ended war %s-%s in %s: peace treaty was signed before the update", a, b, chat_id)
    backend = make_backend(settings.ai_provider.lower(), settings.ai_model, settings.anthropic_api_key,
                           settings.openrouter_api_key)
    gm = GameMaster(backend)
    logging.info("AI provider: %s, model: %s", settings.ai_provider, settings.ai_model)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, gm=gm, settings=settings)
    dp.include_router(setup_routers())

    await bot.set_my_commands(COMMANDS)
    for old_id, new_id in await heal_migrations(bot, db):
        logging.info("recovered game from upgraded group %s -> %s", old_id, new_id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
