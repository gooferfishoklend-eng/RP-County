import json

import aiosqlite

from bot.geo import name_key
from bot.stats import STAT_KEYS

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    chat_id        INTEGER PRIMARY KEY,
    title          TEXT,
    turn           INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active',
    created_by     INTEGER,
    npc_chat       INTEGER NOT NULL DEFAULT 1,
    random_events  INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS countries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    user_id      INTEGER,
    player_name  TEXT,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL,
    flag         TEXT,
    government   TEXT,
    leader_title TEXT,
    description  TEXT,
    gdp          REAL NOT NULL,
    budget       REAL NOT NULL,
    population   REAL NOT NULL,
    stability    INTEGER NOT NULL,
    approval     INTEGER NOT NULL,
    military     INTEGER NOT NULL,
    tech         INTEGER NOT NULL,
    corruption   INTEGER NOT NULL,
    influence    INTEGER NOT NULL,
    capital_cell INTEGER,
    alive        INTEGER NOT NULL DEFAULT 1,
    ready_turn   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, code),
    UNIQUE (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_countries_name ON countries (chat_id, name_key);

CREATE TABLE IF NOT EXISTS cells (
    chat_id   INTEGER NOT NULL,
    cell_id   INTEGER NOT NULL,
    owner_id  INTEGER NOT NULL,
    core_id   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, cell_id)
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    turn        INTEGER NOT NULL,
    country_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    target      TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS relations (
    chat_id  INTEGER NOT NULL,
    a_id     INTEGER NOT NULL,
    b_id     INTEGER NOT NULL,
    value    INTEGER NOT NULL DEFAULT 0,
    status   TEXT NOT NULL DEFAULT 'peace',
    PRIMARY KEY (chat_id, a_id, b_id)
);

CREATE TABLE IF NOT EXISTS treaties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    turn        INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    a_id        INTEGER NOT NULL,
    b_id        INTEGER NOT NULL,
    text        TEXT NOT NULL,
    peace_mode  TEXT,
    transfers   TEXT NOT NULL DEFAULT '[]',
    signed_a    INTEGER NOT NULL DEFAULT 0,
    signed_b    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS resolutions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    turn        INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS votes (
    resolution_id INTEGER NOT NULL,
    country_id    INTEGER NOT NULL,
    vote          TEXT NOT NULL,
    PRIMARY KEY (resolution_id, country_id)
);

CREATE TABLE IF NOT EXISTS chronicle (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    turn      INTEGER NOT NULL,
    headline  TEXT NOT NULL,
    news      TEXT NOT NULL,
    event     TEXT
);

CREATE TABLE IF NOT EXISTS generals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    country_id     INTEGER NOT NULL,
    name           TEXT NOT NULL,
    rank           TEXT NOT NULL,
    trait          TEXT NOT NULL,
    skill          INTEGER NOT NULL,
    xp             INTEGER NOT NULL DEFAULT 0,
    directive      TEXT,
    target_id      INTEGER,
    position_cell  INTEGER,
    alive          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    description   TEXT NOT NULL,
    severity      INTEGER NOT NULL,
    affected      TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'active',
    started_turn  INTEGER NOT NULL,
    updated_turn  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_chat_id   INTEGER PRIMARY KEY,
    game_chat_id   INTEGER NOT NULL,
    title          TEXT,
    conference_id  INTEGER
);

CREATE TABLE IF NOT EXISTS conferences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    room_chat_id   INTEGER NOT NULL,
    topic          TEXT NOT NULL,
    initiator_id   INTEGER NOT NULL,
    invite_link    TEXT,
    status         TEXT NOT NULL DEFAULT 'open',
    draft          TEXT,
    draft_version  INTEGER NOT NULL DEFAULT 0,
    turn           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conference_members (
    conference_id  INTEGER NOT NULL,
    country_id     INTEGER NOT NULL,
    user_id        INTEGER,
    joined         INTEGER NOT NULL DEFAULT 0,
    signed         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conference_id, country_id)
);

CREATE TABLE IF NOT EXISTS conference_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    conference_id  INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    country_id     INTEGER,
    speaker        TEXT NOT NULL,
    text           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id         INTEGER PRIMARY KEY,
    active_chat_id  INTEGER
);
"""

GAME_TABLES = ("countries", "cells", "actions", "relations", "treaties", "resolutions", "chronicle", "generals", "events",
               "conferences")
ALL_TABLES = ("games", *GAME_TABLES, "votes", "user_prefs", "proposals", "rooms", "conference_members", "conference_log")

COUNTRY_COLS = ["chat_id", "code", "user_id", "player_name", "name", "name_key", "flag", "government", "leader_title",
                "description", *STAT_KEYS, "capital_cell"]


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _event_row(row: dict | None) -> dict | None:
    if row:
        row["affected"] = json.loads(row["affected"])
    return row


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        async with self.conn.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]
        if version != SCHEMA_VERSION:
            for table in ALL_TABLES:
                await self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        await self.conn.executescript(SCHEMA)
        await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _exec(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    # games
    async def get_game(self, chat_id: int) -> dict | None:
        return await self._one("SELECT * FROM games WHERE chat_id = ?", (chat_id,))

    async def create_game(self, chat_id: int, title: str, user_id: int, countries: list[dict],
                          cells: list[tuple[int, str]]) -> None:
        """Reset the chat's game and seed every country (as NPC) and every map cell."""
        for table in GAME_TABLES:
            await self.conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
        await self.conn.execute(
            "INSERT OR REPLACE INTO games (chat_id, title, turn, status, created_by) VALUES (?, ?, 1, 'active', ?)",
            (chat_id, title, user_id),
        )
        sql = f"INSERT INTO countries ({', '.join(COUNTRY_COLS)}) VALUES ({', '.join('?' * len(COUNTRY_COLS))})"
        rows = []
        for c in countries:
            data = {**c, "chat_id": chat_id, "user_id": None, "player_name": None, "name_key": name_key(c["name"])}
            rows.append(tuple(data[k] for k in COUNTRY_COLS))
        await self.conn.executemany(sql, rows)
        ids = {r["code"]: r["id"] for r in await self._all("SELECT id, code FROM countries WHERE chat_id = ?", (chat_id,))}
        await self.conn.executemany(
            "INSERT INTO cells (chat_id, cell_id, owner_id, core_id) VALUES (?, ?, ?, ?)",
            [(chat_id, cell_id, ids[code], ids[code]) for cell_id, code in cells if code in ids],
        )
        await self.conn.commit()

    async def set_game_status(self, chat_id: int, status: str) -> None:
        await self._exec("UPDATE games SET status = ? WHERE chat_id = ?", (status, chat_id))

    async def set_game_flag(self, chat_id: int, flag: str, value: bool) -> None:
        if flag not in ("npc_chat", "random_events"):
            raise ValueError(flag)
        await self._exec(f"UPDATE games SET {flag} = ? WHERE chat_id = ?", (int(value), chat_id))

    async def advance_turn(self, chat_id: int) -> None:
        await self._exec("UPDATE games SET turn = turn + 1 WHERE chat_id = ?", (chat_id,))

    # countries
    async def get_country(self, country_id: int) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE id = ?", (country_id,))

    async def get_country_by_user(self, chat_id: int, user_id: int) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

    async def get_country_by_code(self, chat_id: int, code: str) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE chat_id = ? AND code = ?", (chat_id, code))

    async def list_countries(self, chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM countries WHERE chat_id = ? ORDER BY id", (chat_id,))

    async def list_players(self, chat_id: int) -> list[dict]:
        return await self._all(
            "SELECT * FROM countries WHERE chat_id = ? AND user_id IS NOT NULL AND alive = 1 ORDER BY id", (chat_id,)
        )

    async def assign_player(self, country_id: int, user_id: int, player_name: str, profile: dict) -> None:
        stats = {k: profile[k] for k in STAT_KEYS}
        sets = ", ".join(f"{k} = ?" for k in [*stats, "government", "leader_title", "description"])
        await self._exec(
            f"UPDATE countries SET user_id = ?, player_name = ?, ready_turn = 0, {sets} WHERE id = ?",
            (user_id, player_name, *stats.values(), profile["government"], profile["leader_title"],
             profile["description"], country_id),
        )

    async def release_player(self, country_id: int) -> None:
        await self._exec("UPDATE countries SET user_id = NULL, player_name = NULL WHERE id = ?", (country_id,))

    async def update_country_stats(self, country_id: int, stats: dict) -> None:
        keys = [k for k in stats if k in STAT_KEYS]
        if not keys:
            return
        sql = f"UPDATE countries SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?"
        await self._exec(sql, tuple(stats[k] for k in keys) + (country_id,))

    async def set_capital(self, country_id: int, cell_id: int) -> None:
        await self._exec("UPDATE countries SET capital_cell = ? WHERE id = ?", (cell_id, country_id))

    async def eliminate(self, country_id: int) -> None:
        await self._exec("UPDATE countries SET alive = 0, user_id = NULL WHERE id = ?", (country_id,))
        await self._exec("UPDATE generals SET alive = 0 WHERE country_id = ?", (country_id,))

    async def set_ready(self, country_id: int, turn: int) -> None:
        await self._exec("UPDATE countries SET ready_turn = ? WHERE id = ?", (turn, country_id))

    # cells
    async def cell_owners(self, chat_id: int) -> tuple[dict[int, int], dict[int, int]]:
        rows = await self._all("SELECT cell_id, owner_id, core_id FROM cells WHERE chat_id = ?", (chat_id,))
        return {r["cell_id"]: r["owner_id"] for r in rows}, {r["cell_id"]: r["core_id"] for r in rows}

    async def set_cell_owner(self, chat_id: int, cell_id: int, owner_id: int, core_id: int | None = None) -> None:
        if core_id is None:
            await self.conn.execute("UPDATE cells SET owner_id = ? WHERE chat_id = ? AND cell_id = ?",
                                    (owner_id, chat_id, cell_id))
        else:
            await self.conn.execute("UPDATE cells SET owner_id = ?, core_id = ? WHERE chat_id = ? AND cell_id = ?",
                                    (owner_id, core_id, chat_id, cell_id))
        await self.conn.commit()

    # actions
    async def add_action(self, chat_id: int, turn: int, country_id: int, kind: str, text: str, target: str | None = None) -> int:
        return await self._exec(
            "INSERT INTO actions (chat_id, turn, country_id, kind, text, target) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, turn, country_id, kind, text, target),
        )

    async def list_actions(self, chat_id: int, turn: int, country_id: int | None = None) -> list[dict]:
        if country_id is None:
            return await self._all("SELECT * FROM actions WHERE chat_id = ? AND turn = ? ORDER BY id", (chat_id, turn))
        return await self._all(
            "SELECT * FROM actions WHERE chat_id = ? AND turn = ? AND country_id = ? ORDER BY id",
            (chat_id, turn, country_id),
        )

    async def delete_last_action(self, chat_id: int, turn: int, country_id: int) -> dict | None:
        row = await self._one(
            "SELECT * FROM actions WHERE chat_id = ? AND turn = ? AND country_id = ? "
            "AND kind NOT IN ('treaty', 'npc_deal') ORDER BY id DESC LIMIT 1",
            (chat_id, turn, country_id),
        )
        if row:
            await self._exec("DELETE FROM actions WHERE id = ?", (row["id"],))
        return row

    # relations
    async def get_relation(self, chat_id: int, a: int, b: int) -> dict:
        a, b = _pair(a, b)
        row = await self._one("SELECT * FROM relations WHERE chat_id = ? AND a_id = ? AND b_id = ?", (chat_id, a, b))
        return row or {"chat_id": chat_id, "a_id": a, "b_id": b, "value": 0, "status": "peace"}

    async def change_relation(self, chat_id: int, a: int, b: int, delta: int, status: str | None = None) -> dict:
        rel = await self.get_relation(chat_id, a, b)
        value = max(-100, min(100, rel["value"] + delta))
        new_status = status or rel["status"]
        await self._exec(
            "INSERT OR REPLACE INTO relations (chat_id, a_id, b_id, value, status) VALUES (?, ?, ?, ?, ?)",
            (chat_id, rel["a_id"], rel["b_id"], value, new_status),
        )
        return {**rel, "value": value, "status": new_status}

    async def list_relations(self, chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM relations WHERE chat_id = ?", (chat_id,))

    async def wars(self, chat_id: int) -> list[tuple[int, int]]:
        rows = await self._all("SELECT a_id, b_id FROM relations WHERE chat_id = ? AND status = 'war'", (chat_id,))
        return [(r["a_id"], r["b_id"]) for r in rows]

    # treaties
    async def add_treaty(self, chat_id: int, turn: int, kind: str, a_id: int, b_id: int, text: str,
                         peace_mode: str | None = None, transfers: list | None = None) -> int:
        return await self._exec(
            "INSERT INTO treaties (chat_id, turn, kind, a_id, b_id, text, peace_mode, transfers, signed_a) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (chat_id, turn, kind, a_id, b_id, text, peace_mode, json.dumps(transfers or [])),
        )

    async def get_treaty(self, treaty_id: int) -> dict | None:
        row = await self._one("SELECT * FROM treaties WHERE id = ?", (treaty_id,))
        if row:
            row["transfers"] = json.loads(row["transfers"])
        return row

    async def sign_treaty(self, treaty_id: int, side: str) -> None:
        col = "signed_a" if side == "a" else "signed_b"
        await self._exec(f"UPDATE treaties SET {col} = 1 WHERE id = ?", (treaty_id,))

    async def set_treaty_status(self, treaty_id: int, status: str) -> None:
        await self._exec("UPDATE treaties SET status = ? WHERE id = ?", (status, treaty_id))

    # resolutions
    async def add_resolution(self, chat_id: int, turn: int, author_id: int, text: str) -> int:
        return await self._exec(
            "INSERT INTO resolutions (chat_id, turn, author_id, text) VALUES (?, ?, ?, ?)",
            (chat_id, turn, author_id, text),
        )

    async def get_resolution(self, resolution_id: int) -> dict | None:
        return await self._one("SELECT * FROM resolutions WHERE id = ?", (resolution_id,))

    async def open_resolutions(self, chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM resolutions WHERE chat_id = ? AND status = 'open'", (chat_id,))

    async def close_resolution(self, resolution_id: int, status: str) -> None:
        await self._exec("UPDATE resolutions SET status = ? WHERE id = ?", (status, resolution_id))

    async def cast_vote(self, resolution_id: int, country_id: int, vote: str) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO votes (resolution_id, country_id, vote) VALUES (?, ?, ?)",
            (resolution_id, country_id, vote),
        )

    async def tally(self, resolution_id: int) -> dict[str, int]:
        rows = await self._all("SELECT vote, COUNT(*) AS n FROM votes WHERE resolution_id = ? GROUP BY vote", (resolution_id,))
        result = {"yes": 0, "no": 0, "abstain": 0}
        for r in rows:
            result[r["vote"]] = r["n"]
        return result

    # chronicle
    async def add_chronicle(self, chat_id: int, turn: int, headline: str, news: str, event: str) -> None:
        await self._exec(
            "INSERT INTO chronicle (chat_id, turn, headline, news, event) VALUES (?, ?, ?, ?, ?)",
            (chat_id, turn, headline, news, event),
        )

    async def recent_chronicle(self, chat_id: int, limit: int = 3) -> list[dict]:
        rows = await self._all("SELECT * FROM chronicle WHERE chat_id = ? ORDER BY turn DESC LIMIT ?", (chat_id, limit))
        return list(reversed(rows))

    # generals
    async def add_general(self, chat_id: int, country_id: int, name: str, rank: str, trait: str, skill: int,
                          position_cell: int | None) -> int:
        return await self._exec(
            "INSERT INTO generals (chat_id, country_id, name, rank, trait, skill, position_cell) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, country_id, name, rank, trait, skill, position_cell),
        )

    async def list_generals(self, chat_id: int, country_id: int | None = None) -> list[dict]:
        if country_id is None:
            return await self._all("SELECT * FROM generals WHERE chat_id = ? AND alive = 1 ORDER BY id", (chat_id,))
        return await self._all("SELECT * FROM generals WHERE chat_id = ? AND country_id = ? AND alive = 1 ORDER BY id",
                               (chat_id, country_id))

    async def get_general(self, general_id: int) -> dict | None:
        return await self._one("SELECT * FROM generals WHERE id = ?", (general_id,))

    async def update_general(self, general_id: int, **fields) -> None:
        allowed = {"directive", "target_id", "position_cell", "xp", "skill", "alive", "name", "rank", "trait"}
        keys = [k for k in fields if k in allowed]
        sql = f"UPDATE generals SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?"
        await self._exec(sql, tuple(fields[k] for k in keys) + (general_id,))

    # events
    async def add_event(self, chat_id: int, turn: int, name: str, kind: str, description: str, severity: int,
                        affected: list[int]) -> int:
        return await self._exec(
            "INSERT INTO events (chat_id, name, kind, description, severity, affected, started_turn, updated_turn) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, name, kind, description, severity, json.dumps(affected), turn, turn),
        )

    async def update_event(self, event_id: int, turn: int, description: str, severity: int, affected: list[int],
                           status: str) -> None:
        await self._exec(
            "UPDATE events SET description = ?, severity = ?, affected = ?, status = ?, updated_turn = ? WHERE id = ?",
            (description, severity, json.dumps(affected), status, turn, event_id),
        )

    async def active_events(self, chat_id: int) -> list[dict]:
        rows = await self._all("SELECT * FROM events WHERE chat_id = ? AND status = 'active' ORDER BY id", (chat_id,))
        return [_event_row(r) for r in rows]

    async def signed_agreements(self, chat_id: int, limit: int = 8) -> list[dict]:
        treaties = await self._all(
            "SELECT * FROM treaties WHERE chat_id = ? AND status = 'signed' ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )
        confs = await self._all(
            "SELECT * FROM conferences WHERE chat_id = ? AND status = 'signed' ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )
        out = [{"kind": t["kind"], "parties": [t["a_id"], t["b_id"]], "text": t["text"], "turn": t["turn"]} for t in treaties]
        for c in confs:
            terms = json.loads(c["draft"] or "{}")
            members = await self.conference_members(c["id"])
            out.append({"kind": "conference", "parties": [m["country_id"] for m in members], "turn": c["turn"],
                        "title": terms.get("title"), "text": terms.get("summary"), "clauses": terms.get("clauses", [])})
        return sorted(out, key=lambda x: -x["turn"])[:limit]

    # conference rooms
    async def add_room(self, room_chat_id: int, game_chat_id: int, title: str) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO rooms (room_chat_id, game_chat_id, title, conference_id) VALUES (?, ?, ?, NULL)",
            (room_chat_id, game_chat_id, title),
        )

    async def get_room(self, room_chat_id: int) -> dict | None:
        return await self._one("SELECT * FROM rooms WHERE room_chat_id = ?", (room_chat_id,))

    async def list_rooms(self, game_chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM rooms WHERE game_chat_id = ?", (game_chat_id,))

    async def set_room_conference(self, room_chat_id: int, conference_id: int | None) -> None:
        await self._exec("UPDATE rooms SET conference_id = ? WHERE room_chat_id = ?", (conference_id, room_chat_id))

    # conferences
    async def create_conference(self, chat_id: int, room_chat_id: int, topic: str, initiator_id: int, turn: int,
                                members: list[tuple[int, int | None]]) -> int:
        cid = await self._exec(
            "INSERT INTO conferences (chat_id, room_chat_id, topic, initiator_id, turn) VALUES (?, ?, ?, ?, ?)",
            (chat_id, room_chat_id, topic, initiator_id, turn),
        )
        await self.conn.executemany(
            "INSERT INTO conference_members (conference_id, country_id, user_id, joined) VALUES (?, ?, ?, ?)",
            [(cid, country_id, user_id, 0 if user_id else 1) for country_id, user_id in members],
        )
        await self.conn.commit()
        return cid

    async def get_conference(self, conference_id: int) -> dict | None:
        row = await self._one("SELECT * FROM conferences WHERE id = ?", (conference_id,))
        if row:
            row["draft"] = json.loads(row["draft"]) if row["draft"] else None
        return row

    async def open_conferences(self, chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM conferences WHERE chat_id = ? AND status = 'open'", (chat_id,))

    async def update_conference(self, conference_id: int, **fields) -> None:
        allowed = {"invite_link", "status", "draft", "draft_version"}
        keys = [k for k in fields if k in allowed]
        values = [json.dumps(fields[k], ensure_ascii=False) if k == "draft" else fields[k] for k in keys]
        await self._exec(f"UPDATE conferences SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                         (*values, conference_id))

    async def conference_members(self, conference_id: int) -> list[dict]:
        return await self._all("SELECT * FROM conference_members WHERE conference_id = ? ORDER BY country_id",
                               (conference_id,))

    async def set_member(self, conference_id: int, country_id: int, **fields) -> None:
        keys = [k for k in fields if k in ("joined", "signed")]
        await self._exec(
            f"UPDATE conference_members SET {', '.join(f'{k} = ?' for k in keys)} WHERE conference_id = ? AND country_id = ?",
            (*(fields[k] for k in keys), conference_id, country_id),
        )

    async def reset_signatures(self, conference_id: int) -> None:
        await self._exec("UPDATE conference_members SET signed = 0 WHERE conference_id = ?", (conference_id,))

    async def log_conference(self, conference_id: int, message_id: int, kind: str, country_id: int | None,
                             speaker: str, text: str) -> None:
        await self._exec(
            "INSERT INTO conference_log (conference_id, message_id, kind, country_id, speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
            (conference_id, message_id, kind, country_id, speaker, text),
        )

    async def conference_log(self, conference_id: int) -> list[dict]:
        return await self._all("SELECT * FROM conference_log WHERE conference_id = ? ORDER BY id", (conference_id,))

    # group -> supergroup migration (Telegram changes the chat id)
    async def migrate_chat(self, old_id: int, new_id: int) -> bool:
        """Move every record of chat old_id to new_id. Returns True if anything moved."""
        if old_id == new_id:
            return False
        moved = False
        if await self.get_game(old_id) and not await self.get_game(new_id):
            for table in ("games", *GAME_TABLES):
                await self.conn.execute(f"UPDATE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
            await self.conn.execute("UPDATE user_prefs SET active_chat_id = ? WHERE active_chat_id = ?", (new_id, old_id))
            await self.conn.execute("UPDATE rooms SET game_chat_id = ? WHERE game_chat_id = ?", (new_id, old_id))
            moved = True
        if await self.get_room(old_id) and not await self.get_room(new_id):
            await self.conn.execute("UPDATE rooms SET room_chat_id = ? WHERE room_chat_id = ?", (new_id, old_id))
            await self.conn.execute("UPDATE conferences SET room_chat_id = ? WHERE room_chat_id = ?", (new_id, old_id))
            moved = True
        await self.conn.commit()
        return moved

    async def basic_group_game_ids(self) -> list[int]:
        """Games stored under a basic-group id (supergroups are <= -100xxxxxxxxxx) that may have been migrated."""
        rows = await self._all("SELECT chat_id FROM games WHERE chat_id < 0 AND chat_id > -1000000000000")
        return [r["chat_id"] for r in rows]

    # user prefs
    async def set_active_chat(self, user_id: int, chat_id: int) -> None:
        await self._exec("INSERT OR REPLACE INTO user_prefs (user_id, active_chat_id) VALUES (?, ?)", (user_id, chat_id))

    async def get_active_chat(self, user_id: int) -> int | None:
        row = await self._one("SELECT active_chat_id FROM user_prefs WHERE user_id = ?", (user_id,))
        return row["active_chat_id"] if row else None

    async def user_games(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT c.*, g.title AS game_title FROM countries c JOIN games g ON g.chat_id = c.chat_id "
            "WHERE c.user_id = ? AND g.status = 'active'",
            (user_id,),
        )
