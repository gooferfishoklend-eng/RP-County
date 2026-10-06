import asyncio
import logging
import random
from collections import Counter
from dataclasses import dataclass, field

from bot.ai import AIError, GameMaster
from bot.db import Database
from bot.geo import World, load_world
from bot.mapdraw import MapView
from bot.seed import initial_cells, initial_countries
from bot.stats import STAT_KEYS, apply_deltas, power_index
from bot.war import apply_peace, army_power, attack_targets, max_attacks, resolve_operations

log = logging.getLogger(__name__)

ACTION_KINDS = {
    "reform": "внутренняя реформа",
    "secret": "ТАЙНОЕ действие",
    "foreign": "внешняя политика",
    "war": "объявление войны",
    "sanction": "санкции",
    "aid": "помощь другой стране",
    "treaty": "подписанный договор",
    "npc_deal": "договор со страной-НИП",
}

COLLAPSE_STABILITY = 5
MAX_GENERALS = 4
TOP_NPC = 8

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
    war_lines: list[str] = field(default_factory=list)
    private_reports: dict[int, str] = field(default_factory=dict)
    changes: dict[int, dict] = field(default_factory=dict)
    collapses: list[str] = field(default_factory=list)
    eliminated: list[dict] = field(default_factory=list)
    npc_messages: list[tuple[dict, str]] = field(default_factory=list)
    event_lines: list[str] = field(default_factory=list)


async def new_game(db: Database, chat_id: int, title: str, user_id: int) -> None:
    world = await asyncio.to_thread(load_world)
    await db.create_game(chat_id, title, user_id, initial_countries(world), initial_cells(world))


def country_brief(c: dict) -> dict:
    return {
        "id": c["id"],
        "name": c["name"],
        "player": bool(c["user_id"]),
        "government": c["government"],
        "power_index": power_index(c),
        **{k: c[k] for k in STAT_KEYS},
    }


def neighbors_of(world: World, owner: dict[int, int], country_id: int) -> Counter:
    border = Counter()
    for cell, o in owner.items():
        if o != country_id:
            continue
        for n in world.cells[cell].neighbors:
            other = owner.get(n)
            if other is not None and other != country_id:
                border[other] += 1
    return border


def territory(world: World, owner: dict[int, int], core: dict[int, int], c: dict, by_id: dict[int, dict]) -> dict:
    cid = c["id"]
    owned = [cell for cell, o in owner.items() if o == cid]
    occupied_by = Counter(owner[cell] for cell, k in core.items() if k == cid and owner[cell] != cid)
    occupying = Counter(core[cell] for cell in owned if core[cell] != cid)
    cap = c.get("capital_cell")
    return {
        "provinces": len(owned),
        "core_provinces": sum(1 for k in core.values() if k == cid),
        "occupied_by": {by_id[k]["name"]: n for k, n in occupied_by.items() if k in by_id},
        "occupying": {by_id[k]["name"]: n for k, n in occupying.items() if k in by_id},
        "capital": world.cells[cap].label if cap is not None else None,
        "neighbors": [by_id[k]["name"] for k, _ in neighbors_of(world, owner, cid).most_common(10) if k in by_id],
    }


async def world_snapshot(db: Database, chat_id: int, extra_ids: set[int] | None = None) -> dict:
    world = load_world()
    game = await db.get_game(chat_id)
    countries = [c for c in await db.list_countries(chat_id) if c["alive"]]
    by_id = {c["id"]: c for c in countries}
    owner, core = await db.cell_owners(chat_id)
    wars = await db.wars(chat_id)
    events = await db.active_events(chat_id)

    relevant = {c["id"] for c in countries if c["user_id"]}
    for cid in list(relevant):
        relevant.update(k for k, _ in neighbors_of(world, owner, cid).most_common(6))
    for a, b in wars:
        relevant.update((a, b))
    for ev in events:
        relevant.update(ev["affected"][:10])
    relevant.update(extra_ids or set())
    npcs = sorted((c for c in countries if not c["user_id"]), key=power_index, reverse=True)
    relevant.update(c["id"] for c in npcs[:TOP_NPC])
    relevant &= set(by_id)

    out_countries = []
    for cid in sorted(relevant):
        c = by_id[cid]
        brief = country_brief(c)
        if c["user_id"]:
            brief["territory"] = territory(world, owner, core, c, by_id)
        out_countries.append(brief)

    relations = [
        {"a": by_id[r["a_id"]]["name"], "b": by_id[r["b_id"]]["name"], "a_id": r["a_id"], "b_id": r["b_id"],
         "value": r["value"], "status": r["status"]}
        for r in await db.list_relations(chat_id)
        if r["a_id"] in by_id and r["b_id"] in by_id and (r["a_id"] in relevant or r["b_id"] in relevant)
    ]
    history = [{"turn": h["turn"], "headline": h["headline"], "event": h["event"]} for h in await db.recent_chronicle(chat_id)]
    agreements = [
        {**a, "parties": [by_id[pid]["name"] for pid in a["parties"] if pid in by_id]}
        for a in await db.signed_agreements(chat_id)
    ]
    return {
        "turn": game["turn"] if game else 1,
        "countries": out_countries,
        "relations": relations,
        "wars": [{"a": by_id[a]["name"], "b": by_id[b]["name"], "a_id": a, "b_id": b} for a, b in wars
                 if a in by_id and b in by_id],
        "active_events": [
            {"event_id": e["id"], "name": e["name"], "kind": e["kind"], "description": e["description"],
             "severity": e["severity"], "affected_country_ids": e["affected"], "since_turn": e["started_turn"]}
            for e in events
        ],
        "recent_history": history,
        "agreements_in_force": agreements,
    }


async def ensure_general(db: Database, gm: GameMaster, chat_id: int, country: dict, enemy_id: int) -> dict | None:
    generals = await db.list_generals(chat_id, country["id"])
    if any(g["target_id"] == enemy_id for g in generals):
        return None
    free = next((g for g in generals if g["target_id"] is None), None)
    if free:
        await db.update_general(free["id"], target_id=enemy_id)
        return {**free, "target_id": enemy_id}
    if len(generals) >= MAX_GENERALS:
        return None
    return await recruit_general(db, gm, chat_id, country, enemy_id)


async def recruit_general(db: Database, gm: GameMaster, chat_id: int, country: dict, enemy_id: int | None) -> dict:
    generals = await db.list_generals(chat_id, country["id"])
    try:
        data = await gm.create_general(country, [g["name"] for g in generals])
    except AIError:
        data = {"name": f"Генерал штаба №{len(generals) + 1}", "rank": "генерал", "trait": "уравновешенный"}
    skill = random.randint(3, 7)
    gid = await db.add_general(chat_id, country["id"], data["name"][:60], data["rank"][:40], data["trait"][:60], skill,
                               country.get("capital_cell"))
    if enemy_id is not None:
        await db.update_general(gid, target_id=enemy_id)
    return await db.get_general(gid)


async def declare_war(db: Database, gm: GameMaster, chat_id: int, attacker: dict, defender: dict) -> list[dict]:
    await db.change_relation(chat_id, attacker["id"], defender["id"], -40, "war")
    created = []
    for side, enemy in ((attacker, defender), (defender, attacker)):
        g = await ensure_general(db, gm, chat_id, side, enemy["id"])
        if g:
            created.append(g)
    return created


def war_briefs(world: World, owner: dict, core: dict, by_id: dict, wars: list, generals: list, actions: list) -> list[dict]:
    briefs = []
    for a, b in wars:
        for side, enemy in ((a, b), (b, a)):
            me, foe = by_id[side], by_id[enemy]
            mine = [g for g in generals if g["country_id"] == side and g["target_id"] in (enemy, None)]
            orders = [x["text"] for x in actions if x["country_id"] == side and x["kind"] == "war" and x["target"]]
            briefs.append({
                "country_id": side,
                "country": me["name"],
                "enemy_id": enemy,
                "enemy": foe["name"],
                "is_player": bool(me["user_id"]),
                "our_power": round(army_power(me), 1),
                "enemy_power": round(army_power(foe), 1),
                "max_attacks": max_attacks(me),
                "generals": [{"general_id": g["id"], "name": g["name"], "rank": g["rank"], "trait": g["trait"],
                              "skill": g["skill"], "directive": g["directive"]} for g in mine],
                "leader_orders": orders[-3:],
                "targets": attack_targets(world, owner, core, me, foe),
                "own_front": [{"cell_id": t["cell_id"], "label": t["label"], "is_capital": t["is_enemy_capital"]}
                              for t in attack_targets(world, owner, core, foe, me)],
            })
    return briefs


async def run_war_phase(db: Database, gm: GameMaster, chat_id: int, actions: list[dict], rng: random.Random) -> tuple[dict, list[str], dict[int, list[str]], list[dict]]:
    world = load_world()
    wars = await db.wars(chat_id)
    countries = {c["id"]: c for c in await db.list_countries(chat_id) if c["alive"]}
    wars = [(a, b) for a, b in wars if a in countries and b in countries]
    if not wars:
        return {}, [], {}, []
    owner, core = await db.cell_owners(chat_id)
    generals = await db.list_generals(chat_id)
    briefs = war_briefs(world, owner, core, countries, wars, generals, actions)
    try:
        plans = await gm.plan_operations(briefs)
    except AIError:
        log.warning("generals planning failed, armies hold positions")
        plans = []

    before = dict(owner)
    result = resolve_operations(world, owner, core, countries, wars, generals, plans, rng)

    for cell, new in owner.items():
        if before.get(cell) != new:
            await db.set_cell_owner(chat_id, cell, new)
    for cid, changes in result.stat_changes.items():
        c = countries[cid]
        new_stats = apply_deltas(c, changes)
        await db.update_country_stats(cid, new_stats)
        countries[cid] = {**c, **new_stats}
    for cid, cell in result.capital_moves.items():
        if cell is not None:
            await db.set_capital(cid, cell)
    eliminated = []
    for cid in result.eliminated:
        eliminated.append(countries[cid])
        await db.eliminate(cid)
        for other in countries:
            if other != cid:
                rel = await db.get_relation(chat_id, cid, other)
                if rel["status"] == "war":
                    await db.change_relation(chat_id, cid, other, 0, "peace")
    gen_by_id = {g["id"]: g for g in generals}
    for gid, upd in result.general_updates.items():
        await db.update_general(gid, **upd)
    for gid in result.fallen_generals:
        await db.update_general(gid, alive=0)

    name = lambda cid: countries[cid]["name"]  # noqa: E731
    battles = [
        {"attacker": name(b.attacker_id), "defender": name(b.defender_id), "province": b.label, "via": b.via,
         "general": gen_by_id[b.general_id]["name"] if b.general_id in gen_by_id else None,
         "chance": b.probability, "captured": b.success}
        for b in result.battles
    ]
    lines = []
    for a, b in wars:
        for side, enemy in ((a, b), (b, a)):
            won = [x.label for x in result.battles if x.attacker_id == side and x.defender_id == enemy and x.success]
            failed = sum(1 for x in result.battles if x.attacker_id == side and x.defender_id == enemy and not x.success)
            pocket = [world.cells[cell].label for cell, new, old in result.encircled if new == side and old == enemy]
            if won or failed or pocket:
                part = f"{countries[side]['flag']} {name(side)} → {countries[enemy]['flag']} {name(enemy)}: "
                bits = []
                if won:
                    bits.append("взято: " + ", ".join(won))
                if pocket:
                    bits.append("окружены и сдались: " + ", ".join(pocket))
                if failed:
                    bits.append(f"отбито атак: {failed}")
                lines.append(part + "; ".join(bits))
    for cid in set(result.capital_lost):
        lines.append(f"🏛 Пала столица страны {name(cid)}! Правительство эвакуировано.")
    for gid in result.fallen_generals:
        g = gen_by_id[gid]
        lines.append(f"🎖 {g['rank']} {g['name']} ({name(g['country_id'])}) погиб или попал в плен.")

    war_results = {
        "battles": battles,
        "encircled": [{"province": world.cells[c].label, "to": name(n), "from": name(o)} for c, n, o in result.encircled],
        "capitals_fallen": [name(c) for c in set(result.capital_lost)],
        "eliminated": [name(c) for c in result.eliminated],
        "generals_fallen": [gen_by_id[g]["name"] for g in result.fallen_generals],
    }
    return war_results, lines, result.reports, eliminated


async def resolve_turn(db: Database, gm: GameMaster, chat_id: int, rng: random.Random | None = None) -> TurnOutcome:
    rng = rng or random.Random()
    game = await db.get_game(chat_id)
    turn = game["turn"]
    start_stats = {c["id"]: c for c in await db.list_countries(chat_id)}
    actions = await db.list_actions(chat_id, turn)

    war_results, war_lines, general_reports, eliminated = await run_war_phase(db, gm, chat_id, actions, rng)

    countries = [c for c in await db.list_countries(chat_id) if c["alive"]]
    by_id = {c["id"]: c for c in countries}
    action_ids = {a["country_id"] for a in actions}
    world_data = await world_snapshot(db, chat_id, extra_ids=action_ids)
    world_data["actions"] = [
        {"country_id": a["country_id"], "country": by_id[a["country_id"]]["name"], "kind": a["kind"],
         "kind_ru": ACTION_KINDS.get(a["kind"], a["kind"]), "target": a["target"], "text": a["text"]}
        for a in actions if a["country_id"] in by_id
    ]
    world_data["war_results"] = war_results

    resolutions = []
    for r in await db.open_resolutions(chat_id):
        votes = await db.tally(r["id"])
        passed = votes["yes"] > votes["no"]
        resolutions.append({"author": by_id.get(r["author_id"], {}).get("name"), "text": r["text"],
                            "votes": votes, "passed": passed})
        await db.close_resolution(r["id"], "passed" if passed else "failed")
    world_data["un_resolutions"] = resolutions

    result = await gm.resolve_turn(world_data, npc_chat=bool(game["npc_chat"]), random_events=bool(game["random_events"]))

    outcome = TurnOutcome(turn=turn, headline=result["headline"], world_news=result["world_news"],
                          world_event=result["world_event"], war_lines=war_lines)
    outcome.eliminated = eliminated

    for entry in result["countries"]:
        country = by_id.get(entry["country_id"])
        if not country:
            continue
        new_stats = apply_deltas(country, entry["deltas"])
        if new_stats["stability"] <= COLLAPSE_STABILITY:
            new_stats.update(stability=30, approval=40, budget=round(new_stats["budget"] * 0.5, 1),
                             gdp=round(new_stats["gdp"] * 0.9, 1))
            outcome.collapses.append(country["name"])
        await db.update_country_stats(country["id"], new_stats)
        by_id[country["id"]] = {**country, **new_stats}
        if country["user_id"]:
            line = f"{country['flag']} {country['name']}: {entry['public_summary']}"
            if entry["secret_exposed"]:
                line += " 🕵️ Раскрыта тайная операция!"
            outcome.public_lines.append(line)
            report = entry["private_report"]
            if general_reports.get(country["id"]):
                report += "\n\n🎖 Доклады генералов:\n" + "\n".join(f"— {r}" for r in general_reports[country["id"]])
            outcome.private_reports[country["id"]] = report

    for c in countries:
        if c["user_id"]:
            old = start_stats[c["id"]]
            outcome.changes[c["id"]] = {k: (old[k], by_id[c["id"]][k]) for k in STAT_KEYS}

    for rel in result["relations"]:
        a, b = rel["a_id"], rel["b_id"]
        if a in by_id and b in by_id and a != b:
            await db.change_relation(chat_id, a, b, int(rel["delta"]), rel["status"])

    active = {e["id"]: e for e in await db.active_events(chat_id)}
    for ev in result["events"]:
        affected = [cid for cid in ev["affected_country_ids"] if cid in by_id]
        severity = max(0, min(100, int(ev["severity"])))
        if ev["event_id"] in active:
            await db.update_event(ev["event_id"], turn, ev["description"], severity, affected, ev["status"])
            mark = "✅ завершилось" if ev["status"] == "ended" else f"тяжесть {severity}/100"
            outcome.event_lines.append(f"{EVENT_ICONS.get(ev['kind'], '🌐')} {ev['name']} — {mark}")
        elif ev["event_id"] == 0 and ev["status"] == "active" and game["random_events"]:
            await db.add_event(chat_id, turn, ev["name"], ev["kind"], ev["description"], severity, affected)
            outcome.event_lines.append(f"🆕 {EVENT_ICONS.get(ev['kind'], '🌐')} {ev['name']}: {ev['description']}")

    if game["npc_chat"]:
        for msg in result["npc_messages"][:6]:
            c = by_id.get(msg["country_id"])
            if c and not c["user_id"]:
                outcome.npc_messages.append((c, msg["text"]))

    await db.add_chronicle(chat_id, turn, outcome.headline, outcome.world_news, outcome.world_event)
    await db.advance_turn(chat_id)
    return outcome


EVENT_ICONS = {"pandemic": "🦠", "disaster": "🌋", "economic": "📉", "climate": "🌡", "tech": "💡", "political": "🏛", "other": "🌐"}


async def fix_capital(db: Database, world: World, chat_id: int, country: dict, owner: dict[int, int],
                      core: dict[int, int]) -> bool:
    """Move the capital if it was lost; eliminate the country if it has no land. Returns False if eliminated."""
    mine = [cell for cell, o in owner.items() if o == country["id"]]
    if not mine:
        await db.eliminate(country["id"])
        return False
    if owner.get(country["capital_cell"]) != country["id"]:
        best = max(mine, key=lambda c: (core.get(c) == country["id"], world.cells[c].city_pop, world.cells[c].area_km2))
        await db.set_capital(country["id"], best)
    return True


async def apply_treaty(db: Database, treaty: dict) -> list[str]:
    """Execute a treaty signed by both sides. Returns human-readable effects."""
    world = load_world()
    chat_id, a, b = treaty["chat_id"], treaty["a_id"], treaty["b_id"]
    game = await db.get_game(chat_id)
    ca, cb = await db.get_country(a), await db.get_country(b)
    effects = []
    owner, core = await db.cell_owners(chat_id)

    if treaty["kind"] == "peace":
        updates = apply_peace(owner, core, a, b, treaty["peace_mode"] or "front")
        for cell, new_owner, new_core in updates:
            await db.set_cell_owner(chat_id, cell, new_owner, new_core)
            owner[cell], core[cell] = new_owner, new_core
        await db.change_relation(chat_id, a, b, 10, "peace")
        for g in await db.list_generals(chat_id):
            if (g["country_id"], g["target_id"]) in ((a, b), (b, a)):
                await db.update_general(g["id"], target_id=None)
        mode = "по линии фронта" if treaty["peace_mode"] == "front" else "с возвратом к довоенным границам"
        effects.append(f"🕊 Война окончена, мир {mode}. Провинций перешло: {len(updates)}.")
    elif treaty["kind"] == "transfer":
        moved = []
        for cell, from_id, to_id in treaty["transfers"]:
            if owner.get(cell) == from_id:
                await db.set_cell_owner(chat_id, cell, to_id, to_id)
                owner[cell], core[cell] = to_id, to_id
                moved.append(world.cells[cell].label)
        effects.append("🗺 Переданы провинции: " + (", ".join(moved) if moved else "нет (уже сменили владельца)"))
        await db.change_relation(chat_id, a, b, 5)
    else:
        await db.change_relation(chat_id, a, b, 15)
        effects.append("🤝 Договор вступил в силу.")

    for c in (ca, cb):
        if c and not await fix_capital(db, world, chat_id, await db.get_country(c["id"]), owner, core):
            effects.append(f"🏳️ {c['name']} прекратила существование как государство.")
    for cid, partner in ((a, cb["name"]), (b, ca["name"])):
        await db.add_action(chat_id, game["turn"], cid, "treaty", treaty["text"], partner)
    return effects


async def map_view(db: Database, chat_id: int, title: str) -> MapView:
    owner, core = await db.cell_owners(chat_id)
    countries = {c["id"]: c for c in await db.list_countries(chat_id) if c["alive"]}
    return MapView(
        owner=owner,
        core=core,
        countries=countries,
        wars=await db.wars(chat_id),
        generals=await db.list_generals(chat_id),
        events=[{"name": e["name"], "affected": e["affected"]} for e in await db.active_events(chat_id)],
        title=title,
    )
