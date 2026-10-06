import pytest

from bot.db import Database
from bot.game import resolve_turn
from bot.stats import STAT_KEYS, apply_deltas, power_index, turn_label

CHAT = -100

BASE = {
    "flag": "🏳️", "government": "Республика", "leader_title": "Президент", "description": "",
    "gdp": 1000.0, "budget": 50.0, "population": 50.0, "stability": 60, "approval": 55,
    "military": 40, "tech": 50, "corruption": 30, "influence": 30,
}


class FakeGM:
    def __init__(self, deltas_by_id=None):
        self.deltas_by_id = deltas_by_id or {}
        self.last_world = None

    async def resolve_turn(self, world):
        self.last_world = world
        ids = [c["id"] for c in world["countries"]]
        zero = {k: 0 for k in STAT_KEYS}
        return {
            "headline": "Тихий квартал",
            "world_news": "Ничего особенного.",
            "world_event": "Нефть подорожала.",
            "countries": [
                {"country_id": cid, "deltas": {**zero, **self.deltas_by_id.get(cid, {})},
                 "public_summary": "ок", "private_report": f"доклад {cid}", "secret_exposed": False}
                for cid in ids
            ] + [{"country_id": 9999, "deltas": zero, "public_summary": "x", "private_report": "x", "secret_exposed": False}],
            "relations": [{"a_id": ids[0], "b_id": ids[1], "delta": 10, "status": "alliance", "reason": "договор"}],
        }


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.connect()
    await database.create_game(CHAT, "Тест", 1)
    yield database
    await database.close()


async def add(db, user_id, name):
    return await db.add_country(CHAT, user_id, f"player{user_id}", {**BASE, "name": name})


def test_apply_deltas_clamps():
    c = {**BASE}
    new = apply_deltas(c, {"stability": 90, "gdp": 5000, "population": -100, "budget": -10_000, "tech": -3})
    assert new["stability"] == 85  # +25 cap
    assert new["gdp"] == 1200.0  # +20% cap
    assert new["population"] == 47.5  # -5% cap
    assert new["budget"] == 50.0 - 500.0  # -50% of gdp cap
    assert new["tech"] == 47


def test_power_and_label():
    assert 0 < power_index(BASE) < 100
    assert turn_label(1, 2026) == "1-й квартал 2026 г."
    assert turn_label(6, 2026) == "2-й квартал 2027 г."


async def test_unique_country_name_case_insensitive(db):
    await add(db, 1, "Франция")
    assert (await db.get_country_by_name(CHAT, "франция"))["user_id"] == 1


async def test_resolve_turn_applies_changes(db):
    a = await add(db, 1, "Франция")
    b = await add(db, 2, "Германия")
    await db.add_action(CHAT, 1, a, "secret", "тайно реформировать налоговую")
    rid = await db.add_resolution(CHAT, 1, a, "Запретить боевых роботов")
    await db.cast_vote(rid, a, "yes")
    await db.cast_vote(rid, b, "no")

    gm = FakeGM({a: {"tech": 5, "gdp": 50}})
    outcome = await resolve_turn(db, gm, CHAT)

    assert gm.last_world["actions"][0]["kind"] == "secret"
    assert gm.last_world["un_resolutions"][0]["passed"] is False
    france = await db.get_country(a)
    assert france["tech"] == 55 and france["gdp"] == 1050.0
    assert outcome.private_reports[b] == f"доклад {b}"
    rel = await db.get_relation(CHAT, b, a)
    assert rel["value"] == 10 and rel["status"] == "alliance"
    assert (await db.get_game(CHAT))["turn"] == 2
    assert (await db.open_resolutions(CHAT)) == []


async def test_collapse_on_low_stability(db):
    a = await add(db, 1, "Франция")
    await add(db, 2, "Германия")
    await db.update_country_stats(a, {"stability": 20})
    outcome = await resolve_turn(db, FakeGM({a: {"stability": -20}}), CHAT)
    assert outcome.collapses == ["Франция"]
    france = await db.get_country(a)
    assert france["stability"] == 30
    assert france["budget"] == 25.0
