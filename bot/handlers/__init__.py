from aiogram import Router

from bot.handlers import actions, admin, advisor, alliance, conference, country, diplomacy, game_admin, military


def setup_routers() -> Router:
    root = Router()
    root.include_routers(game_admin.router, admin.router, country.router, actions.router, diplomacy.router,
                         military.router, alliance.router, advisor.router, conference.router)
    return root
