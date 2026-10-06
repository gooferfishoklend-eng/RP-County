import asyncio
from dataclasses import dataclass, field

from bot.ai import GameMaster
from bot.db import Database
from bot.stats import STAT_KEYS, apply_deltas, power_index

ACTION_KINDS = {
    "reform": "внутренняя реформа",
    "secret": "ТАЙНОЕ действие",
    "foreign": "внешняя политика",
    "war": "объявление войны",
    "sanction": "санкции",
    "treaty": "заключённый договор",
    "npc_deal": "договор со страной-НИП",
}

COLLAPSE_STABILITY = 5

_locks: dict[int, asyncio.Lock] = {}


def chat_lock(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


@dataclass
class TurnOutcome:
    turn: int
    headline: str
    world_news: str
    world_event: str
    public_lines: list[str] = field(default_factory=list)
    private_reports: dict[int, str] = field(default_factory=dict)
    changes: dict[int, dict] = field(default_factory=dict)
    collapses: list[str] = field(default_factory=list)


def country_public(c: dict) -> dict:
    return {
        "id": c["id"],
        "name": c["name"],
        "government": c["government"],
        "power_index": power_index(c),
        **{k: c[k] for k in STAT_KEYS},
    }


async def world_snapshot(db: Database, chat_id: int) -> dict:
    game = await db.get_game(chat_id)
    countries = await db.list_countries(chat_id)
    names = {c["id"]: c["name"] for c in countries}
    relations = [
        {"a": names.get(r["a_id"]), "b": names.get(r["b_id"]), "a_id": r["a_id"], "b_id": r["b_id"],
         "value": r["value"], "status": r["status"]}
        for r in await db.list_relations(chat_id)
    ]
    history = [
        {"turn": h["turn"], "headline": h["headline"], "event": h["event"]}
        for h in await db.recent_chronicle(chat_id)
    ]
    return {
        "turn": game["turn"] if game else 1,
        "countries": [country_public(c) for c in countries],
        "relations": relations,
        "recent_history": history,
    }


async def resolve_turn(db: Database, gm: GameMaster, chat_id: int) -> TurnOutcome:
    game = await db.get_game(chat_id)
    turn = game["turn"]
    countries = await db.list_countries(chat_id)
    by_id = {c["id"]: c for c in countries}

    world = await world_snapshot(db, chat_id)
    actions = await db.list_actions(chat_id, turn)
    world["actions"] = [
        {"country_id": a["country_id"], "country": by_id[a["country_id"]]["name"], "kind": a["kind"],
         "kind_ru": ACTION_KINDS.get(a["kind"], a["kind"]), "target": a["target"], "text": a["text"]}
        for a in actions if a["country_id"] in by_id
    ]

    resolutions = []
    for r in await db.open_resolutions(chat_id):
        votes = await db.tally(r["id"])
        passed = votes["yes"] > votes["no"]
        resolutions.append({"author": by_id.get(r["author_id"], {}).get("name"), "text": r["text"],
                            "votes": votes, "passed": passed})
        await db.close_resolution(r["id"], "passed" if passed else "failed")
    world["un_resolutions"] = resolutions

    result = await gm.resolve_turn(world)

    outcome = TurnOutcome(
        turn=turn,
        headline=result["headline"],
        world_news=result["world_news"],
        world_event=result["world_event"],
    )

    for entry in result["countries"]:
        country = by_id.get(entry["country_id"])
        if not country:
            continue
        new_stats = apply_deltas(country, entry["deltas"])
        if new_stats["stability"] <= COLLAPSE_STABILITY:
            new_stats["stability"] = 30
            new_stats["approval"] = 40
            new_stats["budget"] = round(new_stats["budget"] * 0.5, 1)
            new_stats["gdp"] = round(new_stats["gdp"] * 0.9, 1)
            outcome.collapses.append(country["name"])
        await db.update_country_stats(country["id"], new_stats)
        outcome.changes[country["id"]] = {k: (country[k], new_stats[k]) for k in STAT_KEYS}
        line = f"{country['flag']} {country['name']}: {entry['public_summary']}"
        if entry["secret_exposed"]:
            line += " 🕵️ Раскрыта тайная операция!"
        outcome.public_lines.append(line)
        outcome.private_reports[country["id"]] = entry["private_report"]

    for rel in result["relations"]:
        a, b = rel["a_id"], rel["b_id"]
        if a in by_id and b in by_id and a != b:
            await db.change_relation(chat_id, a, b, int(rel["delta"]), rel["status"])

    await db.add_chronicle(chat_id, turn, outcome.headline, outcome.world_news, outcome.world_event)
    await db.advance_turn(chat_id)
    return outcome
