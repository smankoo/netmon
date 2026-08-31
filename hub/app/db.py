import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_name TEXT NOT NULL,
    ts REAL NOT NULL,
    target TEXT NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    loss_pct REAL NOT NULL,
    min_ms REAL,
    avg_ms REAL,
    max_ms REAL,
    mdev_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping_samples(ts);
CREATE INDEX IF NOT EXISTS idx_ping_probe ON ping_samples(probe_name);

CREATE TABLE IF NOT EXISTS speed_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_name TEXT NOT NULL,
    ts REAL NOT NULL,
    download_mbps REAL,
    upload_mbps REAL,
    idle_latency_ms REAL,
    loaded_latency_avg_ms REAL,
    loaded_latency_max_ms REAL,
    server_name TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed_ts ON speed_samples(ts);
CREATE INDEX IF NOT EXISTS idx_speed_probe ON speed_samples(probe_name);

CREATE TABLE IF NOT EXISTS modem_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_name TEXT NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    channel_id TEXT,
    port_id TEXT,
    frequency TEXT,
    modulation TEXT,
    signal_strength_dbmv REAL,
    snr_db REAL,
    corrected INTEGER,
    uncorrectable INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_modem_ts ON modem_samples(ts);
CREATE INDEX IF NOT EXISTS idx_modem_probe ON modem_samples(probe_name);

CREATE TABLE IF NOT EXISTS http_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_name TEXT NOT NULL,
    ts REAL NOT NULL,
    url TEXT NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_http_ts ON http_samples(ts);
CREATE INDEX IF NOT EXISTS idx_http_probe ON http_samples(probe_name);

-- One row per alert condition (e.g. "loss:<probe>:1.1.1.1"). `active` tracks
-- whether it's currently firing so alerts.py knows trigger vs. reminder vs.
-- recovered; `message` is the last human-readable text sent, so the API/UI
-- can show *why* without re-deriving it from raw samples.
CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    last_notified REAL
);
CREATE INDEX IF NOT EXISTS idx_http_probe ON http_samples(probe_name);
"""


def get_conn() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def write_conn():
    with _lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
