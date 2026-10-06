"""Render the game world map to PNG."""

import colorsys
import hashlib
import io
from dataclasses import dataclass, field

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import shapely  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402

from bot.geo import World  # noqa: E402

OCEAN = "#9ec9e2"
PLAYER_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46c8d8", "#f032e6", "#bcf60c",
    "#008080", "#9a6324", "#800000", "#808000", "#000075", "#ffd400", "#fa8072", "#2e8b57",
]
FRONT_BUFFER = 25 / 6371


@dataclass
class MapView:
    owner: dict[int, int]
    core: dict[int, int]
    countries: dict[int, dict]
    wars: list[tuple[int, int]] = field(default_factory=list)
    generals: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    title: str = ""


def npc_color(code: str) -> str:
    h = int(hashlib.md5(code.encode()).hexdigest()[:6], 16)
    hue = (h % 360) / 360
    r, g, b = colorsys.hls_to_rgb(hue, 0.80 + (h >> 9) % 8 / 100, 0.25 + (h >> 4) % 15 / 100)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def country_colors(countries: dict[int, dict]) -> dict[int, str]:
    colors = {}
    players = sorted(cid for cid, c in countries.items() if c.get("user_id"))
    for i, cid in enumerate(players):
        colors[cid] = PLAYER_COLORS[i % len(PLAYER_COLORS)]
    for cid, c in countries.items():
        colors.setdefault(cid, npc_color(c["code"]))
    return colors


def _path(geom) -> MplPath | None:
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    verts, codes = [], []
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            xy = list(ring.coords)
            if len(xy) < 3:
                continue
            verts.extend(xy)
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(xy) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes) if verts else None


def _fill(ax, geom, **kw):
    p = _path(geom)
    if p is not None:
        ax.add_patch(PathPatch(p, **kw))


def _lines(ax, geom, **kw):
    if geom.is_empty:
        return
    parts = getattr(geom, "geoms", [geom])
    for g in parts:
        if g.geom_type in ("LineString", "LinearRing"):
            xs, ys = g.xy
            ax.plot(xs, ys, **kw)
        elif g.geom_type in ("MultiLineString", "GeometryCollection"):
            _lines(ax, g, **kw)


def render_map(world: World, view: MapView, focus: int | None = None, mode: str = "political") -> bytes:
    colors = country_colors(view.countries)
    owned: dict[int, list] = {}
    for cell_id, owner in view.owner.items():
        owned.setdefault(owner, []).append(cell_id)
    unions = {cid: shapely.union_all([world.cells[i].geom for i in ids]) for cid, ids in owned.items()}

    if focus is not None and focus in owned:
        focus_cells = owned[focus]
        near = set(focus_cells)
        for i in focus_cells:
            near.update(world.cells[i].neighbors)
        bounds = shapely.union_all([world.cells[i].geom for i in near]).bounds
        minx, miny, maxx, maxy = bounds
        pad = max(maxx - minx, maxy - miny) * 0.15 + 0.02
        minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        w, h = maxx - minx, maxy - miny
        figsize = (12, max(5, min(14, 12 * h / w)))
    else:
        focus = None
        minx, miny, maxx, maxy = -2.75, -1.0, 2.75, 1.36
        figsize = (18, 8.2)

    fig, ax = plt.subplots(figsize=figsize, dpi=100)
    ax.set_facecolor(OCEAN)
    fig.patch.set_facecolor("#f4f1ea")
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])

    zoom = focus is not None
    border_lw = 1.0 if zoom else 0.35

    for cid, geom in unions.items():
        _fill(ax, geom, facecolor=colors[cid], edgecolor="none", zorder=1)

    for cell_id, owner in view.owner.items():
        core = view.core.get(cell_id, owner)
        if core != owner:
            _fill(ax, world.cells[cell_id].geom, facecolor="none", edgecolor=colors.get(core, "#555"),
                  hatch="////", linewidth=0, zorder=2)

    if zoom:
        visible = [i for i, c in enumerate(world.cells) if minx <= c.x <= maxx and miny <= c.y <= maxy and i in view.owner]
        for i in visible:
            _lines(ax, world.cells[i].geom.boundary, color="#00000030", linewidth=0.4, zorder=2)

    for cid, geom in unions.items():
        is_player = bool(view.countries.get(cid, {}).get("user_id"))
        _lines(ax, geom.boundary, color="#222" if is_player else "#555",
               linewidth=border_lw * (1.8 if is_player else 1), zorder=3)

    if mode == "events":
        for ev in view.events:
            for cid in ev.get("affected", []):
                if cid in unions:
                    _fill(ax, unions[cid], facecolor="#ff000040", edgecolor="#b00000", hatch="xx",
                          linewidth=0.8, zorder=4)

    for a, b in view.wars:
        if a in unions and b in unions:
            front = shapely.intersection(unions[a].boundary, shapely.buffer(unions[b], FRONT_BUFFER))
            _lines(ax, front, color="#d00000", linewidth=3.2 if zoom else 2.2, zorder=6, solid_capstyle="round")
            _lines(ax, front, color="#ffffff", linewidth=0.9 if zoom else 0.6, linestyle=(0, (2, 2)), zorder=7)

    for cid, c in view.countries.items():
        cap = c.get("capital_cell")
        if cap is None or cap not in view.owner:
            continue
        if not (c.get("user_id") or zoom):
            continue
        cell = world.cells[cap]
        if not (minx <= cell.x <= maxx and miny <= cell.y <= maxy):
            continue
        ax.plot(cell.x, cell.y, marker="*", markersize=11 if c.get("user_id") else 7, color="#ffd700",
                markeredgecolor="#000", markeredgewidth=0.6, zorder=13)

    for g in view.generals:
        cell_id = g.get("position_cell")
        if cell_id is None or cell_id >= len(world.cells):
            continue
        cell = world.cells[cell_id]
        ax.plot(cell.x, cell.y, marker="^", markersize=11 if zoom else 7, color=colors.get(g["country_id"], "#000"),
                markeredgecolor="#fff", markeredgewidth=1.2, zorder=13)
        if zoom:
            ax.annotate(g["name"], (cell.x, cell.y), xytext=(5, -9), textcoords="offset points", fontsize=7,
                        zorder=10, bbox=dict(boxstyle="round,pad=0.15", fc="#ffffffcc", ec="none"))

    if zoom:
        for i in visible:
            cell = world.cells[i]
            owner = view.owner[i]
            if owner == focus or any(view.owner.get(n) == focus for n in cell.neighbors):
                ax.text(cell.x, cell.y, str(i), fontsize=6.5, ha="center", va="center", color="#000", zorder=11,
                        bbox=dict(boxstyle="round,pad=0.1", fc="#ffffffb0", ec="none"))
            if cell.city and cell.city_pop > 300_000:
                ax.annotate(cell.city, (cell.x, cell.y), xytext=(0, 7), textcoords="offset points", fontsize=6.5,
                            ha="center", color="#333", zorder=10)

    for cid, geom in unions.items():
        c = view.countries.get(cid)
        if not c:
            continue
        is_player = bool(c.get("user_id"))
        area_km2 = geom.area * 6371 ** 2
        if not is_player and not zoom and area_km2 < 900_000:
            continue
        biggest = max(getattr(geom, "geoms", [geom]), key=lambda g: g.area)
        pt = biggest.representative_point()
        if not (minx <= pt.x <= maxx and miny <= pt.y <= maxy):
            continue
        size = 10 if is_player else (8 if zoom else 6)
        ax.text(pt.x, pt.y, c["name"], fontsize=size, ha="center", va="center", zorder=12,
                fontweight="bold" if is_player else "normal", color="#000" if is_player else "#444",
                bbox=dict(boxstyle="round,pad=0.2", fc="#ffffffd0", ec="none") if is_player else None)

    handles = [
        Patch(facecolor=colors[cid], edgecolor="#222", label=f"{c['name']} ({c.get('player_name') or ''})")
        for cid, c in sorted(view.countries.items()) if c.get("user_id") and cid in owned
    ]
    if view.wars:
        handles.append(Line2D([0], [0], color="#d00000", lw=3, label="Линия фронта"))
    if any(view.core.get(i, o) != o for i, o in view.owner.items()):
        handles.append(Patch(facecolor="#ddd", edgecolor="#555", hatch="////", label="Оккупировано"))
    if view.generals:
        handles.append(Line2D([0], [0], marker="^", color="w", markerfacecolor="#888", markeredgecolor="#000",
                              markersize=9, label="Генерал"))
    if mode == "events":
        for ev in view.events:
            handles.append(Patch(facecolor="#ff000040", edgecolor="#b00000", hatch="xx", label=ev["name"]))
    if handles:
        ax.legend(handles=handles, loc="lower left", fontsize=8, framealpha=0.9)

    ax.set_title(view.title, fontsize=13, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
