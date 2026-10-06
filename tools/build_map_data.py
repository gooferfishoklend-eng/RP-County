"""Build bot/data/world.json and bot/data/cities.json from Natural Earth GeoJSON.

Usage: python tools/build_map_data.py <ne_50m_admin_0_countries.geojson> <ne_10m_populated_places.geojson>
Sources: https://github.com/nvkelso/natural-earth-vector (public domain).
"""

import json
import sys
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

OUT = Path(__file__).resolve().parent.parent / "bot" / "data"
DROP = {"ATA"}
FORCE_SEPARATE = {"PSX"}
MERGE_INTO = {"KAS": "IND"}
SHORT_NAMES = {
    "CHN": "Китай", "KOR": "Южная Корея", "PRK": "Северная Корея", "HTI": "Гаити", "KOS": "Косово",
    "CYN": "Северный Кипр", "COD": "ДР Конго", "CAF": "ЦАР", "ARE": "ОАЭ", "PNG": "Папуа — Новая Гвинея",
}


def round_coords(obj, nd=3):
    if isinstance(obj, (list, tuple)):
        return [round_coords(o, nd) for o in obj]
    return round(obj, nd)


def build_countries(path: str) -> dict:
    features = json.load(open(path, encoding="utf-8"))["features"]
    main_by_sov = {}
    for f in features:
        p = f["properties"]
        if p["ADMIN"] == p["SOVEREIGNT"]:
            main_by_sov[p["SOV_A3"]] = p["ADM0_A3"]

    groups: dict[str, dict] = {}
    for f in features:
        p = f["properties"]
        code = p["ADM0_A3"]
        if code in DROP:
            continue
        owner = code
        if code in MERGE_INTO:
            owner = MERGE_INTO[code]
        elif p["ADMIN"] != p["SOVEREIGNT"] and code not in FORCE_SEPARATE:
            owner = main_by_sov.get(p["SOV_A3"], code)
        g = groups.setdefault(owner, {"geoms": [], "pop": 0, "gdp": 0, "props": None})
        g["geoms"].append(shape(f["geometry"]))
        g["pop"] += max(0, p["POP_EST"] or 0)
        g["gdp"] += max(0, p["GDP_MD"] or 0)
        if owner == code:
            g["props"] = p

    countries = []
    for code, g in sorted(groups.items()):
        p = g["props"]
        if p is None:
            continue
        geom = unary_union(g["geoms"]).simplify(0.03, preserve_topology=True)
        iso2 = p["ISO_A2_EH"] if len(p["ISO_A2_EH"] or "") == 2 else ""
        countries.append({
            "code": code,
            "name_ru": SHORT_NAMES.get(code) or p["NAME_RU"] or p["NAME"],
            "name_en": p["NAME"],
            "name_long": p["NAME_LONG"],
            "iso2": iso2,
            "pop": int(g["pop"]),
            "gdp_md": int(g["gdp"]),
            "income": int(str(p["INCOME_GRP"])[0]),
            "continent": p["CONTINENT"],
            "geom": round_coords(mapping(geom)["coordinates"]),
            "geom_type": geom.geom_type,
        })
    return {"countries": countries}


def build_cities(path: str, codes: set[str]) -> list[dict]:
    features = json.load(open(path, encoding="utf-8"))["features"]
    cities = []
    for f in features:
        p = f["properties"]
        capital = p["ADM0CAP"] == 1 or p["FEATURECLA"] == "Admin-0 capital"
        if not capital and p["SCALERANK"] > 6 and (p["POP_MAX"] or 0) < 500_000:
            continue
        cities.append({
            "name": p["NAME_RU"] or p["NAME"],
            "code": p["ADM0_A3"] if p["ADM0_A3"] in codes else p["SOV_A3"],
            "lon": round(p["LONGITUDE"], 3),
            "lat": round(p["LATITUDE"], 3),
            "pop": int(p["POP_MAX"] or 0),
            "capital": capital,
        })
    return cities


def main():
    countries_path, places_path = sys.argv[1], sys.argv[2]
    OUT.mkdir(parents=True, exist_ok=True)
    world = build_countries(countries_path)
    codes = {c["code"] for c in world["countries"]}
    cities = build_cities(places_path, codes)
    (OUT / "world.json").write_text(json.dumps(world, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "cities.json").write_text(json.dumps(cities, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"countries: {len(world['countries'])}, cities: {len(cities)}")


if __name__ == "__main__":
    main()
