import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


PING_TARGETS = [t.strip() for t in os.environ.get("PING_TARGETS", "1.1.1.1,8.8.8.8").split(",") if t.strip()]
PING_INTERVAL_SEC = _int("PING_INTERVAL_SEC", 60)
PING_COUNT = _int("PING_COUNT", 4)

SPEEDTEST_INTERVAL_SEC = _int("SPEEDTEST_INTERVAL_SEC", 1800)
BUFFERBLOAT_PING_TARGET = os.environ.get("BUFFERBLOAT_PING_TARGET", "1.1.1.1")

MODEM_HOST = os.environ.get("MODEM_HOST", "192.168.0.1")
MODEM_USER = os.environ.get("MODEM_USER", "")
MODEM_PASS = os.environ.get("MODEM_PASS", "")
MODEM_POLL_INTERVAL_SEC = _int("MODEM_POLL_INTERVAL_SEC", 900)

HTTP_CHECK_URLS = [u.strip() for u in os.environ.get("HTTP_CHECK_URLS", "").split(",") if u.strip()]
HTTP_CHECK_INTERVAL_SEC = _int("HTTP_CHECK_INTERVAL_SEC", 60)
HTTP_CHECK_TIMEOUT_SEC = _int("HTTP_CHECK_TIMEOUT_SEC", 10)

# Where this probe sends its data. HUB_TOKEN is this probe's own credential -
# the hub derives the probe's identity from which token was presented, so
# there's no separate "probe name" to configure or trust here.
HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
SEND_INTERVAL_SEC = _int("SEND_INTERVAL_SEC", 30)

OUTBOX_PATH = os.environ.get("OUTBOX_PATH", "/data/outbox.db")
