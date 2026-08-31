"""Configuration for the netmon MCP server.

Everything comes from `mcp/.env` (gitignored) with sane defaults here, so no
secret and no host-specific path is ever committed. `.env.example` documents
every knob.

Note on the proxy secret: in the default `ssh` access mode the Mac never needs
it at all - the remote shell reads it out of the hub's own env file on the VPS
(see transport.py). It's only read from here in `http` mode.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# override=False: a variable already exported in the environment (e.g. injected
# by whatever supervises the MCP server) wins over the file.
load_dotenv(ROOT / ".env", override=False)


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _opt_float(name: str) -> float | None:
    raw = _str(name)
    return float(raw) if raw else None


# --- how the MCP server reaches the hub ------------------------------------
# "ssh"  - run curl on the VPS over ssh and read the JSON back (default).
# "http" - talk HTTP directly, sending the proxy-secret header ourselves.
#          Only usable from somewhere that can actually reach the hub port:
#          on the VPS itself, or through a tunnel.
ACCESS_MODE = _str("NETMON_ACCESS", "ssh").lower()

SSH_TARGET = _str("NETMON_SSH_TARGET", "deploy@vps.taild6699c.ts.net")
# The hub as seen from the VPS. It's published on loopback only.
SSH_HUB_URL = _str("NETMON_SSH_HUB_URL", "http://127.0.0.1:8000").rstrip("/")
# The hub's own env file on the VPS - where the remote side reads the secret.
SSH_ENV_FILE = _str("NETMON_SSH_ENV_FILE", "/opt/netmon-hub/.env")
SSH_CONNECT_TIMEOUT = _int("NETMON_SSH_CONNECT_TIMEOUT", 10)
# Wall clock we give the whole ssh invocation locally.
SSH_TIMEOUT = _int("NETMON_SSH_TIMEOUT", 60)
# Per-request timeout handed to curl on the far side.
REMOTE_CURL_TIMEOUT = _int("NETMON_REMOTE_CURL_TIMEOUT", 25)

HUB_URL = _str("NETMON_HUB_URL", "http://127.0.0.1:8000").rstrip("/")
PROXY_SECRET = _str("NETMON_PROXY_SECRET")
PROXY_SECRET_HEADER = _str("NETMON_PROXY_SECRET_HEADER", "X-Netmon-Proxy-Secret")
HTTP_TIMEOUT = _int("NETMON_HTTP_TIMEOUT", 30)

# --- analysis thresholds ---------------------------------------------------
# The provisioned plan. Left unset by default: guessing it would put a fake
# number in a document meant to be quoted at an ISP. When unset, the report
# says so and falls back to comparing against what the line itself achieves
# at its best (p95 download) - a shortfall against your own best throughput
# is still evidence, just weaker.
PLAN_DOWN_MBPS = _opt_float("NETMON_PLAN_DOWN_MBPS")
PLAN_UP_MBPS = _opt_float("NETMON_PLAN_UP_MBPS")

# Latency above this counts as a "high latency" sample.
LATENCY_HIGH_MS = _float("NETMON_LATENCY_HIGH_MS", 100.0)
# Below this, a round trip to a public internet target is not physically
# possible - something on the path is answering the ICMP locally, so the
# latency and loss figures for that target describe the wrong thing entirely.
MIN_PLAUSIBLE_RTT_MS = _float("NETMON_MIN_PLAUSIBLE_RTT_MS", 3.0)
# Mirrors the hub's own alert rule (hub/app/alerts.py): mean loss over a
# trailing window, minimum sample count, so the report and the pushes agree.
LOSS_WINDOW_MINUTES = _float("NETMON_LOSS_WINDOW_MINUTES", 15.0)
LOSS_WINDOW_MIN_SAMPLES = _int("NETMON_LOSS_WINDOW_MIN_SAMPLES", 3)
LOSS_ALERT_PCT = _float("NETMON_LOSS_ALERT_PCT", 10.0)
# Background loss: anything above this share of samples showing *any* loss is
# worth raising. A healthy cable line sits at ~0.
LOSS_BACKGROUND_PCT = _float("NETMON_LOSS_BACKGROUND_PCT", 1.0)

# DOCSIS downstream: SNR floor and signal-level window. 35 dB is the usual
# QAM256 comfort floor; the -15..+15 dBmV window is the spec limit, and
# anything past +-10 is generally treated as marginal by cable techs.
SNR_MIN_DB = _float("NETMON_SNR_MIN_DB", 35.0)
DS_SIGNAL_MIN_DBMV = _float("NETMON_DS_SIGNAL_MIN_DBMV", -15.0)
DS_SIGNAL_MAX_DBMV = _float("NETMON_DS_SIGNAL_MAX_DBMV", 15.0)
DS_SIGNAL_MARGINAL_DBMV = _float("NETMON_DS_SIGNAL_MARGINAL_DBMV", 10.0)
US_SIGNAL_MIN_DBMV = _float("NETMON_US_SIGNAL_MIN_DBMV", 35.0)
US_SIGNAL_MAX_DBMV = _float("NETMON_US_SIGNAL_MAX_DBMV", 52.0)

# Loaded-minus-idle latency during a speedtest that counts as bad bufferbloat.
BUFFERBLOAT_MS = _float("NETMON_BUFFERBLOAT_MS", 100.0)

# Expected ping cadence, used to spot gaps in probe reporting and to express
# coverage as a percentage of the samples we should have.
PING_INTERVAL_SEC = _float("NETMON_PING_INTERVAL_SEC", 60.0)
# A gap longer than this many intervals is called out.
GAP_FACTOR = _float("NETMON_GAP_FACTOR", 3.0)

# Timestamps in the report are rendered in this zone - the report exists to be
# read out to a support agent, and epoch seconds don't help there.
TIMEZONE = _str("NETMON_TZ", "America/Toronto")

try:
    TZ = ZoneInfo(TIMEZONE)
except Exception:  # pragma: no cover - bad tz name shouldn't break the server
    TZ = ZoneInfo("UTC")
