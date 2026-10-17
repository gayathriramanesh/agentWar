"""Shipment store on SQLite, shared by the agent process and the MCP server process.

SQLite gives atomic, process-safe writes (the old JSON file could be read half-written by the MCP
server) and O(1) inserts instead of rewriting the whole file per shipment. Swap for the real TMS later.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from . import config

_LOCAL = threading.local()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    tracking_id TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    origin      TEXT NOT NULL,
    record      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_order ON shipments(order_id);
"""


def _path():
    return config.STATE_DIR / "shipments.db"


def _conn() -> sqlite3.Connection:
    """One connection per thread (sqlite3 connections must not be shared across threads)."""
    path = str(_path())
    conn = getattr(_LOCAL, "conn", None)
    if conn is None or getattr(_LOCAL, "path", None) != path:
        _path().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _LOCAL.conn, _LOCAL.path = conn, path
    return conn


def save_shipment(record: dict) -> None:
    _conn().execute("INSERT OR REPLACE INTO shipments (tracking_id, order_id, origin, record, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (record["tracking_id"], record["order_id"], record["origin"], json.dumps(record), time.time()))


def get_shipment(tracking_id: str) -> dict | None:
    row = _conn().execute("SELECT record FROM shipments WHERE tracking_id = ?", (tracking_id,)).fetchone()
    return json.loads(row[0]) if row else None


def find_shipments(order_id: str) -> list[dict]:
    """All shipments already booked for an order (idempotency + duplicate-order detection)."""
    rows = _conn().execute("SELECT record FROM shipments WHERE order_id = ? ORDER BY created_at", (order_id,))
    return [json.loads(r[0]) for r in rows]


def reset() -> None:
    _conn().execute("DELETE FROM shipments")
