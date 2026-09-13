import asyncio
import logging
import time

from . import config, outbox, sender
from .collectors import http_check, modem, ping, speed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("probe.runner")


def _next_slot(interval_s: int, offset_s: int, now: float) -> float:
    """Absolute time of the next run, anchored to the epoch rather than to startup.

    The old loop slept `interval - elapsed`, which gives an exact start-to-start
    period - so a phase offset applied once at startup would hold indefinitely.
    It would not survive a *restart*, though: whichever probe was rebuilt last
    would re-anchor its phase to its own start time, and two probes that were
    deliberately half an interval apart could silently drift into lockstep and
    start speedtesting on top of each other. iris reboots nightly for Hermes, so
    that is a matter of time, not a hypothetical.

    Anchoring to the epoch instead makes the schedule a property of the
    configuration rather than of process history: every probe with the same
    interval and offset lands on the same absolute seconds, restart or not.
    """
    phase = offset_s % interval_s
    slot = now - (now % interval_s) + phase
    while slot <= now:
        slot += interval_s
    return slot


async def _collector_loop(name: str, kind: str, interval_s: int, fn, offset_s: int = 0) -> None:
    if interval_s <= 0:
        log.info("%s: disabled (interval <= 0)", name)
        return

    log.info("%s: every %ds, phase offset %ds", name, interval_s, offset_s % interval_s)
    while True:
        # Sleep to the top of the slot before collecting, so the first run after a
        # restart is on-schedule too. Cost: after a restart a collector stays quiet
        # for up to one interval, which for speed (1800s) is a visible gap.
        await asyncio.sleep(max(0.0, _next_slot(interval_s, offset_s, time.time()) - time.time()))
        try:
            result = await asyncio.to_thread(fn)
            rows = result if isinstance(result, list) else [result]
            outbox.enqueue(kind, rows, time.time())
            log.info("%s: collected %d row(s)", name, len(rows))
        except Exception:  # noqa: BLE001
            log.exception("%s collector failed", name)


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
        asyncio.create_task(_collector_loop(
            "ping", "ping", config.PING_INTERVAL_SEC, ping.collect_once, config.PING_PHASE_OFFSET_SEC)),
        asyncio.create_task(_collector_loop(
            "speed", "speed", config.SPEEDTEST_INTERVAL_SEC, speed.collect_once, config.SPEEDTEST_PHASE_OFFSET_SEC)),
        asyncio.create_task(_collector_loop(
            "modem", "modem", config.MODEM_POLL_INTERVAL_SEC, modem.collect_once, config.MODEM_PHASE_OFFSET_SEC)),
    ]
    if config.HTTP_CHECK_URLS:
        tasks.append(asyncio.create_task(_collector_loop(
            "http_check", "http", config.HTTP_CHECK_INTERVAL_SEC, http_check.collect_once,
            config.HTTP_CHECK_PHASE_OFFSET_SEC)))
    tasks.append(asyncio.create_task(_sender_loop()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
