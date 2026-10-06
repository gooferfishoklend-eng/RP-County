import random

import pytest

from bot.db import Database
from bot.game import apply_treaty, declare_war, map_view, new_game, resolve_turn, world_snapshot
from bot.geo import load_world
from bot.mapdraw import render_map
from bot.stats import STAT_KEYS, apply_deltas, power_index, turn_label
from bot.war import attack_targets, encirclements, resolve_operations

CHAT = -100


class FakeGM:
    def __init__(self, deltas=None, plans=None, events=None, npc_messages=None):
        self.deltas = deltas or {}
        self.plans = plans
        self.events = events or []
        self.npc_messages = npc_messages or []
        self.last_world = None
        self.last_briefs = None

    async def resolve_turn(self, world, *, npc_chat, random_events):
        self.last_world = world
        zero = {k: 0 for k in STAT_KEYS}
        players = [c["id"] for c in world["countries"] if c["player"]]
        return {
            "headline": "Тихий квартал",
            "world_news": "Ничего особенного.",
            "world_event": "Нефть подорожала.",
            "countries": [
                {"country_id": cid, "deltas": {**zero, **self.deltas.get(cid, {})}, "public_summary": "ок",
                 "private_report": f"доклад {cid}", "secret_exposed": False}
                for cid in players
            ] + [{"country_id": 999999, "deltas": zero, "public_summary": "x", "private_report": "x", "secret_exposed": False}],
            "relations": [],
            "events": self.events,
            "npc_messages": self.npc_messages,
        }

    async def plan_operations(self, briefs):
        self.last_briefs = briefs
        if self.plans is not None:
            return self.plans(briefs)
        plans = []
        for b in briefs:
            attacks = [{"target_cell_id": t["cell_id"], "general_id": b["generals"][0]["general_id"] if b["generals"] else 0,
                        "force": 45} for t in b["targets"][:2]] if b["is_player"] else []
            plans.append({"country_id": b["country_id"], "enemy_id": b["enemy_id"], "attacks": attacks,
                          "defense": [], "report": "Наступаем."})
        return plans

    async def create_general(self, country, existing):
        return {"name": f"Генерал {len(existing) + 1}", "rank": "генерал-майор", "trait": "агрессивный"}


@pytest.fixture(scope="session")
def world():
    return load_world()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.connect()
    await new_game(database, CHAT, "Тест", 1)
    yield database
    await database.close()


async def take(db, user_id, code):
    c = await db.get_country_by_code(CHAT, code)
    await db.assign_player(c["id"], user_id, f"player{user_id}", {**c})
    return await db.get_country(c["id"])


def test_apply_deltas_clamps():
    c = {"gdp": 1000.0, "budget": 50.0, "population": 50.0, "stability": 60, "approval": 55, "military": 40,
         "tech": 50, "corruption": 30, "influence": 30}
    new = apply_deltas(c, {"stability": 90, "gdp": 5000, "population": -100, "budget": -10_000, "tech": -3})
    assert new["stability"] == 85
    assert new["gdp"] == 1200.0
    assert new["population"] == 47.5
    assert new["budget"] == 50.0 - 500.0
    assert new["tech"] == 47
    assert turn_label(6, 2026) == "2-й квартал 2027 г."


def test_world_and_lookup(world):
    assert len(world.countries) == 200
    for q, code in [("Франции", "FRA"), ("сша", "USA"), ("Германию", "DEU"), ("беларусь", "BLR"), ("китай", "CHN")]:
        assert world.find_country(q) == code
    assert world.find_country("Атлантида") is None
    assert world.cells[world.capital_cell["UKR"]].label == "Киев"
    kyiv = world.capital_cell["UKR"]
    assert any(world.cells[n].code == "UKR" for n in world.cells[kyiv].neighbors)


async def test_new_game_seeds_everything(db, world):
    countries = await db.list_countries(CHAT)
    owner, core = await db.cell_owners(CHAT)
    assert len(countries) == 200 and len(owner) == len(world.cells)
    usa = await db.get_country_by_code(CHAT, "USA")
    lux = await db.get_country_by_code(CHAT, "LUX")
    assert power_index(usa) > power_index(lux)
    assert usa["military"] == 97


async def test_war_turn_moves_front_and_reports(db):
    ukr = await take(db, 1, "UKR")
    rus = await take(db, 2, "RUS")
    gm = FakeGM()
    generals = await declare_war(db, gm, CHAT, rus, ukr)
    assert {g["country_id"] for g in generals} == {rus["id"], ukr["id"]}
    await db.add_action(CHAT, 1, rus["id"], "war", "наступать на Харьков", ukr["name"])

    outcome = await resolve_turn(db, gm, CHAT, rng=random.Random(1))
    brief = next(b for b in gm.last_briefs if b["country_id"] == rus["id"])
    assert brief["targets"] and brief["leader_orders"] == ["наступать на Харьков"]
    assert outcome.war_lines
    assert "Доклады генералов" in outcome.private_reports[rus["id"]]
    assert gm.last_world["war_results"]["battles"]
    owner, core = await db.cell_owners(CHAT)
    assert any(o == rus["id"] and core[c] == ukr["id"] for c, o in owner.items())
    assert (await db.get_game(CHAT))["turn"] == 2


def test_combat_strong_beats_weak(world):
    rng = random.Random(7)
    strong = {"id": 1, "gdp": 20000, "budget": 100, "military": 95, "stability": 70, "tech": 80, "population": 300,
              "capital_cell": world.capital_cell["USA"]}
    weak = {"id": 2, "gdp": 20, "budget": 1, "military": 10, "stability": 40, "tech": 20, "population": 10,
            "capital_cell": world.capital_cell["MEX"]}
    owner = {c.id: (1 if c.code == "USA" else 2 if c.code == "MEX" else 99) for c in world.cells}
    core = dict(owner)
    targets = attack_targets(world, owner, core, strong, weak)
    assert targets and all(t["via"] in ("land", "sea") for t in targets)
    plans = [{"country_id": 1, "enemy_id": 2, "attacks": [{"target_cell_id": t["cell_id"], "general_id": 0, "force": 40}
                                                         for t in targets[:2]], "defense": [], "report": ""}]
    res = resolve_operations(world, owner, core, {1: strong, 2: weak}, [(1, 2)], [], plans, rng)
    assert sum(b.success for b in res.battles) >= 1
    assert res.stat_changes[2]["stability"] < 0


def test_encirclement_captures_small_pocket(world):
    pocket = world.capital_cell["FRA"]
    owner = {c.id: 2 for c in world.cells}
    for cell in world.cells_by_country["RUS"]:
        owner[cell] = 1
    owner[pocket] = 1
    countries = {1: {"capital_cell": world.capital_cell["RUS"]}, 2: {"capital_cell": world.capital_cell["USA"]}}
    caught = encirclements(world, owner, countries, [(1, 2)])
    assert (pocket, 2, 1) in caught
    assert world.capital_cell["RUS"] not in {c for c, _, _ in caught}
    assert len(caught) <= 4  # the pocket plus tiny exclaves such as Kaliningrad
    assert encirclements(world, owner, countries, []) == []


async def test_peace_front_and_status_quo(db):
    ukr = await take(db, 1, "UKR")
    rus = await take(db, 2, "RUS")
    await db.change_relation(CHAT, rus["id"], ukr["id"], -40, "war")
    owner, _ = await db.cell_owners(CHAT)
    taken = [c for c, o in owner.items() if o == ukr["id"]][:3]
    for c in taken:
        await db.set_cell_owner(CHAT, c, rus["id"])

    tid = await db.add_treaty(CHAT, 1, "peace", rus["id"], ukr["id"], "", peace_mode="status_quo")
    effects = await apply_treaty(db, await db.get_treaty(tid))
    owner, core = await db.cell_owners(CHAT)
    assert all(owner[c] == ukr["id"] for c in taken)
    assert (await db.get_relation(CHAT, rus["id"], ukr["id"]))["status"] == "peace"
    assert effects

    for c in taken:
        await db.set_cell_owner(CHAT, c, rus["id"])
    tid = await db.add_treaty(CHAT, 1, "peace", rus["id"], ukr["id"], "", peace_mode="front")
    await apply_treaty(db, await db.get_treaty(tid))
    owner, core = await db.cell_owners(CHAT)
    assert all(owner[c] == rus["id"] and core[c] == rus["id"] for c in taken)


async def test_transfer_moves_capital_and_eliminates(db, world):
    lux = await take(db, 1, "LUX")
    fra = await take(db, 2, "FRA")
    owner, _ = await db.cell_owners(CHAT)
    lux_cells = [c for c, o in owner.items() if o == lux["id"]]
    tid = await db.add_treaty(CHAT, 1, "transfer", lux["id"], fra["id"], "уния",
                              transfers=[[c, lux["id"], fra["id"]] for c in lux_cells])
    effects = await apply_treaty(db, await db.get_treaty(tid))
    assert any("прекратила существование" in e for e in effects)
    assert not (await db.get_country(lux["id"]))["alive"]


async def test_events_created_and_updated(db):
    ukr = await take(db, 1, "UKR")
    china = await db.get_country_by_code(CHAT, "CHN")
    new = {"event_id": 0, "name": "Вирус X", "kind": "pandemic", "description": "Вспышка", "severity": 40,
           "affected_country_ids": [china["id"]], "status": "active"}
    gm = FakeGM(events=[new], npc_messages=[{"country_id": china["id"], "text": "Ситуация под контролем."},
                                            {"country_id": ukr["id"], "text": "игрок не говорит за НИП"}])
    outcome = await resolve_turn(db, gm, CHAT)
    events = await db.active_events(CHAT)
    assert events and events[0]["affected"] == [china["id"]]
    assert [c["code"] for c, _, _ in outcome.npc_messages] == ["CHN"]

    upd = {**new, "event_id": events[0]["id"], "severity": 70, "status": "ended"}
    await resolve_turn(db, FakeGM(events=[upd]), CHAT)
    assert await db.active_events(CHAT) == []

    await db.set_game_flag(CHAT, "random_events", False)
    await db.set_game_flag(CHAT, "npc_chat", False)
    outcome = await resolve_turn(db, FakeGM(events=[new], npc_messages=[{"country_id": china["id"], "text": "x"}]), CHAT)
    assert await db.active_events(CHAT) == [] and outcome.npc_messages == []


async def test_snapshot_and_map_render(db, world):
    ukr = await take(db, 1, "UKR")
    rus = await take(db, 2, "RUS")
    await declare_war(db, FakeGM(), CHAT, rus, ukr)
    snap = await world_snapshot(db, CHAT)
    names = {c["name"] for c in snap["countries"]}
    assert {"Украина", "Россия", "Польша", "США"} <= names
    ukr_brief = next(c for c in snap["countries"] if c["id"] == ukr["id"])
    assert ukr_brief["territory"]["capital"] == "Киев"
    view = await map_view(db, CHAT, "тест")
    png = render_map(world, view, focus=ukr["id"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 50_000
