"""Deletes raw samples older than RETENTION_DAYS (default 180 = ~6 months).

Plain deletion, not downsampling - if long-term trend data past 6 months
turns out to matter, the next step would be an hourly/daily rollup table
before pruning raw rows, but that's more schema than this needs today.
"""
import logging
import time

from . import config
from .db import write_conn

log = logging.getLogger("netmon.retention")

_TABLES = ("ping_samples", "speed_samples", "modem_samples", "http_samples")


def prune_old_data() -> dict:
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    counts = {}
    with write_conn() as conn:
        for table in _TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            counts[table] = cur.rowcount
    total = sum(counts.values())
    if total:
        log.info("pruned %d row(s) older than %d days: %s", total, config.RETENTION_DAYS, counts)
    return counts
