"""Support between countries: money, weapons, troops, intelligence and humanitarian aid with real effects."""

from bot.db import Database
from bot.stats import clamp

KINDS = {
    "money": "💰 деньги",
    "weapons": "🔫 оружие",
    "troops": "🪖 войска",
    "intel": "🛰 разведданные",
    "humanitarian": "🚑 гуманитарная помощь",
}

KEYWORDS = [
    ("intel", ("развед", "intel", "шпион", "данные")),
    ("troops", ("войск", "армия", "армию", "солдат", "добровол", "контингент", "troops")),
    ("weapons", ("оруж", "вооруж", "техник", "снаряд", "ракет", "танк", "weapon")),
    ("humanitarian", ("гуман", "медиц", "вакцин", "еда", "продовол", "врач", "лекар", "спасат")),
    ("money", ("деньг", "финанс", "кредит", "заём", "заем", "транш", "доллар", "money", "млрд")),
]

TROOPS_TURNS = 2
INTEL_TURNS = 1
MAX_POWER_BONUS = 0.6


def parse_kind(text: str) -> str | None:
    t = text.casefold()
    for kind, words in KEYWORDS:
        if kind == t or any(w in t for w in words):
            return kind
    return None


def needs_amount(kind: str) -> bool:
    return kind in ("money", "weapons", "humanitarian")


async def give_support(db: Database, chat_id: int, turn: int, giver: dict, receiver: dict, kind: str, amount: float,
                       note: str = "", secret: bool = False) -> tuple[bool, str]:
    """Apply the support immediately. Returns (ok, human-readable effect or error)."""
    if giver["id"] == receiver["id"]:
        return False, "Нельзя поддерживать самих себя."
    amount = round(max(0.0, float(amount or 0)), 1)
    if needs_amount(kind):
        if amount <= 0:
            return False, "Укажите сумму в млрд $."
        cap = max(0.0, giver["budget"]) + giver["gdp"] * 0.05
        if amount > cap:
            return False, f"В казне не хватает средств: можно выделить не больше {cap:.1f} млрд $."

    g, r = dict(giver), dict(receiver)
    effect = ""
    turns_left = 0
    if kind == "money":
        g["budget"] -= amount
        r["budget"] += amount
        effect = f"казна {r['name']} +{amount:g} млрд $"
    elif kind == "weapons":
        g["budget"] -= amount
        boost = int(clamp(round(amount / max(0.5, r["gdp"] * 0.004)), 1, 8))
        r["military"] = int(clamp(r["military"] + boost, 0, 100))
        effect = f"армия {r['name']} +{boost}"
    elif kind == "humanitarian":
        g["budget"] -= amount
        boost = int(clamp(round(amount / max(0.3, r["gdp"] * 0.002)), 1, 6))
        r["stability"] = int(clamp(r["stability"] + boost, 0, 100))
        r["approval"] = int(clamp(r["approval"] + 2, 0, 100))
        effect = f"стабильность {r['name']} +{boost}, рейтинг +2"
    elif kind == "troops":
        g["military"] = int(clamp(g["military"] - 3, 0, 100))
        turns_left = TROOPS_TURNS
        effect = f"экспедиционный корпус усиливает армию {r['name']} на фронте на {TROOPS_TURNS} хода"
    elif kind == "intel":
        turns_left = INTEL_TURNS
        effect = f"разведданные усиливают удары {r['name']} в следующем ходу"
    else:
        return False, "Неизвестный вид поддержки."

    for c in (g, r):
        c["budget"] = round(c["budget"], 1)
    await db.update_country_stats(g["id"], {"budget": g["budget"], "military": g["military"]})
    await db.update_country_stats(r["id"], {"budget": r["budget"], "military": r["military"],
                                            "stability": r["stability"], "approval": r["approval"]})
    await db.add_support(chat_id, turn, g["id"], r["id"], kind, amount, note[:500], secret, turns_left)
    await db.change_relation(chat_id, g["id"], r["id"], 10 if kind == "troops" else 5)
    if kind == "troops":
        for a, b in await db.wars(chat_id):
            if r["id"] in (a, b):
                enemy = b if a == r["id"] else a
                if enemy != g["id"]:
                    await db.change_relation(chat_id, g["id"], enemy, -20)
    return True, effect


def support_bonus(supports: list[dict], giver_power: dict[int, float]) -> dict[int, float]:
    """War-power multiplier bonus each country gets from active troops/intel support."""
    bonus: dict[int, float] = {}
    for s in supports:
        if s["kind"] == "troops":
            add = 0.15 + giver_power.get(s["from_id"], 0) / 400
        elif s["kind"] == "intel":
            add = 0.1
        else:
            continue
        bonus[s["to_id"]] = min(MAX_POWER_BONUS, bonus.get(s["to_id"], 0) + add)
    return bonus
