"""Front lines, generals' operations and combat resolution on the hex map."""

import math
import random
from collections import Counter, deque
from dataclasses import dataclass, field

from bot.geo import World

MAX_TARGETS = 14
EXPEDITION_TARGETS = 4
PENALTY = {"land": 1.0, "sea": 0.75, "expedition": 0.5}


def army_power(c: dict) -> float:
    econ = 0.6 + 0.4 * min(1.0, math.log10(max(c["gdp"], 1) + 1) / 4)
    power = c["military"] * (0.7 + c["stability"] / 300) * (0.8 + c["tech"] / 250) * econ
    if c["budget"] < 0:
        power *= 0.85
    return max(1.0, power)


def max_attacks(c: dict) -> int:
    return 1 + c["military"] // 25


def skill_bonus(general: dict | None) -> float:
    return 1 + 0.04 * ((general["skill"] if general else 5) - 5)


def owned_cells(owner: dict[int, int], country_id: int) -> list[int]:
    return [cell for cell, o in owner.items() if o == country_id]


def hops_from(world: World, owner: dict[int, int], start: int | None, country_id: int) -> dict[int, int]:
    if start is None or owner.get(start) != country_id:
        return {}
    dist = {start: 0}
    q = deque([start])
    while q:
        cur = q.popleft()
        for n in world.cells[cur].neighbors:
            if n not in dist and owner.get(n) == country_id:
                dist[n] = dist[cur] + 1
                q.append(n)
    return dist


def attack_targets(world: World, owner: dict[int, int], core: dict[int, int], attacker: dict, defender: dict) -> list[dict]:
    """Enemy cells the attacker can strike this turn, most valuable first."""
    a_id, d_id = attacker["id"], defender["id"]
    found: dict[int, str] = {}
    for cell, o in owner.items():
        if o != d_id:
            continue
        kinds = [k for n, k in world.cells[cell].neighbors.items() if owner.get(n) == a_id]
        if kinds:
            found[cell] = "land" if "land" in kinds else "sea"
    if not found:
        mine = owned_cells(owner, a_id)
        theirs = owned_cells(owner, d_id)
        if not mine or not theirs:
            return []
        def dist(cell: int) -> float:
            c = world.cells[cell]
            return min(math.hypot(c.x - world.cells[m].x, c.y - world.cells[m].y) for m in mine[:400])
        for cell in sorted(theirs, key=dist)[:EXPEDITION_TARGETS]:
            found[cell] = "expedition"

    hops = hops_from(world, owner, defender.get("capital_cell"), d_id)
    out = []
    for cell, via in found.items():
        c = world.cells[cell]
        out.append({
            "cell_id": cell,
            "label": c.label,
            "via": via,
            "is_enemy_capital": cell == defender.get("capital_cell"),
            "city_pop": c.city_pop,
            "hops_to_enemy_capital": hops.get(cell),
            "liberates_our_land": core.get(cell) == a_id,
        })
    out.sort(key=lambda t: (not t["is_enemy_capital"], not t["liberates_our_land"], -t["city_pop"]))
    return out[:MAX_TARGETS]


@dataclass
class Battle:
    attacker_id: int
    defender_id: int
    cell_id: int
    label: str
    general_id: int | None
    probability: float
    success: bool
    via: str


@dataclass
class WarResult:
    battles: list[Battle] = field(default_factory=list)
    encircled: list[tuple[int, int, int]] = field(default_factory=list)
    stat_changes: dict[int, dict] = field(default_factory=dict)
    capital_moves: dict[int, int | None] = field(default_factory=dict)
    capital_lost: list[int] = field(default_factory=list)
    eliminated: list[int] = field(default_factory=list)
    reports: dict[int, list[str]] = field(default_factory=dict)
    general_updates: dict[int, dict] = field(default_factory=dict)
    fallen_generals: list[int] = field(default_factory=list)

    def bump(self, country_id: int, key: str, delta: float) -> None:
        self.stat_changes.setdefault(country_id, {}).setdefault(key, 0)
        self.stat_changes[country_id][key] += delta


def _normalize(orders: list[dict], key: str = "force") -> None:
    total = sum(max(0, int(o.get(key, 0))) for o in orders)
    for o in orders:
        o[key] = max(0, int(o.get(key, 0)))
        if total > 100:
            o[key] = o[key] * 100 / total


def resolve_operations(
    world: World,
    owner: dict[int, int],
    core: dict[int, int],
    countries: dict[int, dict],
    wars: list[tuple[int, int]],
    generals: list[dict],
    plans: list[dict],
    rng: random.Random,
) -> WarResult:
    """Mutates `owner` with captured cells and returns everything that happened."""
    result = WarResult()
    war_count = Counter()
    for a, b in wars:
        war_count[a] += 1
        war_count[b] += 1
    gen_by_id = {g["id"]: g for g in generals}
    plan_by_side = {(p["country_id"], p["enemy_id"]): p for p in plans}

    attacks: list[tuple[int, int, dict, dict]] = []
    defense: dict[tuple[int, int], dict[int, float]] = {}
    reserve: dict[tuple[int, int], float] = {}
    targets_by_side: dict[tuple[int, int], dict[int, dict]] = {}

    for a, b in wars:
        for side, enemy in ((a, b), (b, a)):
            me, foe = countries[side], countries[enemy]
            targets = {t["cell_id"]: t for t in attack_targets(world, owner, core, me, foe)}
            targets_by_side[(side, enemy)] = targets
            plan = plan_by_side.get((side, enemy), {})
            my_atk = [o for o in plan.get("attacks", []) if o.get("target_cell_id") in targets][: max_attacks(me)]
            my_front = {t["cell_id"] for t in attack_targets(world, owner, core, foe, me)}
            my_def = [o for o in plan.get("defense", []) if o.get("cell_id") in my_front]
            _normalize(my_atk + my_def)
            used = sum(o["force"] for o in my_atk + my_def)
            reserve[(side, enemy)] = max(0.0, 100 - used)
            defense[(side, enemy)] = {o["cell_id"]: o["force"] for o in my_def}
            for o in my_atk:
                attacks.append((side, enemy, o, targets[o["target_cell_id"]]))
            if plan.get("report"):
                result.reports.setdefault(side, []).append(plan["report"])

    rng.shuffle(attacks)
    lost_cells = Counter()
    gained_cells = Counter()
    for side, enemy, order, target in attacks:
        cell = target["cell_id"]
        if owner.get(cell) != enemy:
            continue
        me, foe = countries[side], countries[enemy]
        gen = gen_by_id.get(order.get("general_id") or 0)
        if gen and gen["country_id"] != side:
            gen = None
        p_atk = army_power(me) / war_count[side]
        p_def = army_power(foe) / war_count[enemy]
        atk = p_atk * order["force"] / 100 * skill_bonus(gen) * PENALTY[target["via"]]
        if target["liberates_our_land"]:
            atk *= 1.2
        front_n = max(1, len(targets_by_side[(side, enemy)]))
        garrison = defense[(enemy, side)].get(cell, 0) + reserve[(enemy, side)] / front_n + 8
        def_gen = max((g for g in generals if g["country_id"] == enemy and g.get("target_id") in (side, None)),
                      key=lambda g: g["skill"], default=None)
        fort = 1.9 if target["is_enemy_capital"] else 1.4 if target["city_pop"] > 500_000 else 1.25
        if core.get(cell) == enemy:
            fort *= 1.1
        dfn = p_def * garrison / 100 * fort * skill_bonus(def_gen)
        prob = atk ** 1.5 / (atk ** 1.5 + dfn ** 1.5) if atk > 0 else 0.0
        prob = min(0.95, max(0.05, prob))
        success = rng.random() < prob
        result.battles.append(Battle(side, enemy, cell, target["label"], gen["id"] if gen else None, round(prob, 2),
                                     success, target["via"]))

        result.bump(side, "military", -0.4)
        result.bump(enemy, "military", -0.4)
        result.bump(enemy, "population", -foe["population"] * 0.0005)
        if success:
            owner[cell] = side
            lost_cells[enemy] += 1
            gained_cells[side] += 1
            if cell == foe.get("capital_cell"):
                result.capital_lost.append(enemy)
        else:
            result.bump(side, "military", -0.8)

        if gen:
            upd = result.general_updates.setdefault(gen["id"], {"xp": gen["xp"], "skill": gen["skill"]})
            upd["xp"] += 2 if success else 1
            upd["position_cell"] = cell if success else _staging_cell(world, owner, cell, side)
            if not success and prob < 0.25 and rng.random() < 0.15:
                result.fallen_generals.append(gen["id"])

    for gid, upd in result.general_updates.items():
        upd["skill"] = min(10, upd["skill"] + (1 if upd["xp"] // 6 > gen_by_id[gid]["xp"] // 6 else 0))

    for cell_id, new_owner, old_owner in encirclements(world, owner, countries, wars):
        owner[cell_id] = new_owner
        result.encircled.append((cell_id, new_owner, old_owner))
        lost_cells[old_owner] += 1
        gained_cells[new_owner] += 1

    for cid, n in lost_cells.items():
        result.bump(cid, "military", -0.8 * n)
        result.bump(cid, "stability", -min(8, n))
        result.bump(cid, "approval", -min(5, n))
    for cid, n in gained_cells.items():
        result.bump(cid, "approval", min(4, n))
    for cid in set(result.capital_lost):
        result.bump(cid, "stability", -15)
    for a, b in wars:
        for cid in (a, b):
            result.bump(cid, "budget", -countries[cid]["gdp"] * 0.004)
    for cid, changes in result.stat_changes.items():
        if "military" in changes:
            changes["military"] = max(changes["military"], -12)

    involved = {c for war in wars for c in war}
    for cid in involved:
        mine = owned_cells(owner, cid)
        if not mine:
            result.eliminated.append(cid)
            result.capital_moves[cid] = None
            continue
        capital = countries[cid].get("capital_cell")
        if owner.get(capital) != cid:
            result.capital_moves[cid] = max(
                mine, key=lambda c: (core.get(c) == cid, world.cells[c].city_pop, world.cells[c].area_km2)
            )
    return result


def _staging_cell(world: World, owner: dict[int, int], target: int, side: int) -> int | None:
    for n in world.cells[target].neighbors:
        if owner.get(n) == side:
            return n
    mine = owned_cells(owner, side)
    if not mine:
        return None
    t = world.cells[target]
    return min(mine[:400], key=lambda c: math.hypot(world.cells[c].x - t.x, world.cells[c].y - t.y))


def encirclements(world: World, owner: dict[int, int], countries: dict[int, dict],
                  wars: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Small pockets cut off from the main territory and surrounded only by enemies surrender."""
    enemies: dict[int, set[int]] = {}
    for a, b in wars:
        enemies.setdefault(a, set()).add(b)
        enemies.setdefault(b, set()).add(a)
    captured = []
    for cid, foes in enemies.items():
        mine = set(owned_cells(owner, cid))
        if not mine:
            continue
        seen: set[int] = set()
        components = []
        for start in mine:
            if start in seen:
                continue
            comp, q = [], deque([start])
            seen.add(start)
            while q:
                cur = q.popleft()
                comp.append(cur)
                for n in world.cells[cur].neighbors:
                    if n in mine and n not in seen:
                        seen.add(n)
                        q.append(n)
            components.append(comp)
        if len(components) < 2:
            continue
        capital = countries[cid].get("capital_cell")
        main = next((c for c in components if capital in c), max(components, key=len))
        for comp in components:
            if comp is main or len(comp) > 3:
                continue
            border = Counter()
            for cell in comp:
                for n in world.cells[cell].neighbors:
                    if n not in mine:
                        border[owner.get(n)] += 1
            if border and all(o in foes for o in border):
                winner = border.most_common(1)[0][0]
                captured.extend((cell, winner, cid) for cell in comp)
    return captured


def apply_peace(owner: dict[int, int], core: dict[int, int], a: int, b: int, mode: str) -> list[tuple[int, int, int]]:
    """Return (cell, new_owner, new_core) updates for a peace treaty between a and b."""
    updates = []
    for cell, o in owner.items():
        c = core.get(cell, o)
        if o == c or {o, c} != {a, b}:
            continue
        if mode == "front":
            updates.append((cell, o, o))
        else:
            updates.append((cell, c, c))
    return updates
