"""Tiny JSON-file shipment store, so the MCP server process can track what the agent process booked."""
from __future__ import annotations

import json
import threading

from . import config

_LOCK = threading.Lock()
_FILE = config.STATE_DIR / "shipments.json"


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_shipment(record: dict) -> None:
    with _LOCK:
        data = _load()
        data[record["tracking_id"]] = record
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(data, indent=2))


def get_shipment(tracking_id: str) -> dict | None:
    return _load().get(tracking_id)


def reset() -> None:
    with _LOCK:
        if _FILE.exists():
            _FILE.unlink()
