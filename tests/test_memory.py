import random

import pytest

from bot.db import Database
from bot.export import build_history
from bot.game import (apply_treaty, declare_war, new_game, npc_answer, npc_context, repair_signed_peace, resolve_turn,
                      world_snapshot)
from bot.handlers.admin import _parse_value
from bot.stats import STAT_KEYS
from bot.support import give_support, parse_kind, support_bonus
from bot.war import army_power
from test_game import FakeGM

CHAT = -100


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "m.db"))
    await database.connect()
    await new_game(database, CHAT, "Тест", 1)
    yield database
    await database.close()


async def take(db, user_id, code):
    c = await db.get_country_by_code(CHAT, code)
    await db.assign_player(c["id"], user_id, f"player{user_id}", {**c})
    return await db.get_country(c["id"])


class MemoryGM(FakeGM):
    def __init__(self, support=None, **kw):
        super().__init__(**kw)
        self.support = support or {"kind": "none", "amount_bn": 0}
        self.contexts = []

    async def npc_respond(self, npc, speaker, text, context, *, private):
        self.contexts.append(context)
        return {"reply": f"{npc['name']} помнит: {len(context['conversation'])} реплик", "notes": f"Обещали помочь {speaker['name']}",
                "trust_delta": 5, "support": self.support}

    async def interpret_treaty(self, text, a, b, status):
        return {"ends_war": "мир" in text.casefold(), "alliance": False, "non_aggression": False, "summary": text}


async def test_signed_peace_treaty_ends_war(db):
    ukr, rus = await take(db, 1, "UKR"), await take(db, 2, "RUS")
    await declare_war(db, FakeGM(), CHAT, rus, ukr)
    tid = await db.add_treaty(CHAT, 1, "general", ukr["id"], rus["id"], "Мир и прекращение огня")
    effects = await apply_treaty(db, await db.get_treaty(tid), MemoryGM())
    assert (await db.get_relation(CHAT, ukr["id"], rus["id"]))["status"] == "peace"
    assert all(g["target_id"] is None for g in await db.list_generals(CHAT))
    assert any("Война окончена" in e for e in effects)
    # without AI the keyword fallback still recognises peace
    await db.change_relation(CHAT, ukr["id"], rus["id"], 0, "war")
    tid = await db.add_treaty(CHAT, 1, "general", ukr["id"], rus["id"], "Перемирие на 90 дней")
    await apply_treaty(db, await db.get_treaty(tid), None)
    assert (await db.get_relation(CHAT, ukr["id"], rus["id"]))["status"] == "peace"


async def test_turn_ai_cannot_flip_war(db):
    ukr, rus, chn = await take(db, 1, "UKR"), await take(db, 2, "RUS"), await take(db, 3, "CHN")
    await declare_war(db, FakeGM(), CHAT, rus, ukr)

    class FlipGM(FakeGM):
        async def resolve_turn(self, world, **kw):
            out = await super().resolve_turn(world, **kw)
            out["relations"] = [
                {"a_id": ukr["id"], "b_id": rus["id"], "delta": 5, "status": "peace", "reason": "x"},
                {"a_id": ukr["id"], "b_id": chn["id"], "delta": -5, "status": "war", "reason": "x"},
            ]
            return out

        async def plan_operations(self, briefs):
            return []

    await resolve_turn(db, FlipGM(), CHAT, rng=random.Random(1))
    assert (await db.get_relation(CHAT, ukr["id"], rus["id"]))["status"] == "war"
    assert (await db.get_relation(CHAT, ukr["id"], chn["id"]))["status"] == "peace"


async def test_supports_have_effects(db):
    ukr, usa = await take(db, 1, "UKR"), await db.get_country_by_code(CHAT, "USA")
    ok, effect = await give_support(db, CHAT, 1, usa, ukr, "weapons", 5, "ПВО")
    assert ok and "армия" in effect
    assert (await db.get_country(ukr["id"]))["military"] > ukr["military"]
    assert (await db.get_country(usa["id"]))["budget"] == round(usa["budget"] - 5, 1)
    ok, _ = await give_support(db, CHAT, 1, ukr, usa, "money", 10_000, "")
    assert not ok
    ok, _ = await give_support(db, CHAT, 1, usa, ukr, "troops", 0, "", secret=True)
    active = await db.active_supports(CHAT)
    bonus = support_bonus(active, {usa["id"]: usa["military"]})
    assert 0 < bonus[ukr["id"]] <= 0.6
    c = await db.get_country(ukr["id"])
    assert army_power({**c, "support_bonus": bonus[ukr["id"]]}) > army_power(c)
    await db.tick_supports(CHAT)
    await db.tick_supports(CHAT)
    assert await db.active_supports(CHAT) == []
    assert [parse_kind(x) for x in ("оружие", "деньги", "войска", "разведка", "вакцины", "???")] == \
        ["weapons", "money", "troops", "intel", "humanitarian", None]


async def test_npc_remembers_and_gives_support(db):
    ukr = await take(db, 1, "UKR")
    blr = await db.get_country_by_code(CHAT, "BLR")
    gm = MemoryGM(support={"kind": "humanitarian", "amount_bn": 1})
    reply, support = await npc_answer(db, gm, CHAT, blr, ukr, "Давайте дружить", private=True)
    assert "0 реплик" in reply and support and "гуманитар" in support
    blr = await db.get_country(blr["id"])
    assert blr["notes"].startswith("Обещали помочь")
    assert (await db.get_relation(CHAT, ukr["id"], blr["id"]))["value"] >= 10
    reply, _ = await npc_answer(db, gm, CHAT, blr, ukr, "Помнишь меня?", private=False)
    assert "3 реплик" in reply  # question, answer and the support it gave
    ctx = await npc_context(db, CHAT, await db.get_country(blr["id"]), ukr)
    assert [m["text"] for m in ctx["conversation"]][:1] == ["Давайте дружить"] and ctx["your_notes"]


async def test_turn_reports_orders_and_npc_memory(db):
    ukr = await take(db, 1, "UKR")
    blr = await db.get_country_by_code(CHAT, "BLR")
    await db.add_action(CHAT, 1, ukr["id"], "reform", "Налоговая реформа")

    class RichGM(FakeGM):
        async def resolve_turn(self, world, **kw):
            assert "chronicle_summary" in world and "npc_notes" in world and "supports" in world
            out = await super().resolve_turn(world, **kw)
            for c in out["countries"]:
                c["orders"] = [{"order": "Налоговая реформа", "result": "Сборы +8%", "outcome": "success"}]
            out["chronicle_summary"] = "Ход 1: Украина провела налоговую реформу."
            out["npc_notes"] = [{"country_id": blr["id"], "notes": "Следим за Украиной"}]
            out["npc_support"] = [{"from_id": blr["id"], "to_id": ukr["id"], "kind": "money", "amount_bn": 1, "reason": "дружба"}]
            return out

    outcome = await resolve_turn(db, RichGM(), CHAT)
    assert "✅ Налоговая реформа — Сборы +8%" in outcome.private_reports[ukr["id"]]
    assert outcome.support_lines and "Белоруссия" in outcome.support_lines[0]
    assert (await db.get_game(CHAT))["history"].startswith("Ход 1")
    assert (await db.get_country(blr["id"]))["notes"] == "Следим за Украиной"
    snap = await world_snapshot(db, CHAT)
    assert snap["chronicle_summary"].startswith("Ход 1")


async def test_export_hides_others_secrets(db):
    ukr, rus = await take(db, 1, "UKR"), await take(db, 2, "RUS")
    await db.add_action(CHAT, 1, ukr["id"], "secret", "ТАЙНО подкупить генералов")
    await db.add_action(CHAT, 1, rus["id"], "reform", "Публичная реформа")
    public = await build_history(db, CHAT, 2026)
    mine = await build_history(db, CHAT, 2026, viewer_id=ukr["id"])
    assert "Публичная реформа" in public and "ТАЙНО" not in public
    assert "ТАЙНО" in mine


def test_admin_value_parsing():
    assert _parse_value("50", 10) == 50
    assert _parse_value("+5", 10) == 15
    assert _parse_value("-3", 10) == 7
    assert _parse_value("+20%", 100) == 120
    assert _parse_value("abc", 10) is None


async def test_upgrade_from_v3_keeps_game_and_repairs_peace(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    await db.connect()
    await new_game(db, CHAT, "Старая", 1)
    ukr, rus = await take(db, 1, "UKR"), await take(db, 2, "RUS")
    await declare_war(db, FakeGM(), CHAT, rus, ukr)
    tid = await db.add_treaty(CHAT, 1, "general", ukr["id"], rus["id"], "Мир")
    await db.sign_treaty(tid, "b")
    await db.set_treaty_status(tid, "signed")
    for sql in ("ALTER TABLE countries DROP COLUMN notes", "ALTER TABLE games DROP COLUMN history",
                "DROP TABLE memory", "DROP TABLE supports", "PRAGMA user_version = 3"):
        await db.conn.execute(sql)
    await db.conn.commit()
    await db.close()

    db = Database(path)
    await db.connect()
    assert db.migrated_from == 3
    assert (await db.get_country_by_user(CHAT, 1))["name"] == "Украина"
    assert (await db.get_game(CHAT))["history"] == ""
    assert await repair_signed_peace(db) == [(CHAT, ukr["id"], rus["id"])] or \
        await db.wars(CHAT) == []
    assert await db.wars(CHAT) == []
    await db.close()
    import glob
    assert glob.glob(path + ".schema-v3-*.bak")
