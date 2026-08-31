"""A durable local queue between the collectors and the hub.

The probe's whole purpose is to notice when the internet is unreliable - so
it can't turn around and silently drop data the moment the internet (or the
hub) is actually unreachable. Every collected row lands here first; a
separate sender loop drains it to the hub and only deletes on confirmed
success. If the hub is down for a day, this just grows until it's back.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    Path(config.OUTBOX_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.OUTBOX_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_outbox() -> None:
    with _lock, _get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _write_conn():
    with _lock:
        conn = _get_conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def enqueue(kind: str, rows: list[dict], ts: float) -> None:
    if not rows:
        return
    with _write_conn() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO outbox (kind, payload_json, created_at) VALUES (?, ?, ?)",
                (kind, json.dumps(row), ts),
            )


def pending_batch(limit: int = 500) -> list[tuple[int, str, dict]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, kind, payload_json FROM outbox ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
    return [(r["id"], r["kind"], json.loads(r["payload_json"])) for r in rows]


def delete_ids(ids: list[int]) -> None:
    if not ids:
        return
    with _write_conn() as conn:
        conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])


def depth() -> int:
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
