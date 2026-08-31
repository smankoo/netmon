import json
import time

from playwright.sync_api import sync_playwright

from .. import config

LOGIN_URL = "http://{host}/webpages/login.html"


def _parse_number(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_tables(page) -> tuple[list[dict], list[dict]]:
    """Return (downstream_rows, upstream_rows) parsed from the DOCSIS WAN page.

    Each section on this page is a single <table> whose *first* row is just a
    title bar (e.g. "Downstream Overview") - the real column headers are the
    row after that. So instead of assuming row 0 is the header row, find
    whichever row actually contains the column names we care about.
    """
    all_rows = page.eval_on_selector_all(
        "table",
        """
        (tables) => tables.map(t =>
            Array.from(t.querySelectorAll('tr')).map(
                r => Array.from(r.querySelectorAll('th, td')).map(c => c.textContent.trim())
            )
        )
        """,
    )

    downstream, upstream = [], []
    for rows in all_rows:
        header_idx = next((i for i, r in enumerate(rows) if "Uncorrectables" in r), None)
        if header_idx is not None:
            headers = rows[header_idx]
            for row in rows[header_idx + 1:]:
                if len(row) != len(headers):
                    continue
                downstream.append(dict(zip(headers, row)))
            continue

        header_idx = next(
            (i for i, r in enumerate(rows) if "Bandwidth" in r and "Channel ID" in r), None
        )
        if header_idx is not None:
            headers = rows[header_idx]
            for row in rows[header_idx + 1:]:
                if len(row) != len(headers):
                    continue
                upstream.append(dict(zip(headers, row)))

    return downstream, upstream


def collect_once() -> list[dict]:
    """Returns rows ready to send to the hub - the caller owns delivery."""
    ts = time.time()
    rows_to_insert = []
    error = None

    if not config.MODEM_USER or not config.MODEM_PASS:
        error = "MODEM_USER/MODEM_PASS not configured"
    else:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(LOGIN_URL.format(host=config.MODEM_HOST), timeout=15000)
                page.fill("#user_login", config.MODEM_USER)
                page.fill("#user_password", config.MODEM_PASS)
                page.click("#btnLogin")
                # The app's Backbone router needs a few seconds to finish initializing
                # after login before it reliably responds to navigation - clicking too
                # early can silently land on the wrong section.
                page.wait_for_timeout(6000)

                # Drive real clicks on the nav tabs rather than setting location.hash
                # directly: this app's router is flaky about picking up hash changes
                # that aren't triggered by an actual click, so retry a few times.
                found = False
                for _ in range(3):
                    try:
                        page.click("text=Status", timeout=4000)
                        page.wait_for_timeout(500)
                        page.click("text=DOCSIS WAN", timeout=4000)
                        page.wait_for_selector("text=Downstream Overview", timeout=5000)
                        found = True
                        break
                    except Exception:  # noqa: BLE001 - retry
                        continue

                if not found:
                    raise RuntimeError("could not reach DOCSIS WAN page after 3 attempts")

                # The title/header row renders immediately with the route, but the
                # actual per-channel values populate a moment later via a separate
                # async call - 1s wasn't enough and returned header-only tables.
                page.wait_for_timeout(4000)
                downstream, upstream = _extract_tables(page)
                if not downstream or not upstream:
                    debug = page.eval_on_selector_all(
                        "table",
                        """(tables) => tables.map(t =>
                            Array.from(t.querySelectorAll('tr')).map(
                                r => Array.from(r.querySelectorAll('th, td')).map(c => c.textContent.trim())
                            )
                        )""",
                    )
                    with open("/tmp/netmon_modem_debug.json", "w") as f:
                        json.dump(debug, f, indent=2)
                browser.close()

            for d in downstream:
                rows_to_insert.append({
                    "direction": "downstream",
                    "port_id": d.get("Port ID"),
                    "channel_id": d.get("Channel ID"),
                    "frequency": d.get("Frequency (Hz)"),
                    "modulation": d.get("Modulation"),
                    "signal_strength_dbmv": _parse_number(d.get("Signal strength (dBmV)", "")),
                    "snr_db": _parse_number(d.get("Signal noise ratio (dB)", "")),
                    "corrected": int(_parse_number(d.get("Correcteds", "")) or 0),
                    "uncorrectable": int(_parse_number(d.get("Uncorrectables", "")) or 0),
                })
            for u in upstream:
                rows_to_insert.append({
                    "direction": "upstream",
                    "port_id": u.get("Port ID"),
                    "channel_id": u.get("Channel ID"),
                    "frequency": u.get("Frequency (Hz)"),
                    "modulation": u.get("Modulation"),
                    "signal_strength_dbmv": _parse_number(u.get("Signal strength (dBmV)", "")),
                    "snr_db": None,
                    "corrected": None,
                    "uncorrectable": None,
                })

            if not rows_to_insert:
                error = "no rows parsed from DOCSIS WAN page (layout may have changed)"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

    if rows_to_insert:
        return [{**r, "ts": ts, "error": None} for r in rows_to_insert]
    return [{
        "ts": ts, "direction": "error", "channel_id": None, "port_id": None, "frequency": None,
        "modulation": None, "signal_strength_dbmv": None, "snr_db": None, "corrected": None,
        "uncorrectable": None, "error": error,
    }]
