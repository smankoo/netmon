import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .auth import require_probe
from .db import get_conn, init_db, write_conn
from .models import IngestPayload
from .scheduler import start_background_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = start_background_tasks()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="netmon-hub", lifespan=lifespan)


# Traefik forward-auth (Authentik) gates this app on the pretty hostname, but
# this container is published on loopback (127.0.0.1:8000) and Traefik runs
# host-network - docker SNATs its connection to the bridge gateway, same as
# every other host-local process. Trusting the source address alone would
# mean trusting every process on the VPS. Podify hit exactly this
# (`sudo -u nobody curl -H 'X-authentik-username: ...'` read data that had
# needed a password moments before) - this header check is the same fix.
# /api/ingest and /healthz are exempt: ingest has its own per-probe bearer
# token and is an api_bypass_path in Traefik (never sees forward-auth or this
# header); /healthz needs to answer plain liveness probes (Ansible, Docker)
# that hit the container directly, same as podify's/miniflux's own bypass.
_PROXY_SECRET_EXEMPT_PATHS = {"/api/ingest", "/healthz"}


@app.middleware("http")
async def require_proxy_secret(request: Request, call_next):
    if config.PROXY_SECRET and request.url.path not in _PROXY_SECRET_EXEMPT_PATHS:
        if request.headers.get(config.PROXY_SECRET_HEADER) != config.PROXY_SECRET:
            return PlainTextResponse("forbidden - not reached through the proxy", status_code=403)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/ingest")
def ingest(payload: IngestPayload, probe_name: str = Depends(require_probe)):
    counts = {"ping": 0, "speed": 0, "modem": 0, "http": 0}
    with write_conn() as conn:
        for r in payload.ping:
            conn.execute(
                "INSERT INTO ping_samples (probe_name, ts, target, sent, received, loss_pct, min_ms, avg_ms, max_ms, mdev_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (probe_name, r.ts, r.target, r.sent, r.received, r.loss_pct, r.min_ms, r.avg_ms, r.max_ms, r.mdev_ms),
            )
            counts["ping"] += 1

        for r in payload.speed:
            conn.execute(
                "INSERT INTO speed_samples (probe_name, ts, download_mbps, upload_mbps, idle_latency_ms, "
                "loaded_latency_avg_ms, loaded_latency_max_ms, server_name, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (probe_name, r.ts, r.download_mbps, r.upload_mbps, r.idle_latency_ms,
                 r.loaded_latency_avg_ms, r.loaded_latency_max_ms, r.server_name, r.error),
            )
            counts["speed"] += 1

        for r in payload.modem:
            conn.execute(
                "INSERT INTO modem_samples (probe_name, ts, direction, channel_id, port_id, frequency, modulation, "
                "signal_strength_dbmv, snr_db, corrected, uncorrectable, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (probe_name, r.ts, r.direction, r.channel_id, r.port_id, r.frequency, r.modulation,
                 r.signal_strength_dbmv, r.snr_db, r.corrected, r.uncorrectable, r.error),
            )
            counts["modem"] += 1

        for r in payload.http:
            conn.execute(
                "INSERT INTO http_samples (probe_name, ts, url, status_code, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
                (probe_name, r.ts, r.url, r.status_code, r.latency_ms, r.error),
            )
            counts["http"] += 1

    return {"probe": probe_name, "inserted": counts}


@app.get("/api/ping")
def get_ping(hours: float = Query(24, ge=0.1, le=24 * 30), probe: str | None = None):
    since = time.time() - hours * 3600
    with get_conn() as conn:
        if probe:
            rows = conn.execute(
                "SELECT probe_name, ts, target, sent, received, loss_pct, min_ms, avg_ms, max_ms, mdev_ms "
                "FROM ping_samples WHERE ts >= ? AND probe_name = ? ORDER BY ts ASC",
                (since, probe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT probe_name, ts, target, sent, received, loss_pct, min_ms, avg_ms, max_ms, mdev_ms "
                "FROM ping_samples WHERE ts >= ? ORDER BY ts ASC",
                (since,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/speed")
def get_speed(days: float = Query(30, ge=0.1, le=365), probe: str | None = None):
    since = time.time() - days * 86400
    with get_conn() as conn:
        if probe:
            rows = conn.execute(
                "SELECT probe_name, ts, download_mbps, upload_mbps, idle_latency_ms, loaded_latency_avg_ms, "
                "loaded_latency_max_ms, server_name, error FROM speed_samples WHERE ts >= ? AND probe_name = ? ORDER BY ts ASC",
                (since, probe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT probe_name, ts, download_mbps, upload_mbps, idle_latency_ms, loaded_latency_avg_ms, "
                "loaded_latency_max_ms, server_name, error FROM speed_samples WHERE ts >= ? ORDER BY ts ASC",
                (since,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/modem")
def get_modem(hours: float = Query(24 * 7, ge=0.1, le=24 * 90), probe: str | None = None):
    since = time.time() - hours * 3600
    with get_conn() as conn:
        if probe:
            rows = conn.execute(
                "SELECT probe_name, ts, direction, channel_id, port_id, frequency, modulation, "
                "signal_strength_dbmv, snr_db, corrected, uncorrectable, error "
                "FROM modem_samples WHERE ts >= ? AND probe_name = ? ORDER BY ts ASC",
                (since, probe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT probe_name, ts, direction, channel_id, port_id, frequency, modulation, "
                "signal_strength_dbmv, snr_db, corrected, uncorrectable, error "
                "FROM modem_samples WHERE ts >= ? ORDER BY ts ASC",
                (since,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/http")
def get_http(hours: float = Query(24, ge=0.1, le=24 * 30), probe: str | None = None):
    since = time.time() - hours * 3600
    with get_conn() as conn:
        if probe:
            rows = conn.execute(
                "SELECT probe_name, ts, url, status_code, latency_ms, error FROM http_samples "
                "WHERE ts >= ? AND probe_name = ? ORDER BY ts ASC",
                (since, probe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT probe_name, ts, url, status_code, latency_ms, error FROM http_samples "
                "WHERE ts >= ? ORDER BY ts ASC",
                (since,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/probes")
def get_probes():
    now = time.time()
    with get_conn() as conn:
        last_seen: dict[str, float] = {}
        for table in ("ping_samples", "speed_samples", "modem_samples", "http_samples"):
            for r in conn.execute(f"SELECT probe_name, MAX(ts) ts FROM {table} GROUP BY probe_name"):
                if r["ts"] is not None:
                    last_seen[r["probe_name"]] = max(last_seen.get(r["probe_name"], 0), r["ts"])
    return [
        {
            "name": name,
            "last_seen": ts,
            # Per-probe, for the same reason alerts.py is: a probe running only
            # the 1800s speed test is not stale at 10 minutes, and showing it red
            # two thirds of the time teaches you to ignore red.
            "stale": (now - ts) / 60 > config.probe_stale_minutes(name),
            # Whether this probe is still supposed to exist, i.e. still holds a
            # token. This endpoint lists every probe that has ever reported, so a
            # retired one stays here until retention drops its rows - 180 days
            # during which "stale" is true and means nothing. `macbook` was
            # retired on 2026-08-30 and is exactly that. Consumers should read
            # stale together with this: stale + registered = something to fix,
            # stale + not registered = history.
            "registered": name in config.EXPECTED_PROBES,
        }
        for name, ts in sorted(last_seen.items())
    ]


@app.get("/api/alerts")
def get_alerts():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, message, last_notified FROM alert_state WHERE active = 1 ORDER BY last_notified DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/summary")
def get_summary(probe: str | None = None):
    now = time.time()
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    probe_filter = "AND probe_name = ?" if probe else ""
    args_day = (day_ago, probe) if probe else (day_ago,)
    args_week = (week_ago, probe) if probe else (week_ago,)

    with get_conn() as conn:
        ping_24h = conn.execute(
            f"SELECT target, COUNT(*) n, AVG(loss_pct) avg_loss, AVG(avg_ms) avg_latency, MAX(max_ms) worst_latency "
            f"FROM ping_samples WHERE ts >= ? {probe_filter} GROUP BY target",
            args_day,
        ).fetchall()

        speed_7d = conn.execute(
            f"SELECT AVG(download_mbps) avg_down, AVG(upload_mbps) avg_up, "
            f"AVG(idle_latency_ms) avg_idle, AVG(loaded_latency_avg_ms) avg_loaded, "
            f"MAX(loaded_latency_max_ms) worst_loaded "
            f"FROM speed_samples WHERE ts >= ? AND error IS NULL {probe_filter}",
            args_week,
        ).fetchone()

        modem_ts_filter = "AND probe_name = ?" if probe else ""
        modem_ts_args = (probe,) if probe else ()
        last_modem_ts_row = conn.execute(
            f"SELECT MAX(ts) ts FROM modem_samples WHERE direction = 'downstream' {modem_ts_filter}",
            modem_ts_args,
        ).fetchone()
        worst_channels = []
        if last_modem_ts_row and last_modem_ts_row["ts"]:
            last_ts = last_modem_ts_row["ts"]
            worst_channels = [
                dict(r)
                for r in conn.execute(
                    "SELECT channel_id, frequency, snr_db, signal_strength_dbmv, corrected, uncorrectable "
                    "FROM modem_samples WHERE direction = 'downstream' AND ts = ? "
                    "ORDER BY uncorrectable DESC LIMIT 5",
                    (last_ts,),
                ).fetchall()
            ]

        http_24h = conn.execute(
            f"SELECT url, COUNT(*) n, "
            f"SUM(CASE WHEN error IS NULL AND status_code < 400 THEN 1 ELSE 0 END) ok, "
            f"AVG(CASE WHEN error IS NULL THEN latency_ms END) avg_latency, "
            f"MAX(ts) last_ts "
            f"FROM http_samples WHERE ts >= ? {probe_filter} GROUP BY url",
            args_day,
        ).fetchall()
        http_24h = [
            {**dict(r), "uptime_pct": (r["ok"] / r["n"] * 100) if r["n"] else None}
            for r in http_24h
        ]

    return JSONResponse({
        "ping_24h": [dict(r) for r in ping_24h],
        "speed_7d": dict(speed_7d) if speed_7d else None,
        "worst_channels_latest": worst_channels,
        "http_24h": http_24h,
        "generated_at": now,
    })


app.mount("/", StaticFiles(directory="static", html=True), name="static")
