"""Alliances (blocs): one leader who admits members, shared 'alliance' relations between all members."""

from bot.ai import GameMaster
from bot.db import Database

MAX_NAME = 60


async def describe(db: Database, alliance: dict) -> dict:
    members = await db.alliance_members(alliance["id"])
    countries = [await db.get_country(cid) for cid in members]
    return {
        "id": alliance["id"],
        "name": alliance["name"],
        "charter": alliance["charter"],
        "leader_id": alliance["leader_id"],
        "members": [c for c in countries if c and c["alive"]],
    }


async def find_alliance(db: Database, chat_id: int, query: str, find_country) -> dict | None:
    """By alliance name, or by the name of any member country."""
    q = query.strip().casefold()
    for a in await db.list_alliances(chat_id):
        if a["name"].casefold() == q:
            return a
    country = await find_country(db, chat_id, query)
    return await db.alliance_of(chat_id, country["id"]) if country else None


async def join_blockers(db: Database, chat_id: int, alliance: dict, country: dict) -> str | None:
    current = await db.alliance_of(chat_id, country["id"])
    if current and current["id"] == alliance["id"]:
        return f"{country['name']} уже в этом союзе."
    if current:
        return f"{country['name']} уже состоит в союзе «{current['name']}». Сначала нужно выйти из него."
    for member in await db.alliance_members(alliance["id"]):
        if (await db.get_relation(chat_id, member, country["id"]))["status"] == "war":
            other = await db.get_country(member)
            return f"{country['name']} воюет с участником союза — {other['name']}. Сначала нужен мир."
    return None


async def add_member(db: Database, chat_id: int, alliance: dict, country: dict, turn: int) -> None:
    for member in await db.alliance_members(alliance["id"]):
        await db.change_relation(chat_id, member, country["id"], 15, "alliance")
    await db.add_alliance_member(alliance["id"], country["id"], turn)


async def remove_member(db: Database, chat_id: int, alliance: dict, country_id: int) -> str:
    """Remove a member; pass leadership on or dissolve an empty alliance. Returns a status line."""
    await db.remove_alliance_member(alliance["id"], country_id)
    remaining = await db.alliance_members(alliance["id"])
    for member in remaining:
        rel = await db.get_relation(chat_id, member, country_id)
        if rel["status"] == "alliance":
            await db.change_relation(chat_id, member, country_id, -10, "peace")
    if not remaining:
        await db.update_alliance(alliance["id"], status="dissolved")
        return f"Союз «{alliance['name']}» распущен."
    if alliance["leader_id"] == country_id:
        new_leader = await db.get_country(remaining[0])
        await db.update_alliance(alliance["id"], leader_id=new_leader["id"])
        return f"Новый глава союза — {new_leader['flag']} {new_leader['name']}."
    return ""


async def npc_decides(db: Database, gm: GameMaster, chat_id: int, npc: dict, other: dict, question: str) -> dict:
    """An AI-run country decides on an alliance matter with its memory; the decision is remembered."""
    from bot.game import npc_context

    game = await db.get_game(chat_id)
    answer = await gm.npc_diplomacy(other, npc, question, await npc_context(db, chat_id, npc, other))
    await db.set_notes(npc["id"], answer["notes"])
    trust = max(-15, min(15, int(answer["trust_delta"])))
    if trust:
        await db.change_relation(chat_id, npc["id"], other["id"], trust)
    await db.remember(chat_id, game["turn"], npc["id"], other["id"], "alliance", other["name"], question)
    await db.remember(chat_id, game["turn"], npc["id"], other["id"], "alliance", npc["name"],
                      ("Согласились. " if answer["accepted"] else "Отказали. ") + answer["reply"])
    return answer


async def snapshot(db: Database, chat_id: int) -> list[dict]:
    out = []
    for a in await db.list_alliances(chat_id):
        d = await describe(db, a)
        leader = await db.get_country(a["leader_id"])
        out.append({"name": a["name"], "leader": leader["name"] if leader else None, "charter": a["charter"],
                    "members": [c["name"] for c in d["members"]]})
    return out
