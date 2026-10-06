import aiosqlite

from bot.stats import STAT_KEYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    turn        INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'active',
    created_by  INTEGER,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS countries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
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
    ready_turn   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, user_id),
    UNIQUE (chat_id, name_key)
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

CREATE TABLE IF NOT EXISTS proposals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    turn       INTEGER NOT NULL,
    from_id    INTEGER NOT NULL,
    to_id      INTEGER NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
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

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id         INTEGER PRIMARY KEY,
    active_chat_id  INTEGER
);
"""


def name_key(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
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

    async def create_game(self, chat_id: int, title: str, user_id: int) -> None:
        await self.conn.execute("DELETE FROM countries WHERE chat_id = ?", (chat_id,))
        for table in ("actions", "relations", "proposals", "resolutions", "chronicle"):
            await self.conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
        await self.conn.execute(
            "INSERT OR REPLACE INTO games (chat_id, title, turn, status, created_by) VALUES (?, ?, 1, 'active', ?)",
            (chat_id, title, user_id),
        )
        await self.conn.commit()

    async def set_game_status(self, chat_id: int, status: str) -> None:
        await self._exec("UPDATE games SET status = ? WHERE chat_id = ?", (status, chat_id))

    async def advance_turn(self, chat_id: int) -> None:
        await self._exec("UPDATE games SET turn = turn + 1 WHERE chat_id = ?", (chat_id,))

    # countries
    async def add_country(self, chat_id: int, user_id: int, player_name: str, data: dict) -> int:
        cols = ["chat_id", "user_id", "player_name", "name_key", "name", "flag", "government", "leader_title", "description", *STAT_KEYS]
        values = [chat_id, user_id, player_name, name_key(data["name"])] + [data[c] for c in cols[4:]]
        sql = f"INSERT INTO countries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        return await self._exec(sql, tuple(values))

    async def get_country(self, country_id: int) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE id = ?", (country_id,))

    async def get_country_by_user(self, chat_id: int, user_id: int) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

    async def get_country_by_name(self, chat_id: int, name: str) -> dict | None:
        return await self._one("SELECT * FROM countries WHERE chat_id = ? AND name_key = ?", (chat_id, name_key(name)))

    async def list_countries(self, chat_id: int) -> list[dict]:
        return await self._all("SELECT * FROM countries WHERE chat_id = ? ORDER BY id", (chat_id,))

    async def update_country_stats(self, country_id: int, stats: dict) -> None:
        keys = [k for k in stats if k in STAT_KEYS]
        sql = f"UPDATE countries SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?"
        await self._exec(sql, tuple(stats[k] for k in keys) + (country_id,))

    async def delete_country(self, country_id: int) -> None:
        await self.conn.execute("DELETE FROM countries WHERE id = ?", (country_id,))
        await self.conn.execute("DELETE FROM relations WHERE a_id = ? OR b_id = ?", (country_id, country_id))
        await self.conn.execute("DELETE FROM actions WHERE country_id = ?", (country_id,))
        await self.conn.commit()

    async def set_ready(self, country_id: int, turn: int) -> None:
        await self._exec("UPDATE countries SET ready_turn = ? WHERE id = ?", (turn, country_id))

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
            "SELECT * FROM actions WHERE chat_id = ? AND turn = ? AND country_id = ? ORDER BY id DESC LIMIT 1",
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

    # proposals
    async def add_proposal(self, chat_id: int, turn: int, from_id: int, to_id: int, text: str) -> int:
        return await self._exec(
            "INSERT INTO proposals (chat_id, turn, from_id, to_id, text) VALUES (?, ?, ?, ?, ?)",
            (chat_id, turn, from_id, to_id, text),
        )

    async def get_proposal(self, proposal_id: int) -> dict | None:
        return await self._one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))

    async def set_proposal_status(self, proposal_id: int, status: str) -> None:
        await self._exec("UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id))

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
        rows = await self._all(
            "SELECT * FROM chronicle WHERE chat_id = ? ORDER BY turn DESC LIMIT ?", (chat_id, limit)
        )
        return list(reversed(rows))

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
