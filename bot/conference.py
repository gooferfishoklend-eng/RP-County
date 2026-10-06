"""Peace conferences: context for the AI secretariat, treaty validation and execution."""

import re
from html import escape

from bot.db import Database
from bot.game import country_brief, fix_capital
from bot.geo import load_world
from bot.war import apply_peace

MAX_TRANSCRIPT_CHARS = 60_000
MAX_PROVINCES = 200
MAX_RELATION_DELTA = 30


async def participants(db: Database, conference_id: int) -> list[dict]:
    out = []
    for m in await db.conference_members(conference_id):
        c = await db.get_country(m["country_id"])
        if c and c["alive"]:
            out.append({**c, "member": m})
    return out


async def transcript(db: Database, conference_id: int) -> list[dict]:
    lines = [{"speaker": r["speaker"], "country_id": r["country_id"], "text": r["text"]}
             for r in await db.conference_log(conference_id) if r["kind"] in ("player", "npc")]
    total, kept = 0, []
    for line in reversed(lines):
        total += len(line["text"]) + len(line["speaker"])
        if total > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(line)
    return list(reversed(kept))


async def province_options(db: Database, chat_id: int, ids: set[int], talk: list[dict]) -> list[dict]:
    """Provinces the treaty may plausibly move: occupied land, borders between parties, ones named in the talks."""
    world = load_world()
    owner, core = await db.cell_owners(chat_id)
    mentioned_ids = {int(x) for line in talk for x in re.findall(r"\b\d{1,4}\b", line["text"])}
    talk_text = " ".join(line["text"].casefold() for line in talk)
    picked: dict[int, str] = {}
    for cell, o in owner.items():
        if o not in ids:
            continue
        c = world.cells[cell]
        if cell in mentioned_ids or (c.city and c.city.casefold() in talk_text):
            picked[cell] = "mentioned"
        elif core[cell] in ids and core[cell] != o:
            picked[cell] = "occupied"
        elif any(owner.get(n) in ids and owner.get(n) != o for n in c.neighbors):
            picked.setdefault(cell, "border")
    order = {"mentioned": 0, "occupied": 1, "border": 2}
    cells = sorted(picked, key=lambda cell: (order[picked[cell]], -world.cells[cell].city_pop))[:MAX_PROVINCES]
    return [{"cell_id": cell, "label": world.cells[cell].label, "owner_id": owner[cell], "core_id": core[cell],
             "why": picked[cell]} for cell in cells]


async def conference_context(db: Database, conf: dict) -> dict:
    people = await participants(db, conf["id"])
    ids = {p["id"] for p in people}
    talk = await transcript(db, conf["id"])
    world = load_world()
    wars = [{"a_id": a, "b_id": b} for a, b in await db.wars(conf["chat_id"]) if a in ids and b in ids]
    return {
        "topic": conf["topic"],
        "participants": [
            {**country_brief(p), "leader": p["player_name"],
             "capital": world.cells[p["capital_cell"]].label if p["capital_cell"] is not None else None}
            for p in people
        ],
        "wars_between_participants": wars,
        "provinces": await province_options(db, conf["chat_id"], ids, talk),
        "transcript": talk,
    }


def clean_terms(terms: dict, ids: set[int], owner: dict[int, int]) -> dict:
    """Drop anything that references non-participants or provinces the giver doesn't own."""
    ok_pair = lambda a, b: a in ids and b in ids and a != b  # noqa: E731
    seen_cells = set()
    transfers = []
    for t in terms.get("province_transfers", []):
        cell = t["cell_id"]
        if ok_pair(t["from_id"], t["to_id"]) and owner.get(cell) == t["from_id"] and cell not in seen_cells:
            seen_cells.add(cell)
            transfers.append(t)
    return {
        **terms,
        "peace": [p for p in terms.get("peace", []) if ok_pair(p["a_id"], p["b_id"])],
        "province_transfers": transfers,
        "payments": [p for p in terms.get("payments", []) if ok_pair(p["from_id"], p["to_id"]) and p["amount_bn"] > 0],
        "alliances": [p for p in terms.get("alliances", []) if ok_pair(p["a_id"], p["b_id"])],
        "relation_changes": [p for p in terms.get("relation_changes", []) if ok_pair(p["a_id"], p["b_id"])],
        "npc_positions": [p for p in terms.get("npc_positions", []) if p["country_id"] in ids],
    }


def render_terms(terms: dict, names: dict[int, str]) -> str:
    world = load_world()
    n = lambda cid: escape(names.get(cid, "?"))  # noqa: E731
    lines = [f"📜 <b>{escape(terms['title'])}</b>", "", escape(terms["summary"])]
    if terms["peace"]:
        lines += ["", "<b>🕊 Мир:</b>"] + [
            f"• {n(p['a_id'])} — {n(p['b_id'])}: " + ("по линии фронта" if p["mode"] == "front" else "возврат к довоенным границам")
            for p in terms["peace"]]
    if terms["province_transfers"]:
        lines += ["", "<b>🗺 Территории:</b>"] + [
            f"• {t['cell_id']} «{escape(world.cells[t['cell_id']].label)}»: {n(t['from_id'])} → {n(t['to_id'])}"
            for t in terms["province_transfers"]]
    if terms["payments"]:
        lines += ["", "<b>💰 Выплаты:</b>"] + [
            f"• {n(p['from_id'])} → {n(p['to_id'])}: {p['amount_bn']:g} млрд $ ({escape(p['purpose'])})" for p in terms["payments"]]
    if terms["alliances"]:
        lines += ["", "<b>🤝 Союзы:</b>"] + [f"• {n(p['a_id'])} + {n(p['b_id'])}" for p in terms["alliances"]]
    if terms["clauses"]:
        lines += ["", "<b>📌 Прочие условия:</b>"] + [f"{i}. {escape(c)}" for i, c in enumerate(terms["clauses"], 1)]
    if terms["unresolved"]:
        lines += ["", "<b>❓ Не согласовано:</b>"] + [f"• {escape(u)}" for u in terms["unresolved"]]
    for pos in terms["npc_positions"]:
        mark = "✅ готова подписать" if pos["accepts"] else "⛔️ не подпишет"
        lines.append(f"\n🤖 {n(pos['country_id'])} — {mark}: <i>{escape(pos['statement'])}</i>")
    return "\n".join(lines)


async def execute_terms(db: Database, conf: dict, terms: dict) -> list[str]:
    world = load_world()
    chat_id = conf["chat_id"]
    game = await db.get_game(chat_id)
    people = {p["id"]: p for p in await participants(db, conf["id"])}
    ids = set(people)
    owner, core = await db.cell_owners(chat_id)
    terms = clean_terms(terms, ids, owner)
    name = lambda cid: people[cid]["name"]  # noqa: E731
    effects = []

    for p in terms["peace"]:
        a, b = p["a_id"], p["b_id"]
        rel = await db.get_relation(chat_id, a, b)
        moved = 0
        for cell, new_owner, new_core in apply_peace(owner, core, a, b, p["mode"]):
            await db.set_cell_owner(chat_id, cell, new_owner, new_core)
            owner[cell], core[cell] = new_owner, new_core
            moved += 1
        await db.change_relation(chat_id, a, b, 10, "peace")
        for g in await db.list_generals(chat_id):
            if (g["country_id"], g["target_id"]) in ((a, b), (b, a)):
                await db.update_general(g["id"], target_id=None)
        was = "война окончена" if rel["status"] == "war" else "мир подтверждён"
        effects.append(f"🕊 {name(a)} — {name(b)}: {was}" + (f", провинций перешло: {moved}" if moved else ""))

    for t in terms["province_transfers"]:
        cell = t["cell_id"]
        if owner.get(cell) == t["from_id"]:
            await db.set_cell_owner(chat_id, cell, t["to_id"], t["to_id"])
            owner[cell], core[cell] = t["to_id"], t["to_id"]
            effects.append(f"🗺 {world.cells[cell].label}: {name(t['from_id'])} → {name(t['to_id'])}")

    for p in terms["payments"]:
        payer, payee = await db.get_country(p["from_id"]), await db.get_country(p["to_id"])
        cap = max(0.0, payer["budget"]) + payer["gdp"] * 0.05
        amount = round(min(float(p["amount_bn"]), cap), 1)
        if amount <= 0:
            continue
        await db.update_country_stats(payer["id"], {"budget": round(payer["budget"] - amount, 1)})
        await db.update_country_stats(payee["id"], {"budget": round(payee["budget"] + amount, 1)})
        effects.append(f"💰 {payer['name']} → {payee['name']}: {amount:g} млрд $ ({p['purpose']})")

    for p in terms["alliances"]:
        await db.change_relation(chat_id, p["a_id"], p["b_id"], 20, "alliance")
        effects.append(f"🤝 Союз: {name(p['a_id'])} + {name(p['b_id'])}")

    for p in terms["relation_changes"]:
        delta = max(-MAX_RELATION_DELTA, min(MAX_RELATION_DELTA, int(p["delta"])))
        await db.change_relation(chat_id, p["a_id"], p["b_id"], delta)

    for cid in ids:
        c = await db.get_country(cid)
        if c["alive"] and not await fix_capital(db, world, chat_id, c, owner, core):
            effects.append(f"🏳️ {c['name']} прекратила существование как государство.")

    record = terms["title"] + ". " + terms["summary"]
    if terms["clauses"]:
        record += " Условия: " + "; ".join(terms["clauses"])
    for cid in ids:
        others = ", ".join(name(o) for o in ids if o != cid)
        await db.add_action(chat_id, game["turn"], cid, "treaty", record[:2000], others)
    await db.update_conference(conf["id"], status="signed", draft=terms)
    return effects

