"""SQLite 存储层。

第一版使用 aiosqlite。所有表结构变更通过 migrations 管理，
plugin_data 目录由 AstrBot StarTools 提供。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from ..models.player import Binding
from ..models.presence import PlayerPresence, PresenceState

SCHEMA_VERSION = 1

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS bindings (
            qq_id             TEXT NOT NULL,
            puuid             TEXT NOT NULL,
            game_name         TEXT NOT NULL DEFAULT '',
            tag_line          TEXT NOT NULL DEFAULT '',
            platform          TEXT NOT NULL DEFAULT '',
            region            TEXT NOT NULL DEFAULT 'cn',
            enabled           INTEGER NOT NULL DEFAULT 1,
            broadcast_enabled INTEGER NOT NULL DEFAULT 1,
            created_at        REAL NOT NULL DEFAULT 0,
            updated_at        REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (qq_id, puuid)
        );
        CREATE INDEX IF NOT EXISTS idx_bindings_puuid ON bindings(puuid);

        CREATE TABLE IF NOT EXISTS groups (
            group_id                  TEXT PRIMARY KEY,
            umo                       TEXT NOT NULL DEFAULT '',
            enabled                   INTEGER NOT NULL DEFAULT 1,
            online_notification       INTEGER NOT NULL DEFAULT 1,
            game_start_notification   INTEGER NOT NULL DEFAULT 1,
            game_end_notification     INTEGER NOT NULL DEFAULT 1,
            online_cooldown_minutes   INTEGER NOT NULL DEFAULT -1,
            updated_at                REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS presence_state (
            puuid         TEXT PRIMARY KEY,
            qq_id         TEXT NOT NULL DEFAULT '',
            state         TEXT NOT NULL DEFAULT 'UNKNOWN',
            raw_game_status TEXT NOT NULL DEFAULT '',
            availability  TEXT NOT NULL DEFAULT '',
            champion_id   INTEGER,
            queue_id      INTEGER,
            game_id       TEXT,
            game_name     TEXT NOT NULL DEFAULT '',
            tag_line      TEXT NOT NULL DEFAULT '',
            platform      TEXT NOT NULL DEFAULT '',
            updated_at    REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS notification_state (
            puuid                   TEXT NOT NULL,
            group_id                TEXT NOT NULL,
            last_online_notify_at   REAL NOT NULL DEFAULT 0,
            last_game_id            TEXT NOT NULL DEFAULT '',
            last_end_game_id        TEXT NOT NULL DEFAULT '',
            updated_at              REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (puuid, group_id)
        );

        CREATE TABLE IF NOT EXISTS matches (
            game_id     TEXT NOT NULL,
            puuid       TEXT NOT NULL,
            raw_json    TEXT NOT NULL,
            end_time_ms INTEGER NOT NULL DEFAULT 0,
            fetched_at  REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (game_id, puuid)
        );
        CREATE INDEX IF NOT EXISTS idx_matches_puuid_time ON matches(puuid, end_time_ms DESC);
        """,
    ),
]


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "lol_presence.db"

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("PRAGMA user_version")
            (version,) = await cur.fetchone()
            for target, sql in _MIGRATIONS:
                if version < target:
                    await db.executescript(sql)
                    version = target
            await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await db.commit()

    # ---------------- bindings ----------------

    async def upsert_binding(self, b: Binding) -> None:
        now = time.time()
        b.created_at = b.created_at or now
        b.updated_at = now
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO bindings (qq_id, puuid, game_name, tag_line, platform,
                    region, enabled, broadcast_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qq_id, puuid) DO UPDATE SET
                    game_name=excluded.game_name,
                    tag_line=excluded.tag_line,
                    platform=excluded.platform,
                    enabled=excluded.enabled,
                    broadcast_enabled=excluded.broadcast_enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    b.qq_id,
                    b.puuid,
                    b.game_name,
                    b.tag_line,
                    b.platform,
                    b.region,
                    int(b.enabled),
                    int(b.broadcast_enabled),
                    b.created_at,
                    b.updated_at,
                ),
            )
            await db.commit()

    async def get_binding(self, qq_id: str) -> Binding | None:
        """一个 QQ 默认取最新一条绑定。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM bindings WHERE qq_id=? AND enabled=1 ORDER BY updated_at DESC LIMIT 1",
                (str(qq_id),),
            )
            row = await cur.fetchone()
            return Binding.from_row(row) if row else None

    async def get_binding_by_puuid(self, puuid: str) -> Binding | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM bindings WHERE puuid=? AND enabled=1 LIMIT 1", (puuid,)
            )
            row = await cur.fetchone()
            return Binding.from_row(row) if row else None

    async def find_binding_by_name(
        self, game_name: str, tag_line: str = ""
    ) -> Binding | None:
        """按展示名查找（大小写不敏感，仅展示用途）。"""
        q = "SELECT * FROM bindings WHERE enabled=1 AND lower(game_name)=lower(?)"
        args: list = [game_name]
        if tag_line:
            q += " AND lower(tag_line)=lower(?)"
            args.append(tag_line)
        q += " ORDER BY updated_at DESC LIMIT 1"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(q, args)
            row = await cur.fetchone()
            return Binding.from_row(row) if row else None

    async def delete_binding(self, qq_id: str, puuid: str | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            if puuid:
                cur = await db.execute(
                    "DELETE FROM bindings WHERE qq_id=? AND puuid=?",
                    (str(qq_id), puuid),
                )
            else:
                cur = await db.execute(
                    "DELETE FROM bindings WHERE qq_id=?", (str(qq_id),)
                )
            await db.commit()
            return cur.rowcount or 0

    async def all_bindings(self, only_enabled: bool = True) -> list[Binding]:
        q = "SELECT * FROM bindings"
        if only_enabled:
            q += " WHERE enabled=1"
        q += " ORDER BY updated_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(q)
            rows = await cur.fetchall()
            return [Binding.from_row(r) for r in rows]

    async def bindings_of_group(self, qq_ids: list[str]) -> list[Binding]:
        """按一组 QQ 号过滤的绑定（用于群内只显示本群成员）。"""
        if not qq_ids:
            return []
        marks = ",".join("?" * len(qq_ids))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM bindings WHERE enabled=1 AND qq_id IN ({marks})",
                [str(x) for x in qq_ids],
            )
            rows = await cur.fetchall()
            return [Binding.from_row(r) for r in rows]

    # ---------------- groups ----------------

    async def upsert_group(self, group_id: str, umo: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO groups (group_id, umo, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    umo=excluded.umo, updated_at=excluded.updated_at
                """,
                (str(group_id), umo, time.time()),
            )
            await db.commit()

    async def get_group(self, group_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM groups WHERE group_id=?", (str(group_id),)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def all_broadcast_groups(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM groups WHERE enabled=1 AND umo != ''")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_group_flag(self, group_id: str, column: str, value: int) -> None:
        allowed = {
            "enabled",
            "online_notification",
            "game_start_notification",
            "game_end_notification",
            "online_cooldown_minutes",
        }
        if column not in allowed:
            raise ValueError(f"illegal group column: {column}")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE groups SET {column}=?, updated_at=? WHERE group_id=?",
                (value, time.time(), str(group_id)),
            )
            await db.commit()

    # ---------------- presence state ----------------

    async def save_presence(self, p: PlayerPresence) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO presence_state (puuid, qq_id, state, raw_game_status,
                    availability, champion_id, queue_id, game_id, game_name, tag_line,
                    platform, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(puuid) DO UPDATE SET
                    qq_id=excluded.qq_id, state=excluded.state,
                    raw_game_status=excluded.raw_game_status,
                    availability=excluded.availability,
                    champion_id=excluded.champion_id,
                    queue_id=excluded.queue_id,
                    game_id=excluded.game_id,
                    game_name=excluded.game_name,
                    tag_line=excluded.tag_line,
                    platform=excluded.platform,
                    updated_at=excluded.updated_at
                """,
                (
                    p.puuid,
                    p.qq_id,
                    p.state.value,
                    p.raw_game_status,
                    p.availability,
                    p.champion_id,
                    p.queue_id,
                    p.game_id,
                    p.game_name,
                    p.tag_line,
                    p.platform,
                    p.updated_at,
                ),
            )
            await db.commit()

    async def load_all_presence(self) -> dict[str, PlayerPresence]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM presence_state")
            rows = await cur.fetchall()
            out: dict[str, PlayerPresence] = {}
            for r in rows:
                out[r["puuid"]] = PlayerPresence(
                    puuid=r["puuid"],
                    state=PresenceState(r["state"]),
                    qq_id=r["qq_id"] or "",
                    raw_game_status=r["raw_game_status"] or "",
                    availability=r["availability"] or "",
                    champion_id=r["champion_id"],
                    queue_id=r["queue_id"],
                    game_id=r["game_id"],
                    game_name=r["game_name"] or "",
                    tag_line=r["tag_line"] or "",
                    platform=r["platform"] or "",
                    updated_at=r["updated_at"] or 0.0,
                )
            return out

    # ---------------- notification state ----------------

    async def get_last_online_notify(self, puuid: str, group_id: str) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT last_online_notify_at FROM notification_state WHERE puuid=? AND group_id=?",
                (puuid, str(group_id)),
            )
            row = await cur.fetchone()
            return row[0] if row else 0.0

    async def mark_online_notified(self, puuid: str, group_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO notification_state (puuid, group_id, last_online_notify_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(puuid, group_id) DO UPDATE SET
                    last_online_notify_at=excluded.last_online_notify_at,
                    updated_at=excluded.updated_at
                """,
                (puuid, str(group_id), time.time(), time.time()),
            )
            await db.commit()

    async def get_last_game_id(
        self, puuid: str, group_id: str, kind: str = "start"
    ) -> str:
        """kind: start=开局去重键 / end=结算去重键（同一局各查各的）。"""
        col = "last_end_game_id" if kind == "end" else "last_game_id"
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"SELECT {col} FROM notification_state WHERE puuid=? AND group_id=?",
                (puuid, str(group_id)),
            )
            row = await cur.fetchone()
            return row[0] if row else ""

    async def mark_game_notified(
        self, puuid: str, group_id: str, game_id: str, kind: str = "start"
    ) -> None:
        col = "last_end_game_id" if kind == "end" else "last_game_id"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"""
                INSERT INTO notification_state (puuid, group_id, {col}, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(puuid, group_id) DO UPDATE SET
                    {col}=excluded.{col}, updated_at=excluded.updated_at
                """,
                (puuid, str(group_id), game_id, time.time()),
            )
            await db.commit()

    # ---------------- matches cache ----------------

    async def save_match_raw(
        self, game_id: str, puuid: str, raw: dict, end_time_ms: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO matches (game_id, puuid, raw_json, end_time_ms, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(game_id, puuid) DO NOTHING
                """,
                (
                    str(game_id),
                    puuid,
                    json.dumps(raw, ensure_ascii=False),
                    end_time_ms,
                    time.time(),
                ),
            )
            await db.commit()

    async def close(self) -> None:
        # aiosqlite 连接为短连接模式，无需持有长连接
        return None
