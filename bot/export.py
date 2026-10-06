"""Plain-text export of the whole game history."""

import json

from bot.db import Database
from bot.game import ACTION_KINDS
from bot.stats import turn_label
from bot.support import KINDS as SUPPORT_KINDS
from bot.texts import STATUS_RU


async def build_history(db: Database, chat_id: int, start_year: int, viewer_id: int | None = None) -> str:
    """Everything public about the game; the viewer's own secret orders and supports are included for them only."""
    game = await db.get_game(chat_id)
    countries = {c["id"]: c for c in await db.list_countries(chat_id)}
    name = lambda cid: countries[cid]["name"] if cid in countries else "?"  # noqa: E731
    label = lambda t: turn_label(t, start_year)  # noqa: E731
    owner, _ = await db.cell_owners(chat_id)
    lines = [f"ИСТОРИЯ ИГРЫ «{game['title']}» — сейчас {label(game['turn'])} (ход {game['turn']})", "=" * 70, ""]

    if game.get("history"):
        lines += ["ЛЕТОПИСЬ (краткая память ведущего)", "-" * 70, game["history"], ""]

    lines += ["ИГРОКИ", "-" * 70]
    for c in countries.values():
        if c["user_id"] and c["alive"]:
            provinces = sum(1 for o in owner.values() if o == c["id"])
            lines.append(f"{c['flag']} {c['name']} — {c['player_name']} | {c['government']} | провинций: {provinces}")
            lines.append(f"   ВВП {c['gdp']:g} млрд $, казна {c['budget']:g}, население {c['population']:g} млн, "
                         f"стабильность {c['stability']}, рейтинг {c['approval']}, армия {c['military']}, "
                         f"технологии {c['tech']}, коррупция {c['corruption']}, влияние {c['influence']}")

    lines += ["", "ОТНОШЕНИЯ", "-" * 70]
    for r in sorted(await db.list_relations(chat_id), key=lambda r: (r["status"] != "war", r["value"])):
        lines.append(f"{name(r['a_id'])} — {name(r['b_id'])}: {STATUS_RU.get(r['status'], r['status'])} ({r['value']:+d})")

    lines += ["", "ДОГОВОРЫ И КОНФЕРЕНЦИИ", "-" * 70]
    for t in await db._all("SELECT * FROM treaties WHERE chat_id = ? ORDER BY id", (chat_id,)):
        lines.append(f"[{label(t['turn'])}] {name(t['a_id'])} ⟷ {name(t['b_id'])} ({t['kind']}, {t['status']}): {t['text']}")
    for k in await db._all("SELECT * FROM conferences WHERE chat_id = ? ORDER BY id", (chat_id,)):
        draft = json.loads(k["draft"] or "{}")
        lines.append(f"[{label(k['turn'])}] Конференция №{k['id']} «{k['topic']}» ({k['status']}): "
                     f"{draft.get('title', '')} — {draft.get('summary', '')}")
        lines += [f"     • {c}" for c in draft.get("clauses", [])]

    alliances = await db.list_alliances(chat_id)
    if alliances:
        lines += ["", "СОЮЗЫ", "-" * 70]
        for a in alliances:
            members = [name(cid) for cid in await db.alliance_members(a["id"])]
            lines.append(f"«{a['name']}» — глава {name(a['leader_id'])}; участники: {', '.join(members)}"
                         + (f"; устав: {a['charter']}" if a["charter"] else ""))

    supports = [s for s in await db.all_supports(chat_id) if not s["secret"] or s["from_id"] == viewer_id or s["to_id"] == viewer_id]
    if supports:
        lines += ["", "ПОДДЕРЖКА МЕЖДУ СТРАНАМИ", "-" * 70]
        for s in supports:
            lines.append(f"[{label(s['turn'])}] {name(s['from_id'])} → {name(s['to_id'])}: {SUPPORT_KINDS.get(s['kind'], s['kind'])}"
                         + (f" {s['amount']:g} млрд $" if s["amount"] else "") + (" (тайно)" if s["secret"] else "")
                         + (f" — {s['note']}" if s["note"] else ""))

    lines += ["", "МИРОВЫЕ СОБЫТИЯ", "-" * 70]
    for e in await db._all("SELECT * FROM events WHERE chat_id = ? ORDER BY id", (chat_id,)):
        lines.append(f"[с {label(e['started_turn'])}] {e['name']} ({e['status']}, тяжесть {e['severity']}): {e['description']}")

    news = {h["turn"]: h for h in await db._all("SELECT * FROM chronicle WHERE chat_id = ? ORDER BY turn", (chat_id,))}
    actions = await db._all("SELECT * FROM actions WHERE chat_id = ? ORDER BY turn, id", (chat_id,))
    lines += ["", "ХРОНИКА ПО ХОДАМ", "=" * 70]
    for turn in range(1, game["turn"] + 1):
        lines += ["", f"### {label(turn)} (ход {turn})"]
        for a in actions:
            if a["turn"] != turn or (a["kind"] == "secret" and a["country_id"] != viewer_id):
                continue
            target = f" → {a['target']}" if a["target"] else ""
            lines.append(f"  {countries.get(a['country_id'], {}).get('flag', '')} {name(a['country_id'])} — "
                         f"{ACTION_KINDS.get(a['kind'], a['kind'])}{target}: {a['text']}")
        if turn in news:
            h = news[turn]
            lines += ["", f"  ИТОГИ: {h['headline']}", "", h["news"], "", f"  Событие: {h['event'] or '—'}"]
    return "\n".join(lines) + "\n"
