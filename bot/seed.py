"""Starting stats for every country, derived from Natural Earth GDP / population / income group."""

import math

from bot.geo import CountryInfo, World
from bot.stats import clamp

# income group 1 (high, OECD) .. 5 (low)
TECH = {1: 75, 2: 62, 3: 48, 4: 33, 5: 18}
CORRUPTION = {1: 22, 2: 40, 3: 50, 4: 60, 5: 70}
STABILITY = {1: 72, 2: 62, 3: 55, 4: 48, 5: 38}

MILITARY = {
    "USA": 97, "CHN": 90, "RUS": 88, "IND": 76, "FRA": 70, "GBR": 70, "KOR": 64, "JPN": 62, "TUR": 63, "ISR": 66,
    "PAK": 60, "IRN": 57, "PRK": 55, "DEU": 56, "ITA": 52, "UKR": 58, "EGY": 52, "BRA": 52, "SAU": 52, "POL": 54,
    "IDN": 48, "AUS": 50, "TWN": 52, "VNM": 48, "ESP": 48, "CAN": 46, "GRC": 44, "DZA": 44, "SWE": 45,
}
INFLUENCE = {
    "USA": 95, "CHN": 86, "RUS": 72, "GBR": 68, "FRA": 68, "DEU": 66, "JPN": 60, "IND": 62, "SAU": 52, "TUR": 52,
    "BRA": 50, "ITA": 50, "CAN": 48, "KOR": 48, "ISR": 48, "IRN": 46, "AUS": 45, "ARE": 46, "QAT": 42, "UKR": 42,
}
GOVERNMENT = {
    "USA": ("Федеративная президентская республика", "Президент"),
    "RUS": ("Федеративная президентская республика", "Президент"),
    "CHN": ("Однопартийная социалистическая республика", "Председатель КНР"),
    "GBR": ("Конституционная монархия", "Премьер-министр"),
    "FRA": ("Президентско-парламентская республика", "Президент"),
    "DEU": ("Федеративная парламентская республика", "Канцлер"),
    "JPN": ("Конституционная монархия", "Премьер-министр"),
    "SAU": ("Абсолютная монархия", "Король"),
    "IRN": ("Исламская республика", "Верховный лидер"),
    "PRK": ("Однопартийная диктатура", "Председатель"),
}


def base_stats(info: CountryInfo) -> dict:
    gdp = max(0.5, info.gdp_md / 1000)
    pop = max(0.01, info.pop / 1e6)
    lg = math.log10(gdp + 1)
    military = MILITARY.get(info.code, clamp(round(14 * lg - 6 + 4 * math.log10(pop + 1)), 3, 60))
    influence = INFLUENCE.get(info.code, clamp(round(13 * lg - 8), 2, 55))
    gov, leader = GOVERNMENT.get(info.code, ("Республика", "Президент"))
    return {
        "code": info.code,
        "name": info.name,
        "flag": info.flag,
        "government": gov,
        "leader_title": leader,
        "description": "",
        "gdp": round(gdp, 1),
        "budget": round(gdp * 0.03, 1),
        "population": round(pop, 2),
        "stability": STABILITY.get(info.income, 50),
        "approval": 55,
        "military": int(military),
        "tech": TECH.get(info.income, 40),
        "corruption": CORRUPTION.get(info.income, 50),
        "influence": int(influence),
    }


REAL_ALLIANCES = [
    {
        "name": "НАТО",
        "leader": "USA",
        "charter": "Североатлантический договор: вооружённое нападение на одного из членов считается нападением на "
                   "всех (статья 5). Коллективная оборона, общее командование, расходы на оборону от 2% ВВП.",
        "members": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "TUR", "POL", "ESP", "NLD", "BEL", "NOR", "DNK", "PRT",
                    "GRC", "CZE", "HUN", "ROU", "BGR", "SVK", "SVN", "HRV", "ALB", "MNE", "MKD", "EST", "LVA", "LTU",
                    "LUX", "ISL", "FIN", "SWE"],
    },
    {
        "name": "ОДКБ",
        "leader": "RUS",
        "charter": "Договор о коллективной безопасности: агрессия против одного из участников — агрессия против всех, "
                   "коллективные силы оперативного реагирования. Армения заморозила участие в 2024 году.",
        "members": ["RUS", "BLR", "KAZ", "KGZ", "TJK"],
    },
]


def initial_countries(world: World) -> list[dict]:
    rows = []
    for code, info in sorted(world.countries.items()):
        if code not in world.cells_by_country:
            continue
        rows.append({**base_stats(info), "capital_cell": world.capital_cell[code]})
    return rows


def initial_cells(world: World) -> list[tuple[int, str]]:
    return [(c.id, c.code) for c in world.cells]
