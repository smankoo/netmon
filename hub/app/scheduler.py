import asyncio
import logging
import time

from . import alerts, config, retention

log = logging.getLogger("netmon.hub.scheduler")


async def _loop(name: str, interval_s: int, fn) -> None:
    while True:
        start = time.time()
        try:
            await asyncio.to_thread(fn)
        except Exception:  # noqa: BLE001
            log.exception("%s task failed", name)
        elapsed = time.time() - start
        await asyncio.sleep(max(1.0, interval_s - elapsed))


def start_background_tasks() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("alerts", config.ALERT_INTERVAL_SEC, alerts.check_all)),
        asyncio.create_task(_loop("retention", config.RETENTION_INTERVAL_SEC, retention.prune_old_data)),
    ]
