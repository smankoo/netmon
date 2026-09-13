import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# Any *_INTERVAL_SEC (or MODEM_POLL_INTERVAL_SEC) set to 0 or less disables that
# collector outright - the runner never starts its loop. This matters because a
# collector that cannot do its job does not simply go quiet: modem.collect_once()
# returns an *error row* when it has no credentials, which the outbox would then
# push to the hub every interval forever. See runner._collector_loop.
#
# The *_PHASE_OFFSET_SEC knobs stagger collectors across probes that share one
# uplink. Two probes both running a speedtest read roughly half the line each and
# report meaningless bufferbloat, so the two tests must not overlap: give the
# probes offsets half an interval apart. Offsets are measured against the epoch,
# not process start - see runner._next_slot for why.

PING_TARGETS = [t.strip() for t in os.environ.get("PING_TARGETS", "1.1.1.1,8.8.8.8").split(",") if t.strip()]
PING_INTERVAL_SEC = _int("PING_INTERVAL_SEC", 60)
PING_PHASE_OFFSET_SEC = _int("PING_PHASE_OFFSET_SEC", 0)
PING_COUNT = _int("PING_COUNT", 4)

SPEEDTEST_INTERVAL_SEC = _int("SPEEDTEST_INTERVAL_SEC", 1800)
SPEEDTEST_PHASE_OFFSET_SEC = _int("SPEEDTEST_PHASE_OFFSET_SEC", 0)
BUFFERBLOAT_PING_TARGET = os.environ.get("BUFFERBLOAT_PING_TARGET", "1.1.1.1")

MODEM_HOST = os.environ.get("MODEM_HOST", "192.168.0.1")
MODEM_USER = os.environ.get("MODEM_USER", "")
MODEM_PASS = os.environ.get("MODEM_PASS", "")
MODEM_POLL_INTERVAL_SEC = _int("MODEM_POLL_INTERVAL_SEC", 900)
MODEM_PHASE_OFFSET_SEC = _int("MODEM_PHASE_OFFSET_SEC", 0)

HTTP_CHECK_URLS = [u.strip() for u in os.environ.get("HTTP_CHECK_URLS", "").split(",") if u.strip()]
HTTP_CHECK_INTERVAL_SEC = _int("HTTP_CHECK_INTERVAL_SEC", 60)
HTTP_CHECK_PHASE_OFFSET_SEC = _int("HTTP_CHECK_PHASE_OFFSET_SEC", 0)
HTTP_CHECK_TIMEOUT_SEC = _int("HTTP_CHECK_TIMEOUT_SEC", 10)

# Where this probe sends its data. HUB_TOKEN is this probe's own credential -
# the hub derives the probe's identity from which token was presented, so
# there's no separate "probe name" to configure or trust here.
HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
SEND_INTERVAL_SEC = _int("SEND_INTERVAL_SEC", 30)

OUTBOX_PATH = os.environ.get("OUTBOX_PATH", "/data/outbox.db")
