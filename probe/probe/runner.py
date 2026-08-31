import asyncio
import logging
import time

from . import config, outbox, sender
from .collectors import http_check, modem, ping, speed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("probe.runner")


async def _collector_loop(name: str, kind: str, interval_s: int, fn) -> None:
    while True:
        start = time.time()
        try:
            result = await asyncio.to_thread(fn)
            rows = result if isinstance(result, list) else [result]
            outbox.enqueue(kind, rows, time.time())
            log.info("%s: collected %d row(s)", name, len(rows))
        except Exception:  # noqa: BLE001
            log.exception("%s collector failed", name)
        elapsed = time.time() - start
        await asyncio.sleep(max(1.0, interval_s - elapsed))


async def _sender_loop() -> None:
    while True:
        result = sender.send_pending_once()
        depth = outbox.depth()
        if result.get("sent"):
            log.info("sent %d row(s) to hub, %d still pending", result["sent"], depth)
        elif result.get("error"):
            log.warning("could not send to hub (%s) - %d row(s) queued locally", result["error"], depth)
        await asyncio.sleep(config.SEND_INTERVAL_SEC)


async def main() -> None:
    outbox.init_outbox()
    log.info("probe starting - hub=%s outbox=%s", config.HUB_URL or "(not configured)", config.OUTBOX_PATH)

    tasks = [
        asyncio.create_task(_collector_loop("ping", "ping", config.PING_INTERVAL_SEC, ping.collect_once)),
        asyncio.create_task(_collector_loop("speed", "speed", config.SPEEDTEST_INTERVAL_SEC, speed.collect_once)),
        asyncio.create_task(_collector_loop("modem", "modem", config.MODEM_POLL_INTERVAL_SEC, modem.collect_once)),
    ]
    if config.HTTP_CHECK_URLS:
        tasks.append(
            asyncio.create_task(_collector_loop("http_check", "http", config.HTTP_CHECK_INTERVAL_SEC, http_check.collect_once))
        )
    tasks.append(asyncio.create_task(_sender_loop()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
