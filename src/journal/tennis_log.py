"""Append-only tennis journal.

Postgres when ``DATABASE_URL`` is a postgres URL (Railway sets this when a
Postgres plugin is attached). Otherwise SQLite at the sqlite ``DATABASE_URL``
or ``tennis_journal.db``. ``TRADE_LOG_BACKEND=jsonl`` writes a JSONL file
instead, which is enough when no database is available.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional
from urllib.parse import urlparse

import aiosqlite

COLUMNS = (
    "ts",
    "account",
    "kind",
    "ticker",
    "rule",
    "match_label",
    "score",
    "player",
    "side",
    "price",
    "size",
    "fees",
    "client_order_id",
    "order_status",
    "fill_count",
    "order_action",
    "decision",
    "exit_kind",
    "pnl",
    "won",
    "implied_prob",
    "details",
    "dedupe_key",
)

_INT_COLUMNS = {"size", "fill_count", "won"}
_REAL_COLUMNS = {"price", "fees", "pnl", "implied_prob"}


def _normalize(event: dict) -> dict:
    row = {}
    for column in COLUMNS:
        value = event.get(column)
        if value == "":
            value = None
        if column in _INT_COLUMNS and value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        elif column in _REAL_COLUMNS and value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        elif value is not None:
            value = str(value)
        row[column] = value
    return row


def _sqlite_path_from_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):] or "tennis_journal.db"
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):] or "tennis_journal.db"
    return url or "tennis_journal.db"


def _is_postgres(url: str) -> bool:
    scheme = urlparse(url).scheme.lower()
    return scheme in {"postgres", "postgresql"}


class SqliteJournal:
    """SQLite journal. Safe to construct before the event loop starts."""

    def __init__(self, path: str):
        self.path = path or "tennis_journal.db"
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        if self._db is not None:
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        columns_sql = ", ".join(
            f"{name} {'INTEGER' if name in _INT_COLUMNS else 'REAL' if name in _REAL_COLUMNS else 'TEXT'}"
            for name in COLUMNS
        )
        await self._db.execute(
            f"CREATE TABLE IF NOT EXISTS tennis_journal ("
            f"id INTEGER PRIMARY KEY AUTOINCREMENT, {columns_sql}, "
            f"UNIQUE(dedupe_key))"
        )
        await self._db.commit()

    async def record(self, event: dict) -> None:
        await self.open()
        row = _normalize(event)
        placeholders = ", ".join("?" for _ in COLUMNS)
        names = ", ".join(COLUMNS)
        await self._db.execute(
            f"INSERT OR IGNORE INTO tennis_journal ({names}) VALUES ({placeholders})",
            [row[name] for name in COLUMNS],
        )
        await self._db.commit()

    async def load_events(self, account: Optional[str] = None) -> List[dict]:
        await self.open()
        self._db.row_factory = aiosqlite.Row
        if account:
            cursor = await self._db.execute(
                "SELECT id, " + ", ".join(COLUMNS) + " FROM tennis_journal WHERE account = ? ORDER BY id",
                (account,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, " + ", ".join(COLUMNS) + " FROM tennis_journal ORDER BY id"
            )
        rows = await cursor.fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


class PostgresJournal:
    """Postgres journal. ``DATABASE_URL`` is the Railway connection string."""

    def __init__(self, dsn: str):
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        self.dsn = dsn
        self._conn = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        import asyncpg

        self._conn = await asyncpg.connect(self.dsn)
        real_cols = ", ".join(
            f"{name} {'INTEGER' if name in _INT_COLUMNS else 'DOUBLE PRECISION' if name in _REAL_COLUMNS else 'TEXT'}"
            for name in COLUMNS
        )
        await self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS tennis_journal ("
            f"id BIGSERIAL PRIMARY KEY, {real_cols})"
        )
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS tennis_journal_dedupe ON tennis_journal (dedupe_key)"
        )

    async def record(self, event: dict) -> None:
        await self.open()
        row = _normalize(event)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(COLUMNS)))
        names = ", ".join(COLUMNS)
        await self._conn.execute(
            f"INSERT INTO tennis_journal ({names}) VALUES ({placeholders}) "
            f"ON CONFLICT (dedupe_key) DO NOTHING",
            *[row[name] for name in COLUMNS],
        )

    async def load_events(self, account: Optional[str] = None) -> List[dict]:
        await self.open()
        if account:
            rows = await self._conn.fetch(
                "SELECT id, " + ", ".join(COLUMNS) + " FROM tennis_journal WHERE account = $1 ORDER BY id",
                account,
            )
        else:
            rows = await self._conn.fetch(
                "SELECT id, " + ", ".join(COLUMNS) + " FROM tennis_journal ORDER BY id"
            )
        return [dict(row) for row in rows]

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class JsonlJournal:
    """One JSON object per line. Duplicate ``dedupe_key`` values are skipped."""

    def __init__(self, path: str):
        self.path = path or "tennis_journal.jsonl"
        self._seen: Optional[set] = None

    def _load_seen(self) -> None:
        if self._seen is not None:
            return
        self._seen = set()
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    key = json.loads(line).get("dedupe_key")
                except json.JSONDecodeError:
                    continue
                if key:
                    self._seen.add(key)

    async def open(self) -> None:
        self._load_seen()
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def record(self, event: dict) -> None:
        await self.open()
        row = _normalize(event)
        key = row.get("dedupe_key")
        if key and key in self._seen:
            return
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        if key:
            self._seen.add(key)

    async def load_events(self, account: Optional[str] = None) -> List[dict]:
        await self.open()
        rows = []
        if not os.path.exists(self.path):
            return rows
        with open(self.path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row.setdefault("id", index)
                if account and row.get("account") != account:
                    continue
                rows.append(row)
        return rows

    async def close(self) -> None:
        return None


def journal_for_url(database_url: str = "", backend: str = "", jsonl_path: str = "") -> object:
    """Pick a journal from explicit settings. Used by ``open_journal`` and tests."""
    backend = (backend or "").strip().lower()
    url = (database_url or "").strip()
    if backend == "jsonl" or url.lower().startswith("jsonl:"):
        if url.lower().startswith("jsonl:"):
            path = url.split(":", 1)[1] or jsonl_path or "tennis_journal.jsonl"
        else:
            path = jsonl_path or "tennis_journal.jsonl"
        return JsonlJournal(path)
    if backend == "postgres" or (backend != "sqlite" and _is_postgres(url)):
        if not _is_postgres(url):
            raise ValueError("TRADE_LOG_BACKEND=postgres requires a postgres DATABASE_URL")
        return PostgresJournal(url)
    if url.startswith("sqlite:"):
        return SqliteJournal(_sqlite_path_from_url(url))
    if backend == "sqlite" or not url:
        return SqliteJournal(jsonl_path or "tennis_journal.db")
    return SqliteJournal(url)


async def open_journal() -> object:
    """Open the journal selected by the environment."""
    journal = journal_for_url(
        database_url=os.environ.get("DATABASE_URL", ""),
        backend=os.environ.get("TRADE_LOG_BACKEND", ""),
        jsonl_path=os.environ.get("TRADE_LOG_PATH", ""),
    )
    await journal.open()
    return journal
