"""World geography: countries split into hex provinces ("cells") in the Equal Earth projection."""

import difflib
import hashlib
import json
import math
import pickle
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.strtree import STRtree

DATA = Path(__file__).parent / "data"
CACHE = DATA / "cache"
BUILD_VERSION = 4

EARTH_R = 6371.0
CELL_AREA_KM2 = 45_000
SLIVER_SHARE = 0.12
LAND_GAP_KM = 15
SEA_GAP_KM = 160

_A1, _A2, _A3, _A4 = 1.340264, -0.081106, 0.000893, 0.003796
_M = math.sqrt(3) / 2

ALIASES = {
    "сша": "USA", "америка": "USA", "соединенные штаты": "USA", "штаты": "USA",
    "англия": "GBR", "британия": "GBR", "соединенное королевство": "GBR",
    "беларусь": "BLR", "молдова": "MDA", "киргизстан": "KGZ", "кыргызстан": "KGZ", "туркменистан": "TKM",
    "корея": "KOR", "кндр": "PRK", "северная корея": "PRK", "кнр": "CHN", "китайская народная республика": "CHN",
    "оаэ": "ARE", "эмираты": "ARE", "бирма": "MMR", "персия": "IRN", "чехия": "CZE", "голландия": "NLD",
    "кот-дивуар": "CIV", "конго": "COG", "заир": "COD", "юар": "ZAF", "южная африка": "ZAF",
    "ватикан": "VAT", "македония": "MKD", "свазиленд": "SWZ", "тимор": "TLS", "саудовская аравия": "SAU",
}

DIRECTIONS = ["восток", "северо-восток", "север", "северо-запад", "запад", "юго-запад", "юг", "юго-восток"]


def project_coords(coords: np.ndarray) -> np.ndarray:
    lon = np.radians(coords[:, 0])
    lat = np.radians(np.clip(coords[:, 1], -89.9, 89.9))
    th = np.arcsin(_M * np.sin(lat))
    th2 = th * th
    th6 = th2 ** 3
    x = lon * np.cos(th) / (_M * (_A1 + 3 * _A2 * th2 + th6 * (7 * _A3 + 9 * _A4 * th2)))
    y = th * (_A1 + _A2 * th2 + th6 * (_A3 + _A4 * th2))
    return np.column_stack([x, y])


def project_point(lon: float, lat: float) -> tuple[float, float]:
    x, y = project_coords(np.array([[lon, lat]]))[0]
    return float(x), float(y)


def km(units: float) -> float:
    return units * EARTH_R


def name_key(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").replace("’", "").replace("'", "").split())


@dataclass
class Cell:
    id: int
    code: str
    geom: Polygon | MultiPolygon
    x: float
    y: float
    area_km2: float
    label: str
    city: str | None = None
    city_pop: int = 0
    neighbors: dict[int, str] = field(default_factory=dict)


@dataclass
class CountryInfo:
    code: str
    name: str
    name_en: str
    name_long: str
    iso2: str
    pop: int
    gdp_md: int
    income: int
    continent: str
    label_x: float
    label_y: float

    @property
    def flag(self) -> str:
        if len(self.iso2) != 2:
            return "🏳️"
        return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in self.iso2.upper())


@dataclass
class World:
    countries: dict[str, CountryInfo]
    cells: list[Cell]
    cells_by_country: dict[str, list[int]]
    capital_cell: dict[str, int]
    cities: list[dict]
    _index: dict[str, str] | None = field(default=None, repr=False)

    @property
    def name_index(self) -> dict[str, str]:
        if self._index is None:
            index: dict[str, str] = {}
            for code, c in self.countries.items():
                for n in (c.name, c.name_en, c.name_long):
                    if n:
                        index.setdefault(name_key(n), code)
            self._index = index
        return self._index

    def find_country(self, query: str) -> str | None:
        key = name_key(query)
        if not key:
            return None
        if key.upper() in self.countries:
            return key.upper()
        if key in ALIASES:
            return ALIASES[key]
        index = self.name_index
        if key in index:
            return index[key]
        prefix = [code for name, code in index.items() if len(key) >= 4 and name.startswith(key)]
        if len(set(prefix)) == 1:
            return prefix[0]
        close = difflib.get_close_matches(key, list(index), n=1, cutoff=0.75)
        if close:
            return index[close[0]]
        stem = [code for name, code in index.items() if len(key) >= 5 and name.startswith(key[:-1])]
        return stem[0] if len(set(stem)) == 1 else None


def _hex_grid(bounds: tuple, s: float) -> list[tuple[int, int, Polygon]]:
    """Pointy-top hexes whose vertices sit on a shared lattice, so neighbours share exact coordinates."""
    minx, miny, maxx, maxy = bounds
    hx, hy = math.sqrt(3) * s / 2, s / 2
    r0, r1 = math.floor(miny / (3 * hy)) - 1, math.ceil(maxy / (3 * hy)) + 1
    q0, q1 = math.floor(minx / (2 * hx)) - 2, math.ceil(maxx / (2 * hx)) + 2
    out = []
    for r in range(r0, r1 + 1):
        for q in range(q0, q1 + 1):
            ix, iy = 2 * q + (r % 2), 3 * r
            pts = [(ix + 1, iy + 1), (ix, iy + 2), (ix - 1, iy + 1), (ix - 1, iy - 1), (ix, iy - 2), (ix + 1, iy - 1)]
            out.append((r, q, Polygon([(a * hx, b * hy) for a, b in pts])))
    return out


def _direction(dx: float, dy: float, spread: float) -> str:
    if math.hypot(dx, dy) < spread * 0.25:
        return "центр"
    return DIRECTIONS[int(((math.degrees(math.atan2(dy, dx)) + 360 + 22.5) % 360) // 45)]


def _build(world_json: dict, cities_raw: list[dict]) -> World:
    hex_area = CELL_AREA_KM2 / EARTH_R ** 2
    s = math.sqrt(hex_area / (3 * math.sqrt(3) / 2))

    countries: dict[str, CountryInfo] = {}
    geoms: dict[str, Polygon | MultiPolygon] = {}
    for c in world_json["countries"]:
        g = shape({"type": c["geom_type"], "coordinates": c["geom"]})
        g = shapely.make_valid(shapely.transform(g, project_coords))
        geoms[c["code"]] = g
        pt = g.representative_point() if not g.is_empty else Point(0, 0)
        countries[c["code"]] = CountryInfo(
            c["code"], c["name_ru"], c["name_en"], c["name_long"], c["iso2"], c["pop"], c["gdp_md"],
            c["income"], c["continent"], pt.x, pt.y,
        )

    raw_cells: list[tuple[str, Polygon | MultiPolygon]] = []
    for code in sorted(geoms):
        g = geoms[code]
        if g.is_empty:
            continue
        grid = _hex_grid(g.bounds, s)
        hexes = np.array([h for _, _, h in grid], dtype=object)
        cand = hexes[shapely.intersects(hexes, g)]
        pieces = [p for p in shapely.intersection(cand, g) if not p.is_empty and p.area > 0]
        pieces = [shapely.make_valid(p) for p in pieces]
        pieces = [p for p in pieces if p.geom_type in ("Polygon", "MultiPolygon", "GeometryCollection") and p.area > 0]
        if not pieces:
            continue
        kept = [p for p in pieces if p.area >= SLIVER_SHARE * hex_area]
        slivers = [p for p in pieces if p.area < SLIVER_SHARE * hex_area]
        if not kept:
            kept, slivers = [shapely.union_all(pieces)], []
        groups = [[k] for k in kept]
        centers = [k.centroid for k in kept]
        for sl in slivers:
            c = sl.centroid
            idx = min(range(len(kept)), key=lambda i: c.distance(centers[i]))
            groups[idx].append(sl)
        for grp in groups:
            geom = shapely.union_all(grp) if len(grp) > 1 else grp[0]
            geom = _polygonal(geom)
            if geom is not None:
                raw_cells.append((code, geom))

    cells: list[Cell] = []
    by_country: dict[str, list[int]] = {}
    for i, (code, geom) in enumerate(raw_cells):
        pt = geom.representative_point()
        cells.append(Cell(i, code, geom, pt.x, pt.y, round(geom.area * EARTH_R ** 2), label=""))
        by_country.setdefault(code, []).append(i)

    tree = STRtree([c.geom for c in cells])
    land_d, sea_d = LAND_GAP_KM / EARTH_R, SEA_GAP_KM / EARTH_R
    left, right = tree.query([c.geom for c in cells], predicate="dwithin", distance=sea_d)
    for a, b in zip(left.tolist(), right.tolist()):
        if a == b:
            continue
        kind = "land" if cells[a].geom.distance(cells[b].geom) <= land_d else "sea"
        cells[a].neighbors[b] = kind

    cities = []
    for city in sorted(cities_raw, key=lambda c: -c["pop"]):
        x, y = project_point(city["lon"], city["lat"])
        hits = tree.query(Point(x, y), predicate="intersects").tolist()
        cell_id = hits[0] if hits else None
        cities.append({**city, "x": x, "y": y, "cell": cell_id})
        if cell_id is not None and cells[cell_id].city is None:
            cells[cell_id].city = city["name"]
            cells[cell_id].city_pop = city["pop"]

    capital: dict[str, int] = {}
    for city in cities:
        if city["capital"] and city["code"] in by_country and city["code"] not in capital:
            own = by_country[city["code"]]
            if city["cell"] in own:
                capital[city["code"]] = city["cell"]
            else:
                p = Point(city["x"], city["y"])
                capital[city["code"]] = min(own, key=lambda i: cells[i].geom.distance(p))
    for code, own in by_country.items():
        capital.setdefault(code, max(own, key=lambda i: cells[i].area_km2))

    for code, own in by_country.items():
        info = countries[code]
        xs = [cells[i].x for i in own]
        ys = [cells[i].y for i in own]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        spread = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
        used: dict[str, int] = {}
        for i in own:
            c = cells[i]
            if c.city:
                base = c.city
            elif len(own) == 1:
                base = info.name
            else:
                base = f"{info.name}: {_direction(c.x - cx, c.y - cy, spread)}"
            used[base] = used.get(base, 0) + 1
            c.label = base if used[base] == 1 else f"{base} {used[base]}"

    return World(countries, cells, by_country, capital, cities)


def _polygonal(geom):
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not polys:
        return None
    return shapely.union_all(polys)


@lru_cache(maxsize=1)
def load_world() -> World:
    world_bytes = (DATA / "world.json").read_bytes()
    cities_bytes = (DATA / "cities.json").read_bytes()
    digest = hashlib.sha256(world_bytes + cities_bytes + f"{BUILD_VERSION}:{CELL_AREA_KM2}".encode()).hexdigest()[:16]
    cache_file = CACHE / f"world_{digest}.pkl"
    if cache_file.exists():
        with cache_file.open("rb") as f:
            return pickle.load(f)
    world = _build(json.loads(world_bytes), json.loads(cities_bytes))
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump(world, f)
    tmp.replace(cache_file)
    return world
