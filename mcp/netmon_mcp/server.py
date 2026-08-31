"""MCP server exposing the netmon hub's read API.

Every tool returns a JSON-serialisable dict. The raw-sample tools are capped by
default because a day of ping data is a few thousand rows, and dumping those
into a conversation costs a lot of context to say very little -
`netmon_incident_report` exists precisely so that nobody has to.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import anyio.to_thread
from mcp.server.mcpserver import MCPServer

from . import analysis, config, transport

mcp = MCPServer(
    name="netmon",
    version="1.0.0",
    instructions=(
        "Reads the self-hosted netmon internet/IPTV quality monitor (home Cogeco "
        "connection). Start with netmon_summary for a quick health check. For any "
        "question about whether the connection is bad enough to complain to the ISP "
        "about, call netmon_incident_report - it does the loss/latency/DOCSIS/outage "
        "analysis server-side and returns concrete numbers with timestamps. Only "
        "reach for the raw-sample tools (netmon_ping/speed/modem/http) when you need "
        "to look at individual measurements."
    ),
)

# Default cap on rows returned by the raw-sample tools.
DEFAULT_LIMIT = 300


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _query(**params: Any) -> str:
    pairs = {k: v for k, v in params.items() if v is not None}
    return f"?{urlencode(pairs)}" if pairs else ""


async def _fetch_many(paths: dict[str, str]) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(transport.fetch_many, paths)


async def _fetch(path: str) -> Any:
    return (await _fetch_many({"r": path}))["r"]


def _window(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Wrap a raw sample list with its true count, trimmed to the newest rows."""
    out: dict[str, Any] = {"count": len(rows), "returned": len(rows), "samples": rows}
    if len(rows) > limit:
        out["returned"] = limit
        out["samples"] = rows[-limit:]
        out["truncated"] = (
            f"{len(rows)} samples matched; only the {limit} most recent are shown. "
            f"Raise `limit`, narrow the time range, or use netmon_incident_report "
            f"for an analysis over the whole set."
        )
    for row in out["samples"]:
        if isinstance(row.get("ts"), (int, float)):
            row["at"] = analysis.iso(row["ts"])
    return out


# --------------------------------------------------------------------------- #
# rollups
# --------------------------------------------------------------------------- #

@mcp.tool(
    description=(
        "Fast 'how are we doing' rollup from the netmon hub: 24h ping loss/latency "
        "per target, 7d average speed and bufferbloat, the latest modem DOCSIS "
        "channel readings, and 24h HTTP uptime per monitored URL. Optionally scope "
        "to one probe by name."
    )
)
async def netmon_summary(probe: str | None = None) -> dict[str, Any]:
    data = await _fetch(f"/api/summary{_query(probe=probe)}")
    if isinstance(data, dict) and isinstance(data.get("generated_at"), (int, float)):
        data["generated_at_iso"] = analysis.iso(data["generated_at"])
    for entry in (data.get("http_24h") or []) if isinstance(data, dict) else []:
        if isinstance(entry.get("last_ts"), (int, float)):
            entry["last_ts_iso"] = analysis.iso(entry["last_ts"])
    return data


@mcp.tool(
    description=(
        "List every probe that has ever reported to the netmon hub, with when it "
        "was last seen and whether it is stale (silent longer than the hub's "
        "PROBE_STALE_MINUTES). Use this first if a report looks emptier than expected."
    )
)
async def netmon_probes() -> dict[str, Any]:
    rows = await _fetch("/api/probes")
    for row in rows:
        if isinstance(row.get("last_seen"), (int, float)):
            row["last_seen_iso"] = analysis.iso(row["last_seen"])
    return {"count": len(rows), "probes": rows}


@mcp.tool(
    description=(
        "Currently-active netmon alerts (sustained packet loss, uncorrectable modem "
        "FEC errors, a failing HTTP target, or a silent probe). Empty means nothing "
        "is firing right now - it says nothing about earlier today."
    )
)
async def netmon_alerts() -> dict[str, Any]:
    rows = await _fetch("/api/alerts")
    for row in rows:
        if isinstance(row.get("last_notified"), (int, float)):
            row["last_notified_iso"] = analysis.iso(row["last_notified"])
    return {"count": len(rows), "alerts": rows}


# --------------------------------------------------------------------------- #
# raw samples
# --------------------------------------------------------------------------- #

@mcp.tool(
    description=(
        "Raw ping samples (per target: packets sent/received, loss %, min/avg/max/"
        "mdev latency in ms). hours: 0.1-720, default 6. Returns the most recent "
        "`limit` rows."
    )
)
async def netmon_ping(
    hours: float = 6, probe: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    hours = _clamp(hours, 0.1, 24 * 30)
    rows = await _fetch(f"/api/ping{_query(hours=hours, probe=probe)}")
    return {"hours": hours, "probe": probe, **_window(rows, int(limit))}


@mcp.tool(
    description=(
        "Raw speedtest samples (download/upload Mbps, idle and loaded latency for "
        "bufferbloat, server name, error if the test failed). days: 0.1-365, "
        "default 7."
    )
)
async def netmon_speed(
    days: float = 7, probe: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    days = _clamp(days, 0.1, 365)
    rows = await _fetch(f"/api/speed{_query(days=days, probe=probe)}")
    return {"days": days, "probe": probe, **_window(rows, int(limit))}


@mcp.tool(
    description=(
        "Raw modem DOCSIS samples: per-channel direction, channel id, frequency, "
        "modulation, signal strength (dBmV), SNR (dB), and cumulative corrected / "
        "uncorrectable FEC counters. Note the FEC counters are LIFETIME totals that "
        "reset on modem reboot - to know what actually happened in a window, use "
        "netmon_incident_report, which differences them. hours: 0.1-2160, default 24."
    )
)
async def netmon_modem(
    hours: float = 24, probe: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    hours = _clamp(hours, 0.1, 24 * 90)
    rows = await _fetch(f"/api/modem{_query(hours=hours, probe=probe)}")
    return {"hours": hours, "probe": probe, **_window(rows, int(limit))}


@mcp.tool(
    description=(
        "Raw HTTP availability checks (URL, status code, latency, error) - e.g. the "
        "IPTV portal. hours: 0.1-720, default 24."
    )
)
async def netmon_http(
    hours: float = 24, probe: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    hours = _clamp(hours, 0.1, 24 * 30)
    rows = await _fetch(f"/api/http{_query(hours=hours, probe=probe)}")
    return {"hours": hours, "probe": probe, **_window(rows, int(limit))}


# --------------------------------------------------------------------------- #
# the analysis
# --------------------------------------------------------------------------- #

@mcp.tool(
    description=(
        "THE one to use for 'is the connection bad enough to complain about?'. Pulls "
        "every raw sample for the window in a single round trip and computes, "
        "server-side: packet-loss share and worst sustained windows with timestamps; "
        "latency p50/p95/p99 and worst spikes; download/upload min/median/max, "
        "shortfall against the provisioned plan (if configured) and bufferbloat; "
        "DOCSIS uncorrectable FEC errors accrued *within the window* (differenced, "
        "not lifetime totals), channels below the SNR floor and outside the signal "
        "spec, each with timestamps; total-loss outages, probe reporting gaps and "
        "HTTP-target downtime. Ends with a structured verdict: complaint_worthy plus "
        "quotable_facts you can read out to the ISP verbatim. days: 0.1-30, default "
        "1. Pass probe=<name> when more than one probe reports, so two different "
        "connections aren't pooled."
    )
)
async def netmon_incident_report(days: float = 1, probe: str | None = None) -> dict[str, Any]:
    days = _clamp(days, 0.1, 30)
    hours = days * 24
    data = await _fetch_many({
        "ping": f"/api/ping{_query(hours=hours, probe=probe)}",
        "speed": f"/api/speed{_query(days=days, probe=probe)}",
        "modem": f"/api/modem{_query(hours=hours, probe=probe)}",
        "http": f"/api/http{_query(hours=hours, probe=probe)}",
        "probes": "/api/probes",
        "alerts": "/api/alerts",
    })
    return analysis.incident_report(
        now=time.time(),
        days=days,
        probe=probe,
        ping=data["ping"],
        speed=data["speed"],
        modem=data["modem"],
        http=data["http"],
        probes=data["probes"],
        alerts=data["alerts"],
    )


@mcp.tool(
    description=(
        "Diagnose this MCP server's own path to the netmon hub: reports the access "
        "mode (ssh-to-VPS or direct http) and does one authenticated read, timing "
        "the round trip. Call this when another netmon tool errors, to tell 'the hub "
        "is down' apart from 'this Mac cannot reach it'."
    )
)
async def netmon_access_check() -> dict[str, Any]:
    result: dict[str, Any] = {"access": transport.describe_access()}
    started = time.monotonic()
    try:
        probes = await _fetch("/api/probes")
        result["authenticated_read"] = "ok"
        result["probes_visible"] = [p.get("name") for p in probes]
    except Exception as exc:  # surfaced as data, not an error - that's the point
        result["authenticated_read"] = "failed"
        result["error"] = str(exc)
    result["round_trip_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def main() -> None:
    mcp.run(transport="stdio")
