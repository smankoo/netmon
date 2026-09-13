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

# The probes that are SUPPOSED to exist. A probe cannot report without a token,
# so this is the registry - which makes it the right basis for "is anyone
# missing?", unlike the set of names present in the database.
#
# That distinction was a live bug. Heartbeat alerting used to iterate every
# probe_name ever seen in any table, so `macbook` - a probe deliberately retired
# on 2026-08-30 after a single evening - stayed "silent" forever and re-notified
# every ALERT_COOLDOWN_SEC for 13 days. An alert that cannot ever be satisfied
# trains you to ignore the channel it arrives on, which costs you the real one.
# Retiring a probe is now exactly "remove its token".
EXPECTED_PROBES: set[str] = set(PROBE_TOKENS.values())

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


def probe_stale_minutes(probe: str) -> int:
    """Staleness threshold for one probe: PROBE_STALE_MINUTES_<NAME>, else the default.

    A single global threshold assumes every probe reports on a similar cadence,
    and that stopped being true when the collectors were split across hosts on
    2026-09-13: iris runs everything (so it reports within a minute), while the
    iMac runs the speed test alone every 1800s to isolate the Wi-Fi leg. Against
    a 10-minute default the iMac is "silent" for two thirds of every half hour,
    flapping between bad and recovered ~48 times a day - all of it noise, none of
    it a fault.

    Deliberately an explicit per-probe value rather than one inferred from the
    probe's observed interval: a self-tuning threshold quietly widens to
    accommodate a probe that is genuinely degrading, which is the one case where
    you want it to complain.
    """
    return _int(f"PROBE_STALE_MINUTES_{probe.upper().replace('-', '_')}", PROBE_STALE_MINUTES)
# Packet loss average over a trailing 15-minute window that counts as "bad".
LOSS_ALERT_PCT = _float("LOSS_ALERT_PCT", 10.0)

# How long to keep raw samples before they're deleted. 180 days ~= 6 months.
RETENTION_DAYS = _int("RETENTION_DAYS", 180)
RETENTION_INTERVAL_SEC = _int("RETENTION_INTERVAL_SEC", 86400)
