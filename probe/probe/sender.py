import json
import logging
import urllib.error
import urllib.request

from . import config, outbox

log = logging.getLogger("probe.sender")

_KIND_KEYS = {"ping": "ping", "speed": "speed", "modem": "modem", "http": "http"}


def send_pending_once() -> dict:
    if not config.HUB_URL or not config.HUB_TOKEN:
        return {"skipped": "HUB_URL/HUB_TOKEN not configured"}

    batch = outbox.pending_batch(limit=500)
    if not batch:
        return {"sent": 0}

    payload: dict[str, list] = {"ping": [], "speed": [], "modem": [], "http": []}
    ids = []
    for row_id, kind, row in batch:
        key = _KIND_KEYS.get(kind)
        if key:
            payload[key].append(row)
        ids.append(row_id)

    req = urllib.request.Request(
        f"{config.HUB_URL}/api/ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.HUB_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        outbox.delete_ids(ids)
        return {"sent": len(ids)}
    except urllib.error.HTTPError as exc:
        log.warning("hub rejected batch (HTTP %s): %s", exc.code, exc.read()[:500])
        return {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - network down, hub unreachable, etc.
        log.warning("could not reach hub: %s", exc)
        return {"error": str(exc)}
