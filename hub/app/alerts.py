"""Push notifications for netmon (ntfy.sh).

ntfy.sh is a free pub-sub push service: pick a topic (an arbitrary string -
it doubles as the only credential, since there's no separate auth), POST a
message to https://ntfy.sh/<topic>, and everyone subscribed to that topic
(via the ntfy phone app, or `curl -s https://ntfy.sh/<topic>/json`) gets it
instantly. No account needed. Reuses the existing `memwatch` topic already
on the phone (see vault_memwatch_ntfy_topic) rather than adding a new one to
subscribe to - messages are prefixed "Netmon:" to stay distinguishable.

Each condition below (heartbeat, packet loss, FEC errors, HTTP availability)
tracks its own on/off state in the `alert_state` table: a push fires once on
the transition into "bad", a reminder every ALERT_COOLDOWN_SEC while it stays
bad, and exactly one "recovered" push when it clears.
"""
import logging
import time
import urllib.request

from . import config
from .db import get_conn, write_conn

log = logging.getLogger("netmon.alerts")


def send_ntfy(title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> None:
    if not config.NTFY_URL:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    req = urllib.request.Request(config.NTFY_URL, data=message.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        log.exception("failed to send ntfy notification: %s", title)


def _get_state(conn, key: str) -> tuple[bool, float | None]:
    row = conn.execute("SELECT active, last_notified FROM alert_state WHERE key = ?", (key,)).fetchone()
    return (bool(row["active"]), row["last_notified"]) if row else (False, None)


def _set_state(conn, key: str, active: bool, message: str | None, last_notified: float | None) -> None:
    conn.execute(
        "INSERT INTO alert_state (key, active, message, last_notified) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET active = excluded.active, message = excluded.message, "
        "last_notified = excluded.last_notified",
        (key, int(active), message, last_notified),
    )


def evaluate(
    key: str,
    is_bad: bool,
    bad_title: str,
    bad_message: str,
    ok_message: str,
    tags: list[str] | None = None,
) -> None:
    now = time.time()
    with write_conn() as conn:
        was_active, last_notified = _get_state(conn, key)
        if is_bad:
            should_notify = not was_active or last_notified is None or (now - last_notified) >= config.ALERT_COOLDOWN_SEC
            if should_notify:
                send_ntfy(f"Netmon: {bad_title}", bad_message, priority="high", tags=tags or ["warning"])
            _set_state(conn, key, True, bad_message, now if should_notify else last_notified)
        elif was_active:
            send_ntfy("Netmon: recovered", ok_message, priority="default", tags=["white_check_mark"])
            _set_state(conn, key, False, None, now)


def _check_heartbeat(conn, probe: str, now: float) -> None:
    last_seen = 0.0
    for table in ("ping_samples", "speed_samples", "modem_samples", "http_samples"):
        row = conn.execute(f"SELECT MAX(ts) ts FROM {table} WHERE probe_name = ?", (probe,)).fetchone()
        if row["ts"]:
            last_seen = max(last_seen, row["ts"])
    stale_for_min = (now - last_seen) / 60
    is_bad = stale_for_min > config.PROBE_STALE_MINUTES
    evaluate(
        f"heartbeat:{probe}",
        is_bad,
        bad_title=f"{probe} is silent",
        bad_message=f"Probe '{probe}' hasn't reported anything in {stale_for_min:.0f} minutes.",
        ok_message=f"Probe '{probe}' is reporting again.",
        tags=["satellite"],
    )


def _check_loss(conn, probe: str, now: float) -> None:
    since = now - 900  # trailing 15 minutes
    rows = conn.execute(
        "SELECT target, AVG(loss_pct) avg_loss, COUNT(*) n FROM ping_samples "
        "WHERE probe_name = ? AND ts >= ? GROUP BY target",
        (probe, since),
    ).fetchall()
    for r in rows:
        if r["n"] < 3:
            continue  # not enough samples yet to trust the average
        is_bad = r["avg_loss"] >= config.LOSS_ALERT_PCT
        evaluate(
            f"loss:{probe}:{r['target']}",
            is_bad,
            bad_title=f"{probe}: high packet loss",
            bad_message=f"{probe}: {r['avg_loss']:.0f}% packet loss to {r['target']} over the last 15 min.",
            ok_message=f"{probe}: packet loss to {r['target']} back to normal.",
            tags=["signal_strength"],
        )


def _check_modem(conn, probe: str, now: float) -> None:
    last_ts_row = conn.execute(
        "SELECT MAX(ts) ts FROM modem_samples WHERE probe_name = ? AND direction = 'downstream'",
        (probe,),
    ).fetchone()
    if not last_ts_row["ts"]:
        return
    bad_channels = conn.execute(
        "SELECT channel_id, uncorrectable FROM modem_samples "
        "WHERE probe_name = ? AND direction = 'downstream' AND ts = ? AND uncorrectable > 0 "
        "ORDER BY uncorrectable DESC",
        (probe, last_ts_row["ts"]),
    ).fetchall()
    is_bad = len(bad_channels) > 0
    if is_bad:
        detail = ", ".join(f"ch{r['channel_id']}={r['uncorrectable']}" for r in bad_channels[:5])
        bad_message = f"{probe}: uncorrectable FEC errors on downstream channels ({detail}) - likely a line/plant issue."
    else:
        bad_message = ""
    evaluate(
        f"fec:{probe}",
        is_bad,
        bad_title=f"{probe}: modem line errors",
        bad_message=bad_message,
        ok_message=f"{probe}: all downstream channels clean again (0 uncorrectable errors).",
        tags=["satellite_antenna"],
    )


def _check_http(conn, now: float) -> None:
    urls = conn.execute("SELECT DISTINCT probe_name, url FROM http_samples").fetchall()
    for row in urls:
        probe, url = row["probe_name"], row["url"]
        recent = conn.execute(
            "SELECT status_code, error FROM http_samples WHERE probe_name = ? AND url = ? "
            "ORDER BY ts DESC LIMIT 3",
            (probe, url),
        ).fetchall()
        if len(recent) < 3:
            continue
        is_bad = all(r["error"] is not None or (r["status_code"] or 0) >= 400 for r in recent)
        evaluate(
            f"http:{probe}:{url}",
            is_bad,
            bad_title=f"{probe}: {url} is down",
            bad_message=f"{probe}: {url} has failed the last 3 checks in a row.",
            ok_message=f"{probe}: {url} is reachable again.",
            tags=["tv"],
        )


def check_all() -> None:
    with get_conn() as conn:
        now = time.time()
        probes = sorted({
            r["probe_name"]
            for table in ("ping_samples", "speed_samples", "modem_samples", "http_samples")
            for r in conn.execute(f"SELECT DISTINCT probe_name FROM {table}")
        })
        for probe in probes:
            _check_heartbeat(conn, probe, now)
            _check_loss(conn, probe, now)
            _check_modem(conn, probe, now)
        _check_http(conn, now)
