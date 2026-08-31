import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


DB_PATH = os.environ.get("DB_PATH", "/data/netmon.db")

# "name:token,name2:token2" - each probe gets its own token, which the hub
# uses to determine the probe's identity (never trust a probe-supplied name -
# always derive it from which token was presented).
_raw_tokens = os.environ.get("HUB_PROBE_TOKENS", "")
PROBE_TOKENS: dict[str, str] = {}
for pair in _raw_tokens.split(","):
    pair = pair.strip()
    if not pair or ":" not in pair:
        continue
    name, token = pair.split(":", 1)
    PROBE_TOKENS[token.strip()] = name.strip()

# Proof a request actually came through Traefik's forward-auth router, not
# directly from some other process on the VPS box. Empty = check disabled
# (e.g. local dev without a proxy in front).
PROXY_SECRET = os.environ.get("NETMON_PROXY_SECRET", "")
PROXY_SECRET_HEADER = os.environ.get("NETMON_PROXY_SECRET_HEADER", "X-Netmon-Proxy-Secret")

# Push notifications (ntfy.sh - see hub/app/alerts.py). Full URL including the
# topic, e.g. https://ntfy.sh/<topic>. Empty disables alerting entirely.
NTFY_URL = os.environ.get("NTFY_URL", "")
ALERT_INTERVAL_SEC = _int("ALERT_INTERVAL_SEC", 300)
# Suppress repeat pushes for the same ongoing problem - one page on a broken
# connection is useful, one every 5 minutes is how an alert channel gets muted.
ALERT_COOLDOWN_SEC = _int("ALERT_COOLDOWN_SEC", 3600)
# A probe that hasn't reported anything in this long is treated as down.
PROBE_STALE_MINUTES = _int("PROBE_STALE_MINUTES", 10)
# Packet loss average over a trailing 15-minute window that counts as "bad".
LOSS_ALERT_PCT = _float("LOSS_ALERT_PCT", 10.0)

# How long to keep raw samples before they're deleted. 180 days ~= 6 months.
RETENTION_DAYS = _int("RETENTION_DAYS", 180)
RETENTION_INTERVAL_SEC = _int("RETENTION_INTERVAL_SEC", 86400)
