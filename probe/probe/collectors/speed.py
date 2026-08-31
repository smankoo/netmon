import statistics
import threading
import time

import speedtest

from .. import config
from .ping import ping_once


def _measure_latency_during(stop_event: threading.Event, samples: list[float]) -> None:
    while not stop_event.is_set():
        r = ping_once(config.BUFFERBLOAT_PING_TARGET, count=1, timeout_s=3)
        if r.get("avg_ms") is not None:
            samples.append(r["avg_ms"])
        time.sleep(0.5)


def collect_once() -> dict:
    ts = time.time()
    result = {"ts": ts, "download_mbps": None, "upload_mbps": None,
              "idle_latency_ms": None, "loaded_latency_avg_ms": None,
              "loaded_latency_max_ms": None, "server_name": None, "error": None}

    idle = ping_once(config.BUFFERBLOAT_PING_TARGET, count=3, timeout_s=6)
    result["idle_latency_ms"] = idle.get("avg_ms")

    loaded_samples: list[float] = []
    stop_event = threading.Event()
    monitor = threading.Thread(target=_measure_latency_during, args=(stop_event, loaded_samples), daemon=True)

    try:
        monitor.start()
        st = speedtest.Speedtest()
        st.get_best_server()
        result["server_name"] = st.results.server.get("host")
        st.download()
        st.upload()
        result["download_mbps"] = st.results.download / 1_000_000
        result["upload_mbps"] = st.results.upload / 1_000_000
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        stop_event.set()
        monitor.join(timeout=5)

    if loaded_samples:
        result["loaded_latency_avg_ms"] = statistics.mean(loaded_samples)
        result["loaded_latency_max_ms"] = max(loaded_samples)

    return result
