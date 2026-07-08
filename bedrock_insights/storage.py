"""
SQLite persistence for per-event usage facts.

Stores the same slim "fact" the in-memory monitor keeps, keyed by the
deduplication key (``region:eventId``) so inserts are idempotent across
restarts. On startup the monitor loads recent facts back into memory, so
trends survive restarts and can reach further back than CloudWatch's own log
retention. Cost/pricing is frozen at ingest time (point-in-time).

Thread-safety: a single connection (check_same_thread=False) guarded by a lock.
Only the prime (main thread, at startup) and the background poller write; HTTP
request threads read the in-memory facts and never touch the database.
"""

from __future__ import annotations

import sqlite3
import threading

_FACT_FIELDS = (
    "t", "model", "is_global", "ident_key", "ident_label", "region",
    "err", "inp", "out", "cw", "cr", "cost", "known", "display", "op",
)


class FactStore:
    def __init__(self, path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                key         TEXT PRIMARY KEY,
                t           INTEGER NOT NULL,
                model       TEXT,
                is_global   INTEGER,
                ident_key   TEXT,
                ident_label TEXT,
                region      TEXT,
                err         TEXT,
                inp         INTEGER,
                out         INTEGER,
                cw          INTEGER,
                cr          INTEGER,
                cost        REAL,
                known       INTEGER,
                display     TEXT
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_t ON facts(t)")
        # Backward-compatible migration: add newer columns to DBs created before
        # they existed. Statements are static literals (SQLite can't bind column
        # names/DDL as parameters anyway), so there is no dynamic SQL here.
        for stmt in (
            "ALTER TABLE facts ADD COLUMN saved REAL DEFAULT 0",
            "ALTER TABLE facts ADD COLUMN op TEXT DEFAULT ''",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

    def add_many(self, rows) -> None:
        """Persist new facts. rows: iterable of (key, fact_dict). Idempotent."""
        params = [
            (
                key, f["t"], f["model"], int(f["is_global"]), f["ident_key"],
                f["ident_label"], f["region"], f["err"], f["inp"], f["out"],
                f["cw"], f["cr"], f["cost"], int(f["known"]), f["display"],
                f.get("saved", 0.0), f.get("op", ""),
            )
            for key, f in rows
        ]
        if not params:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO facts "
                "(key, t, model, is_global, ident_key, ident_label, region, err, "
                " inp, out, cw, cr, cost, known, display, saved, op) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
            self._conn.commit()

    def load(self, min_t_ms: int) -> tuple[list[dict], list[str]]:
        """Return (facts, keys) for events with t >= min_t_ms, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, t, model, is_global, ident_key, ident_label, region, "
                "err, inp, out, cw, cr, cost, known, display, saved, op "
                "FROM facts WHERE t >= ? ORDER BY t",
                (min_t_ms,),
            )
            rows = cur.fetchall()
        facts, keys = [], []
        for r in rows:
            keys.append(r[0])
            facts.append({
                "key": r[0],
                "t": r[1], "model": r[2], "is_global": bool(r[3]),
                "ident_key": r[4], "ident_label": r[5], "region": r[6], "err": r[7],
                "inp": r[8], "out": r[9], "cw": r[10], "cr": r[11],
                "cost": r[12], "known": bool(r[13]), "display": r[14],
                "saved": r[15] if r[15] is not None else 0.0,
                "op": r[16] if r[16] is not None else "",
            })
        return facts, keys

    def prune(self, before_ms: int) -> int:
        """Delete facts older than before_ms. Returns rows removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE t < ?", (before_ms,))
            self._conn.commit()
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
