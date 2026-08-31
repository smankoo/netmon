import time
import urllib.error
import urllib.request

from .. import config


def check_once(url: str, timeout_s: int) -> dict:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmon/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp.read(2048)  # enough to confirm the body is actually flowing
            status = resp.status
        latency_ms = (time.perf_counter() - start) * 1000
        return {"url": url, "status_code": status, "latency_ms": latency_ms, "error": None}
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return {"url": url, "status_code": exc.code, "latency_ms": latency_ms, "error": None}
    except Exception as exc:  # noqa: BLE001 - DNS failure, timeout, connection refused, etc.
        latency_ms = (time.perf_counter() - start) * 1000
        return {"url": url, "status_code": None, "latency_ms": latency_ms, "error": str(exc)}


def collect_once() -> list[dict]:
    """Returns rows ready to send to the hub - the caller owns delivery."""
    ts = time.time()
    rows = [check_once(url, config.HTTP_CHECK_TIMEOUT_SEC) for url in config.HTTP_CHECK_URLS]
    for r in rows:
        r["ts"] = ts
    return rows
