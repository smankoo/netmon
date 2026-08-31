import re
import subprocess
import sys
import time

from .. import config

# Handles both Linux iputils-ping (used in the container) and macOS/BSD ping
# (used when running this natively for local dev), whose summary format differs:
#   Linux:  4 packets transmitted, 4 received, 0% packet loss, time 3005ms
#           rtt min/avg/max/mdev = 16.439/19.652/25.730/2.365 ms
#   macOS:  4 packets transmitted, 4 packets received, 0.0% packet loss
#           round-trip min/avg/max/stddev = 16.439/19.652/25.730/2.365 ms
_LOSS_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received,.*?([\d.]+)% packet loss")
_RTT_RE = re.compile(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")


# -W means "seconds" on Linux (iputils, used in the container) but
# "milliseconds" on macOS/BSD ping (used for local dev runs).
_WAIT_FLAG_VALUE = "2" if sys.platform.startswith("linux") else "2000"


def ping_once(target: str, count: int = 4, timeout_s: int = 10) -> dict:
    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", _WAIT_FLAG_VALUE, target],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        out = proc.stdout
    except Exception as exc:  # noqa: BLE001 - surface any failure as a lost sample
        return {"target": target, "sent": count, "received": 0, "loss_pct": 100.0, "error": str(exc)}

    loss_match = _LOSS_RE.search(out)
    rtt_match = _RTT_RE.search(out)

    sent = int(loss_match.group(1)) if loss_match else count
    received = int(loss_match.group(2)) if loss_match else 0
    loss_pct = float(loss_match.group(3)) if loss_match else 100.0

    result = {"target": target, "sent": sent, "received": received, "loss_pct": loss_pct}
    if rtt_match:
        result.update(
            min_ms=float(rtt_match.group(1)),
            avg_ms=float(rtt_match.group(2)),
            max_ms=float(rtt_match.group(3)),
            mdev_ms=float(rtt_match.group(4)),
        )
    return result


def collect_once() -> list[dict]:
    """Returns rows ready to send to the hub - the caller owns delivery."""
    ts = time.time()
    rows = []
    for target in config.PING_TARGETS:
        r = ping_once(target, count=config.PING_COUNT)
        r["ts"] = ts
        rows.append(r)
    return rows
