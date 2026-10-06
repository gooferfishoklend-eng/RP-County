import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Stat:
    key: str
    title: str
    emoji: str
    unit: str = ""
    bounded: bool = True


STATS: list[Stat] = [
    Stat("gdp", "ВВП", "💰", "млрд $", bounded=False),
    Stat("budget", "Казна", "🏦", "млрд $", bounded=False),
    Stat("population", "Население", "👥", "млн", bounded=False),
    Stat("stability", "Стабильность", "⚖️"),
    Stat("approval", "Рейтинг власти", "📣"),
    Stat("military", "Армия", "🪖"),
    Stat("tech", "Технологии", "🔬"),
    Stat("corruption", "Коррупция", "🕳"),
    Stat("influence", "Влияние", "🌐"),
]

STAT_KEYS = [s.key for s in STATS]
BOUNDED_KEYS = [s.key for s in STATS if s.bounded]

MAX_BOUNDED_DELTA = 25


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def apply_deltas(country: dict, deltas: dict) -> dict:
    """Return new stat values after applying AI deltas with sanity limits."""
    new = {}
    for key in BOUNDED_KEYS:
        d = clamp(float(deltas.get(key, 0)), -MAX_BOUNDED_DELTA, MAX_BOUNDED_DELTA)
        new[key] = int(round(clamp(country[key] + d, 0, 100)))

    gdp = country["gdp"]
    d_gdp = clamp(float(deltas.get("gdp", 0)), -0.2 * gdp, 0.2 * gdp)
    new["gdp"] = round(max(1.0, gdp + d_gdp), 1)

    d_budget = clamp(float(deltas.get("budget", 0)), -0.5 * gdp, 0.5 * gdp)
    new["budget"] = round(country["budget"] + d_budget, 1)

    pop = country["population"]
    d_pop = clamp(float(deltas.get("population", 0)), -0.05 * pop, 0.05 * pop)
    new["population"] = round(max(0.01, pop + d_pop), 2)
    return new


def power_index(c: dict) -> int:
    economy = clamp(20 * math.log10(max(c["gdp"], 1)), 0, 100)
    score = (
        economy * 0.25
        + c["military"] * 0.25
        + c["tech"] * 0.15
        + c["influence"] * 0.2
        + c["stability"] * 0.15
        - c["corruption"] * 0.1
    )
    return max(0, round(score))


def turn_label(turn: int, start_year: int) -> str:
    year = start_year + (turn - 1) // 4
    quarter = (turn - 1) % 4 + 1
    return f"{quarter}-й квартал {year} г."
