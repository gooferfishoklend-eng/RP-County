import pytest

from bot.conference import clean_terms, conference_context, execute_terms, render_terms
from bot.db import Database
from bot.game import new_game, world_snapshot

CHAT = -100
ROOM = -200


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "c.db"))
    await database.connect()
    await new_game(database, CHAT, "Тест", 1)
    yield database
    await database.close()


async def take(db, user_id, code):
    c = await db.get_country_by_code(CHAT, code)
    await db.assign_player(c["id"], user_id, f"player{user_id}", {**c})
    return await db.get_country(c["id"])


def empty_terms(**kw):
    base = {"title": "Договор", "summary": "Мир.", "peace": [], "province_transfers": [], "payments": [], "alliances": [],
            "relation_changes": [], "clauses": [], "unresolved": [], "npc_positions": [], "ready_to_sign": True}
    return {**base, **kw}


async def setup_conference(db):
    ukr = await take(db, 1, "UKR")
    rus = await take(db, 2, "RUS")
    tur = await db.get_country_by_code(CHAT, "TUR")
    await db.change_relation(CHAT, rus["id"], ukr["id"], -40, "war")
    owner, _ = await db.cell_owners(CHAT)
    occupied = [c for c, o in owner.items() if o == ukr["id"]][:2]
    for c in occupied:
        await db.set_cell_owner(CHAT, c, rus["id"])
    await db.add_room(ROOM, CHAT, "room")
    conf_id = await db.create_conference(CHAT, ROOM, "мир", ukr["id"], 1,
                                         [(ukr["id"], 1), (rus["id"], 2), (tur["id"], None)])
    await db.log_conference(conf_id, 10, "player", ukr["id"], "Украина", f"Верните провинции {occupied[0]} и Харьков")
    await db.log_conference(conf_id, 11, "npc", tur["id"], "Турция", "Готовы гарантировать мир")
    await db.log_conference(conf_id, 12, "system", None, "бот", "служебное")
    return ukr, rus, tur, occupied, await db.get_conference(conf_id)


async def test_context_contains_transcript_and_provinces(db):
    ukr, rus, tur, occupied, conf = await setup_conference(db)
    ctx = await conference_context(db, conf)
    assert {p["name"] for p in ctx["participants"]} == {"Украина", "Россия", "Турция"}
    assert len(ctx["transcript"]) == 2
    assert ctx["wars_between_participants"] == [{"a_id": min(ukr["id"], rus["id"]), "b_id": max(ukr["id"], rus["id"])}]
    cells = {p["cell_id"]: p for p in ctx["provinces"]}
    assert all(c in cells for c in occupied)
    assert cells[occupied[0]]["why"] == "mentioned"
    kharkiv = next(p for p in ctx["provinces"] if p["label"] == "Харьков")
    assert kharkiv["why"] == "mentioned"


async def test_clean_terms_rejects_invalid(db):
    ukr, rus, tur, occupied, conf = await setup_conference(db)
    owner, _ = await db.cell_owners(CHAT)
    usa = await db.get_country_by_code(CHAT, "USA")
    terms = empty_terms(
        province_transfers=[{"cell_id": occupied[0], "from_id": ukr["id"], "to_id": rus["id"]},
                            {"cell_id": occupied[0], "from_id": rus["id"], "to_id": ukr["id"]},
                            {"cell_id": occupied[0], "from_id": rus["id"], "to_id": ukr["id"]}],
        payments=[{"from_id": usa["id"], "to_id": ukr["id"], "amount_bn": 5, "purpose": "x"},
                  {"from_id": rus["id"], "to_id": ukr["id"], "amount_bn": -1, "purpose": "x"}],
        alliances=[{"a_id": ukr["id"], "b_id": ukr["id"]}],
    )
    cleaned = clean_terms(terms, {ukr["id"], rus["id"], tur["id"]}, owner)
    assert cleaned["province_transfers"] == [{"cell_id": occupied[0], "from_id": rus["id"], "to_id": ukr["id"]}]
    assert cleaned["payments"] == [] and cleaned["alliances"] == []


async def test_execute_terms_applies_everything(db):
    ukr, rus, tur, occupied, conf = await setup_conference(db)
    owner, _ = await db.cell_owners(CHAT)
    rus_cell = next(c for c, o in owner.items() if o == rus["id"] and c not in occupied)
    terms = empty_terms(
        title="Стамбульский договор",
        peace=[{"a_id": ukr["id"], "b_id": rus["id"], "mode": "status_quo"}],
        province_transfers=[{"cell_id": rus_cell, "from_id": rus["id"], "to_id": tur["id"]}],
        payments=[{"from_id": rus["id"], "to_id": ukr["id"], "amount_bn": 10, "purpose": "репарации"},
                  {"from_id": ukr["id"], "to_id": tur["id"], "amount_bn": 10_000, "purpose": "слишком много"}],
        alliances=[{"a_id": ukr["id"], "b_id": tur["id"]}],
        clauses=["Демилитаризованная зона 50 км", "Обмен пленными всех на всех"],
    )
    names = {c["id"]: c["name"] for c in (ukr, rus, tur)}
    assert "Стамбульский договор" in render_terms(terms, names)

    effects = await execute_terms(db, conf, terms)
    owner, core = await db.cell_owners(CHAT)
    assert all(owner[c] == ukr["id"] for c in occupied)
    assert owner[rus_cell] == tur["id"] and core[rus_cell] == tur["id"]
    assert (await db.get_relation(CHAT, ukr["id"], rus["id"]))["status"] == "peace"
    assert (await db.get_relation(CHAT, ukr["id"], tur["id"]))["status"] == "alliance"
    assert (await db.get_country(ukr["id"]))["budget"] < ukr["budget"] + 10  # paid Turkey only what it could afford
    assert (await db.get_country(rus["id"]))["budget"] == round(rus["budget"] - 10, 1)
    assert any("репарации" in e for e in effects)
    actions = await db.list_actions(CHAT, 1, ukr["id"])
    assert actions and "Демилитаризованная зона" in actions[-1]["text"]
    assert (await db.get_conference(conf["id"]))["status"] == "signed"
    snapshot = await world_snapshot(db, CHAT)
    assert snapshot["agreements_in_force"][0]["title"] == "Стамбульский договор"
