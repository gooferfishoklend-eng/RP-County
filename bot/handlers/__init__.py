from aiogram import Router

from bot.handlers import actions, advisor, country, diplomacy, game_admin


def setup_routers() -> Router:
    root = Router()
    root.include_routers(game_admin.router, country.router, actions.router, diplomacy.router, advisor.router)
    return root
