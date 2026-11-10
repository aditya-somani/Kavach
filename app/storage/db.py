"""SQLite persistence: calls, transcript segments, and the event log.

All access goes through ``asyncio.to_thread`` so the audio and scoring loops are
never blocked on disk.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    source      TEXT NOT NULL,
    max_risk    REAL NOT NULL DEFAULT 0,
    alerted     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    reasons     TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT NOT NULL,
    at          REAL NOT NULL,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    latency_ms  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT NOT NULL,
    at          REAL NOT NULL,
    kind        TEXT NOT NULL,
    score       REAL NOT NULL DEFAULT 0,
    signals     TEXT,
    text        TEXT
);

CREATE INDEX IF NOT EXISTS idx_segments_call ON segments(call_id, idx);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id, id);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes -----------------------------------------------------------
    async def create_call(self, call_id: str, source: str) -> None:
        await self._run(
            "INSERT OR REPLACE INTO calls (id, started_at, source) VALUES (?, ?, ?)",
            (call_id, time.time(), source),
        )

    async def add_segment(self, call_id: str, idx: int, text: str, latency_ms: int) -> None:
        await self._run(
            "INSERT INTO segments (call_id, at, idx, text, latency_ms) VALUES (?, ?, ?, ?, ?)",
            (call_id, time.time(), idx, text, latency_ms),
        )

    async def add_event(
        self,
        call_id: str,
        kind: str,
        score: float = 0.0,
        signals: list[str] | None = None,
        text: str = "",
    ) -> None:
        await self._run(
            "INSERT INTO events (call_id, at, kind, score, signals, text) VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, time.time(), kind, score, json.dumps(signals or []), text),
        )

    async def finish_call(
        self,
        call_id: str,
        max_risk: float,
        alerted: bool,
        summary: str = "",
        reasons: list[str] | None = None,
    ) -> None:
        await self._run(
            """UPDATE calls SET ended_at=?, max_risk=?, alerted=?, summary=?, reasons=?
               WHERE id=?""",
            (time.time(), max_risk, int(alerted), summary, json.dumps(reasons or []), call_id),
        )

    # -- reads ------------------------------------------------------------
    async def list_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            """SELECT id, started_at, ended_at, source, max_risk, alerted, summary, reasons
               FROM calls ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        )
        return [_call_row(row) for row in rows]

    async def get_call(self, call_id: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM calls WHERE id=?", (call_id,))
        if row is None:
            return None
        call = _call_row(row)
        call["segments"] = [
            dict(r) for r in await self._fetchall(
                "SELECT at, idx, text, latency_ms FROM segments WHERE call_id=? ORDER BY idx",
                (call_id,),
            )
        ]
        call["events"] = [
            dict(r) for r in await self._fetchall(
                "SELECT at, kind, score, signals, text FROM events WHERE call_id=? ORDER BY id",
                (call_id,),
            )
        ]
        return call

    # -- plumbing ---------------------------------------------------------
    async def _run(self, sql: str, params: tuple[Any, ...]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute, sql, params)

    async def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._query_all, sql, params)

    async def _fetchone(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        async with self._lock:
            return await asyncio.to_thread(self._query_one, sql, params)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def _query_all(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()


def _call_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["alerted"] = bool(data.get("alerted"))
    try:
        data["reasons"] = json.loads(data.get("reasons") or "[]")
    except json.JSONDecodeError:
        data["reasons"] = []
    return data
