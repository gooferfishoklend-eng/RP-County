from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.ai import GameMaster
from bot.db import Database
from bot.game import MAX_GENERALS, recruit_general
from bot.geo import load_world
from bot.handlers.actions import MAX_TEXT, parse_parts
from bot.handlers.common import find_country, is_private, player_context

router = Router()


def hire_cost(country: dict) -> float:
    return round(0.5 + country["gdp"] * 0.0005, 1)


async def _my_general(message: Message, db: Database, country: dict, raw: str) -> dict | None:
    digits = raw.strip().lstrip("#")
    if not digits.isdigit():
        await message.answer("Укажите номер генерала, например <code>#12</code>. Список: /generals")
        return None
    g = await db.get_general(int(digits))
    if not g or not g["alive"] or g["country_id"] != country["id"]:
        await message.answer("У вас нет такого генерала. Список: /generals")
        return None
    return g


@router.message(Command("generals"))
async def cmd_generals(message: Message, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    world = load_world()
    names = {c["id"]: c["name"] for c in await db.list_countries(game["chat_id"])}
    generals = await db.list_generals(game["chat_id"], country["id"])
    lines = [f"🎖 <b>Генералы {country['flag']} {escape(country['name'])}</b>", ""]
    for g in generals:
        front = names.get(g["target_id"], "резерв") if g["target_id"] else "резерв"
        pos = world.cells[g["position_cell"]].label if g["position_cell"] is not None else "—"
        lines.append(
            f"<b>#{g['id']}</b> {escape(g['rank'])} {escape(g['name'])}\n"
            f"   {escape(g['trait'])} · навык {g['skill']}/10 · опыт {g['xp']}\n"
            f"   Фронт: {escape(front)} · Ставка: {escape(pos)}\n"
            f"   Приказ: <i>{escape(g['directive'] or 'действовать по обстановке')}</i>"
        )
    if not generals:
        lines.append("Генералов нет. Они назначаются автоматически при объявлении войны, или нанять: /hire")
    lines += ["", "Приказ: <code>/command #id | приказ</code> (или <code>/command #id | страна | приказ</code>)",
              f"Нанять: /hire ({hire_cost(country)} млрд $, максимум {MAX_GENERALS}) · Отставка: /fire #id"]
    await message.answer("\n".join(lines))


@router.message(Command("hire"))
async def cmd_hire(message: Message, db: Database, gm: GameMaster):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    if len(await db.list_generals(game["chat_id"], country["id"])) >= MAX_GENERALS:
        await message.answer(f"Максимум {MAX_GENERALS} генерала. Отправьте кого-нибудь в отставку: /fire #id")
        return
    cost = hire_cost(country)
    if country["budget"] < cost:
        await message.answer(f"В казне не хватает средств: нужно {cost} млрд $.")
        return
    await db.update_country_stats(country["id"], {"budget": round(country["budget"] - cost, 1)})
    g = await recruit_general(db, gm, game["chat_id"], country, None)
    await message.answer(f"🎖 Новый генерал на службе: {escape(g['rank'])} {escape(g['name'])} (#{g['id']})\n"
                         f"Характер: {escape(g['trait'])} · навык {g['skill']}/10\nСтоимость: {cost} млрд $\n"
                         f"Назначьте фронт и приказ: <code>/command #{g['id']} | страна | приказ</code>")


@router.message(Command("command"))
async def cmd_command(message: Message, command: CommandObject, bot: Bot, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    game, country = ctx
    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) < 2 or not parts[-1]:
        await message.answer("Формат: <code>/command #id | приказ</code> или <code>/command #id | страна | приказ</code>\n"
                             "Например: <code>/command #3 | наступать на столицу, окружить группировку на юге</code>")
        return
    g = await _my_general(message, db, country, parts[0])
    if not g:
        return
    directive = parts[-1][:MAX_TEXT]
    target_id = g["target_id"]
    if len(parts) >= 3 and parts[1]:
        enemy = await find_country(db, game["chat_id"], parts[1])
        if not enemy or enemy["id"] == country["id"]:
            await message.answer(f"Не нашёл страну «{escape(parts[1])}».")
            return
        if (await db.get_relation(game["chat_id"], country["id"], enemy["id"]))["status"] != "war":
            await message.answer(f"Вы не воюете с {escape(enemy['name'])}. Генерал может готовиться, но в бой пойдёт только на войне.")
        target_id = enemy["id"]
    await db.update_general(g["id"], directive=directive, target_id=target_id)
    reply = f"🫡 {escape(g['rank'])} {escape(g['name'])}: «Есть!»\nПриказ принят: <i>{escape(directive)}</i>"
    await message.answer(reply)
    if not is_private(message):
        return
    await bot.send_message(game["chat_id"], f"📡 Перехвачены переговоры штаба {country['flag']} {escape(country['name'])}: "
                                            "генералы получили новые приказы.")


@router.message(Command("fire"))
async def cmd_fire(message: Message, command: CommandObject, db: Database):
    ctx = await player_context(message, db)
    if not ctx:
        return
    _, country = ctx
    g = await _my_general(message, db, country, command.args or "")
    if g:
        await db.update_general(g["id"], alive=0)
        await message.answer(f"📜 {escape(g['rank'])} {escape(g['name'])} уходит в отставку.")
