"""Checkpoint store — "how far into each upstream source have I read".

This is connector state, not memory state: it lives with collectbase,
in its own ``sync.db``. Keyed by (source, location, upstream_session_id)
so the same upstream id at two endpoints (US + EU) keeps independent
cursors. The engine reads it to short-circuit unchanged sources (sha
match) and to seed the seek hint (line_offset).
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

_DDL = """
CREATE TABLE IF NOT EXISTS sync_session_checkpoint (
    source         TEXT    NOT NULL,
    location       TEXT    NOT NULL DEFAULT '',
    session_id     TEXT    NOT NULL,
    sha256         TEXT    NOT NULL,
    last_round_id  TEXT,
    line_offset    INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (source, location, session_id)
)
"""


class CheckpointStore:
    """Async SQLite store for per-source sync cursors."""

    def __init__(self, conn: aiosqlite.Connection, db_path: Path):
        self.conn = conn
        self.db_path = db_path

    @classmethod
    async def open(cls, db_path: str | Path) -> "CheckpointStore":
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute(_DDL)
        await conn.commit()
        return cls(conn, db_path)

    async def close(self) -> None:
        await self.conn.close()

    async def get(self, source: str, location: str, session_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT sha256, last_round_id, line_offset, updated_at "
            "FROM sync_session_checkpoint "
            "WHERE source = ? AND location = ? AND session_id = ?",
            (source, location, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "sha256": row["sha256"],
            "last_round_id": row["last_round_id"],
            "line_offset": row["line_offset"],
            "updated_at": row["updated_at"],
        }

    async def upsert(
        self,
        source: str,
        location: str,
        session_id: str,
        sha256: str,
        last_round_id: str | None,
        line_offset: int,
        updated_at: str,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO sync_session_checkpoint "
            "(source, location, session_id, sha256, last_round_id, line_offset, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, location, session_id, sha256, last_round_id, line_offset, updated_at),
        )
        await self.conn.commit()

    async def count(self) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM sync_session_checkpoint"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0
