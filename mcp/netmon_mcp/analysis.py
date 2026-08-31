"""Turn raw netmon samples into an ISP-complaint-grade incident report.

The point of this module is that nobody - human or model - should have to read
several thousand raw ping rows to answer "is there something wrong with this
line, and can I prove it?". Everything here reduces to numbers with timestamps
attached, because that is what a support agent can be held to.

Two domain facts shape the analysis, and getting them wrong would produce
confident nonsense:

1. **FEC counters are cumulative and reset when the modem reboots.** The raw
   `uncorrectable` value is a lifetime total, so a big number says nothing
   about the window you asked for. What matters is how much it *grew* during
   the window, so this computes per-channel deltas and treats a decrease as a
   counter reset rather than negative errors.

2. **A gap in samples is not an outage.** The probe keeps a local outbox and
   backfills with the original timestamps once it can reach the hub again, so a
   real internet outage shows up as samples with 100% loss, *not* as missing
   samples. Missing samples mean the probe host itself was off or asleep.
   These are reported separately and labelled as such.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Sequence

from . import config

Row = dict[str, Any]

CRITICAL, MAJOR, MINOR, INFO = "critical", "major", "minor", "info"
_SEVERITY_ORDER = {INFO: 0, MINOR: 1, MAJOR: 2, CRITICAL: 3}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, config.TZ).isoformat(timespec="seconds")


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolated percentile. `values` need not be sorted."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": _r(min(values)),
        "p05": _r(_percentile(values, 5)),
        "p50": _r(_percentile(values, 50)),
        "p95": _r(_percentile(values, 95)),
        "p99": _r(_percentile(values, 99)),
        "mean": _r(sum(values) / len(values)),
        "max": _r(max(values)),
    }


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _num(row: Row, key: str) -> float | None:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _contiguous(bins: Iterable[int], bin_sec: float) -> list[tuple[int, int]]:
    """Collapse a set of bin indices into (first_bin, last_bin) runs."""
    ordered = sorted(set(bins))
    runs: list[tuple[int, int]] = []
    for b in ordered:
        if runs and b == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], b)
        else:
            runs.append((b, b))
    return runs


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def _coverage(
    ping: list[Row], speed: list[Row], modem: list[Row], http: list[Row],
    probes: list[Row], window_sec: float,
) -> dict[str, Any]:
    names: set[str] = set()
    for rows in (ping, speed, modem, http):
        names.update(str(r.get("probe_name")) for r in rows if r.get("probe_name"))

    per_probe: dict[str, Any] = {}
    for name in sorted(names):
        pings = [r for r in ping if r.get("probe_name") == name]
        timestamps = [r["ts"] for r in pings if r.get("ts") is not None]
        # Each ping round produces one sample per target, so the expected count
        # scales with how many targets that probe watches.
        targets = len({r.get("target") for r in pings}) or 1
        expected = (window_sec / config.PING_INTERVAL_SEC) * targets if window_sec else 0
        per_probe[name] = {
            "ping_samples": len(pings),
            "speed_tests": sum(1 for r in speed if r.get("probe_name") == name),
            "modem_samples": sum(1 for r in modem if r.get("probe_name") == name),
            "http_checks": sum(1 for r in http if r.get("probe_name") == name),
            "ping_targets": targets,
            "first_sample_at": iso(min(timestamps)) if timestamps else None,
            "last_sample_at": iso(max(timestamps)) if timestamps else None,
            "expected_ping_samples": int(expected),
            "ping_coverage_pct": _r(len(pings) / expected * 100) if expected else None,
        }

    registered = {
        str(p.get("name")): {"last_seen": iso(p.get("last_seen")), "stale": p.get("stale")}
        for p in probes
    }
    return {
        "probes_with_data_in_window": sorted(names),
        "per_probe": per_probe,
        "registered_probes": registered,
    }


def _packet_loss(ping: list[Row]) -> dict[str, Any]:
    losses = [(r["ts"], str(r.get("target")), _num(r, "loss_pct") or 0.0)
              for r in ping if r.get("ts") is not None]
    if not losses:
        return {"samples": 0}

    with_loss = [x for x in losses if x[2] > 0]
    total_loss = [x for x in losses if x[2] >= 100]

    per_target: dict[str, Any] = {}
    for target in sorted({t for _, t, _ in losses}):
        vals = [l for _, t, l in losses if t == target]
        hits = [v for v in vals if v > 0]
        per_target[target] = {
            "samples": len(vals),
            "samples_with_loss": len(hits),
            "pct_samples_with_loss": _r(len(hits) / len(vals) * 100, 3),
            "mean_loss_pct": _r(sum(vals) / len(vals), 3),
            "max_loss_pct": _r(max(vals)),
            "samples_at_100pct_loss": sum(1 for v in vals if v >= 100),
        }

    # Rolling windows that mirror the hub's own loss alert rule, so the report
    # and the ntfy pushes can never disagree about what counted as an episode.
    win_sec = config.LOSS_WINDOW_MINUTES * 60
    buckets: dict[int, list[float]] = defaultdict(list)
    for ts, _, loss in losses:
        buckets[int(ts // win_sec)].append(loss)
    windows = [
        {
            "start": iso(b * win_sec),
            "end": iso((b + 1) * win_sec),
            "samples": len(vals),
            "mean_loss_pct": _r(sum(vals) / len(vals), 3),
            "max_loss_pct": _r(max(vals)),
        }
        for b, vals in buckets.items()
        if len(vals) >= config.LOSS_WINDOW_MIN_SAMPLES
    ]
    windows.sort(key=lambda w: w["mean_loss_pct"] or 0, reverse=True)
    over_threshold = [w for w in windows if (w["mean_loss_pct"] or 0) >= config.LOSS_ALERT_PCT]

    # Episodes: consecutive one-minute bins in which any target lost packets.
    bin_sec = config.PING_INTERVAL_SEC
    lossy_bins: dict[int, list[float]] = defaultdict(list)
    for ts, _, loss in with_loss:
        lossy_bins[int(ts // bin_sec)].append(loss)
    episodes = []
    for first, last in _contiguous(lossy_bins, bin_sec):
        vals = [v for b in range(first, last + 1) for v in lossy_bins.get(b, [])]
        episodes.append({
            "start": iso(first * bin_sec),
            "end": iso((last + 1) * bin_sec),
            "duration": _fmt_duration((last - first + 1) * bin_sec),
            "duration_sec": int((last - first + 1) * bin_sec),
            "lossy_samples": len(vals),
            "mean_loss_pct": _r(sum(vals) / len(vals), 2),
            "max_loss_pct": _r(max(vals)),
        })
    episodes.sort(key=lambda e: (e["duration_sec"], e["max_loss_pct"] or 0), reverse=True)

    return {
        "samples": len(losses),
        "samples_with_loss": len(with_loss),
        "pct_samples_with_loss": _r(len(with_loss) / len(losses) * 100, 3),
        "samples_at_100pct_loss": len(total_loss),
        "mean_loss_pct": _r(sum(l for _, _, l in losses) / len(losses), 3),
        "per_target": per_target,
        f"windows_of_{int(config.LOSS_WINDOW_MINUTES)}min_over_{config.LOSS_ALERT_PCT}pct": len(over_threshold),
        # Only windows that actually lost something. Listing the "worst" of a
        # set of all-zero windows would put arbitrary timestamps in front of a
        # reader who would reasonably assume they meant something.
        "worst_windows": [w for w in windows if (w["mean_loss_pct"] or 0) > 0][:5],
        "loss_episodes_total": len(episodes),
        "worst_loss_episodes": episodes[:10],
    }


def _latency(ping: list[Row]) -> dict[str, Any]:
    samples = [r for r in ping if _num(r, "avg_ms") is not None]
    if not samples:
        return {"samples": 0}

    all_avg = [_num(r, "avg_ms") for r in samples]
    high = [r for r in samples if (_num(r, "avg_ms") or 0) > config.LATENCY_HIGH_MS]

    per_target = {}
    # A public internet target cannot answer in well under a millisecond. When
    # it appears to, ICMP is being answered somewhere on the path - a router
    # doing "ping acceleration", a VPN/exit node, or Docker's own NAT - and the
    # numbers describe that responder, not the ISP's line. Worth detecting,
    # because a latency graph that looks perfect for the wrong reason is exactly
    # how you end up arguing from bad evidence.
    # Checked per probe, not just per target: pooling a probe that measures the
    # real path with one that doesn't would either hide the bad probe or smear
    # the blame across the good one.
    suspect: list[dict[str, Any]] = []
    for target in sorted({str(r.get("target")) for r in samples}):
        rows = [r for r in samples if str(r.get("target")) == target]
        per_target[target] = _stats([v for r in rows if (v := _num(r, "avg_ms")) is not None])
        for probe in sorted({str(r.get("probe_name")) for r in rows}):
            vals = [v for r in rows if str(r.get("probe_name")) == probe
                    and (v := _num(r, "avg_ms")) is not None]
            p50 = _percentile(vals, 50)
            if p50 is not None and p50 < config.MIN_PLAUSIBLE_RTT_MS:
                suspect.append({
                    "target": target, "probe": probe,
                    "p50_ms": _r(p50), "samples": len(vals),
                })

    spikes = sorted(samples, key=lambda r: _num(r, "max_ms") or 0, reverse=True)[:10]
    return {
        "samples": len(samples),
        "avg_ms": _stats([v for v in all_avg if v is not None]),
        "per_target_avg_ms": per_target,
        "implausibly_fast_targets": suspect,
        f"samples_over_{int(config.LATENCY_HIGH_MS)}ms": len(high),
        f"pct_over_{int(config.LATENCY_HIGH_MS)}ms": _r(len(high) / len(samples) * 100, 3),
        "worst_spikes": [
            {
                "at": iso(r.get("ts")),
                "probe": r.get("probe_name"),
                "target": r.get("target"),
                "avg_ms": _r(_num(r, "avg_ms")),
                "max_ms": _r(_num(r, "max_ms")),
                "loss_pct": _r(_num(r, "loss_pct")),
            }
            for r in spikes
        ],
    }


def _speed(speed: list[Row]) -> dict[str, Any]:
    failed = [r for r in speed if r.get("error")]
    ok = [r for r in speed if not r.get("error")]
    down = [v for r in ok if (v := _num(r, "download_mbps")) is not None]
    up = [v for r in ok if (v := _num(r, "upload_mbps")) is not None]

    # A test that reported no error but moved nothing. Kept in the stats (it is
    # a real result, and dropping it would quietly raise the reported minimum),
    # but called out separately so a `min` of 0.0 Mbps is explained rather than
    # just puzzling.
    zero = [r for r in ok if _num(r, "download_mbps") == 0 and _num(r, "upload_mbps") == 0]

    out: dict[str, Any] = {
        "tests": len(speed),
        "failed_tests": len(failed),
        "failures": [
            {"at": iso(r.get("ts")), "probe": r.get("probe_name"), "error": str(r.get("error"))[:200]}
            for r in failed[:5]
        ],
        "zero_throughput_tests": [
            {"at": iso(r.get("ts")), "probe": r.get("probe_name"), "server": r.get("server_name")}
            for r in zero
        ],
        "download_mbps": _stats(down),
        "upload_mbps": _stats(up),
    }

    plan = {"download_mbps": config.PLAN_DOWN_MBPS, "upload_mbps": config.PLAN_UP_MBPS}
    out["plan"] = plan
    if config.PLAN_DOWN_MBPS and down:
        below80 = [v for v in down if v < config.PLAN_DOWN_MBPS * 0.8]
        below50 = [v for v in down if v < config.PLAN_DOWN_MBPS * 0.5]
        out["download_vs_plan"] = {
            "median_pct_of_plan": _r(_percentile(down, 50) / config.PLAN_DOWN_MBPS * 100, 1),
            "worst_pct_of_plan": _r(min(down) / config.PLAN_DOWN_MBPS * 100, 1),
            "tests_below_80pct": len(below80),
            "tests_below_50pct": len(below50),
            "pct_tests_below_80pct": _r(len(below80) / len(down) * 100, 1),
        }
    else:
        out["download_vs_plan"] = None
        out["plan_note"] = (
            "NETMON_PLAN_DOWN_MBPS / NETMON_PLAN_UP_MBPS are not configured, so no "
            "comparison against the provisioned plan was made. Set them in mcp/.env "
            "to get plan-compliance numbers."
        )

    # Fallback yardstick when the plan is unknown: the line's own best result.
    # A line that manages p95 and then delivers a fraction of it later is
    # inconsistent regardless of what the contract says.
    if down:
        reference = _percentile(down, 95)
        out["inferred_line_capability"] = {
            "basis": "p95 of observed download in this window (NOT the contracted plan)",
            "download_p95_mbps": _r(reference),
            "tests_below_50pct_of_p95": sum(1 for v in down if v < (reference or 0) * 0.5),
            "worst_pct_of_p95": _r(min(down) / reference * 100, 1) if reference else None,
        }

    deltas = []
    for r in ok:
        idle, loaded = _num(r, "idle_latency_ms"), _num(r, "loaded_latency_avg_ms")
        if idle is not None and loaded is not None:
            deltas.append((r, loaded - idle))
    if deltas:
        values = [d for _, d in deltas]
        worst = sorted(deltas, key=lambda x: x[1], reverse=True)[:5]
        out["bufferbloat"] = {
            "samples": len(values),
            "loaded_minus_idle_ms": _stats(values),
            f"tests_over_{int(config.BUFFERBLOAT_MS)}ms": sum(1 for v in values if v > config.BUFFERBLOAT_MS),
            "worst": [
                {
                    "at": iso(r.get("ts")),
                    "idle_ms": _r(_num(r, "idle_latency_ms")),
                    "loaded_avg_ms": _r(_num(r, "loaded_latency_avg_ms")),
                    "loaded_max_ms": _r(_num(r, "loaded_latency_max_ms")),
                    "delta_ms": _r(delta),
                }
                for r, delta in worst
            ],
        }
    else:
        out["bufferbloat"] = {"samples": 0}

    if down:
        slowest = sorted((r for r in ok if _num(r, "download_mbps") is not None),
                         key=lambda r: _num(r, "download_mbps") or 0)[:5]
        out["slowest_tests"] = [
            {
                "at": iso(r.get("ts")),
                "probe": r.get("probe_name"),
                "download_mbps": _r(_num(r, "download_mbps")),
                "upload_mbps": _r(_num(r, "upload_mbps")),
                "server": r.get("server_name"),
            }
            for r in slowest
        ]
    return out


def _modem_for_probe(rows: list[Row]) -> dict[str, Any]:
    """DOCSIS analysis for one probe (one modem). Channel ids from different
    modems are not comparable, which is why this is never mixed across probes."""
    errors = [r for r in rows if r.get("error")]
    rows = [r for r in rows if not r.get("error") and r.get("ts") is not None]
    result: dict[str, Any] = {
        "samples": len(rows) + len(errors),
        "collection_errors": len(errors),
        "collection_error_examples": [
            {"at": iso(r.get("ts")), "error": str(r.get("error"))[:200]} for r in errors[:3]
        ],
    }
    if not rows:
        return result

    result["latest_poll_at"] = iso(max(r["ts"] for r in rows))
    result["polls"] = len({r["ts"] for r in rows})

    for direction in ("downstream", "upstream"):
        dir_rows = [r for r in rows if r.get("direction") == direction]
        if not dir_rows:
            continue

        by_channel: dict[str, list[Row]] = defaultdict(list)
        for r in dir_rows:
            by_channel[str(r.get("channel_id"))].append(r)
        for series in by_channel.values():
            series.sort(key=lambda r: r["ts"])

        # --- FEC deltas (see module docstring on why raw totals are useless)
        fec_channels: list[dict[str, Any]] = []
        uncorrectable_events: list[dict[str, Any]] = []
        resets: list[dict[str, Any]] = []
        total_uncorrectable = 0
        total_corrected = 0
        for channel, series in by_channel.items():
            accrued_unc = accrued_cor = 0
            first_at = last_at = None
            events = 0
            for prev, cur in zip(series, series[1:]):
                for key, is_unc in (("uncorrectable", True), ("corrected", False)):
                    before, after = _num(prev, key), _num(cur, key)
                    if before is None or after is None:
                        continue
                    delta = after - before
                    if delta < 0:
                        resets.append({
                            "at": iso(cur["ts"]), "channel": channel, "counter": key,
                            "from": int(before), "to": int(after),
                            "note": "counter went backwards - modem reboot or counter wrap; "
                                    "treated as a new baseline, not as negative errors",
                        })
                        continue
                    if is_unc:
                        accrued_unc += int(delta)
                        if delta > 0:
                            events += 1
                            first_at = first_at or cur["ts"]
                            last_at = cur["ts"]
                            uncorrectable_events.append({
                                "at": iso(cur["ts"]), "channel": channel,
                                "frequency": cur.get("frequency"),
                                "new_uncorrectable": int(delta),
                                "running_total": int(after),
                                "snr_db": _r(_num(cur, "snr_db")),
                                "signal_dbmv": _r(_num(cur, "signal_strength_dbmv")),
                            })
                    else:
                        accrued_cor += int(delta)
            total_uncorrectable += accrued_unc
            total_corrected += accrued_cor
            if accrued_unc:
                fec_channels.append({
                    "channel": channel,
                    "frequency": series[-1].get("frequency"),
                    "uncorrectable_accrued_in_window": accrued_unc,
                    "corrected_accrued_in_window": accrued_cor,
                    "polls_with_new_uncorrectable": events,
                    "first_at": iso(first_at),
                    "last_at": iso(last_at),
                    "lifetime_total_at_last_poll": int(_num(series[-1], "uncorrectable") or 0),
                })
        fec_channels.sort(key=lambda c: c["uncorrectable_accrued_in_window"], reverse=True)
        uncorrectable_events.sort(key=lambda e: e["new_uncorrectable"], reverse=True)

        # --- SNR
        snr_channels = []
        snr_all: list[float] = []
        for channel, series in by_channel.items():
            vals = [(r["ts"], _num(r, "snr_db")) for r in series if _num(r, "snr_db") is not None]
            if not vals:
                continue
            snr_all.extend(v for _, v in vals)
            low = [(ts, v) for ts, v in vals if v < config.SNR_MIN_DB]
            worst_ts, worst = min(vals, key=lambda x: x[1])
            entry = {
                "channel": channel,
                "frequency": series[-1].get("frequency"),
                "min_snr_db": _r(worst),
                "min_at": iso(worst_ts),
                "mean_snr_db": _r(sum(v for _, v in vals) / len(vals)),
                "polls_below_threshold": len(low),
            }
            if low:
                entry["first_below_at"] = iso(low[0][0])
                entry["last_below_at"] = iso(low[-1][0])
                snr_channels.append(entry)
        snr_channels.sort(key=lambda c: c["min_snr_db"] or 0)

        # --- signal level
        if direction == "downstream":
            lo, hi, marginal = config.DS_SIGNAL_MIN_DBMV, config.DS_SIGNAL_MAX_DBMV, config.DS_SIGNAL_MARGINAL_DBMV
        else:
            lo, hi, marginal = config.US_SIGNAL_MIN_DBMV, config.US_SIGNAL_MAX_DBMV, None

        out_of_spec, marginal_channels = [], []
        signal_all: list[float] = []
        for channel, series in by_channel.items():
            vals = [(r["ts"], _num(r, "signal_strength_dbmv")) for r in series
                    if _num(r, "signal_strength_dbmv") is not None]
            if not vals:
                continue
            signal_all.extend(v for _, v in vals)
            bad = [(ts, v) for ts, v in vals if v < lo or v > hi]
            entry = {
                "channel": channel,
                "frequency": series[-1].get("frequency"),
                "min_dbmv": _r(min(v for _, v in vals)),
                "max_dbmv": _r(max(v for _, v in vals)),
            }
            if bad:
                worst_ts, worst_v = max(bad, key=lambda x: abs(x[1]))
                out_of_spec.append({**entry, "polls_out_of_spec": len(bad),
                                    "worst_dbmv": _r(worst_v), "worst_at": iso(worst_ts)})
            elif marginal is not None and any(abs(v) > marginal for _, v in vals):
                marginal_channels.append(entry)

        result[direction] = {
            "channels": len(by_channel),
            "fec": {
                "uncorrectable_accrued_in_window": total_uncorrectable,
                "corrected_accrued_in_window": total_corrected,
                "channels_with_uncorrectable": fec_channels,
                "worst_uncorrectable_events": uncorrectable_events[:15],
                "counter_resets": resets[:10],
                "note": "accrued = sum of positive per-channel deltas between consecutive "
                        "polls, i.e. errors that actually happened inside this window; "
                        "the modem's own display shows lifetime totals instead",
            },
            "snr_db": {
                "overall": _stats(snr_all),
                "threshold_db": config.SNR_MIN_DB,
                "channels_below_threshold": snr_channels,
            },
            "signal_dbmv": {
                "overall": _stats(signal_all),
                "spec_window_dbmv": [lo, hi],
                "channels_out_of_spec": out_of_spec,
                "channels_marginal": marginal_channels,
            },
        }
    return result


def _outages(ping: list[Row], http: list[Row], window: tuple[float, float]) -> dict[str, Any]:
    bin_sec = config.PING_INTERVAL_SEC

    # A bin counts as a real connectivity outage only if EVERY ping sample in
    # it lost everything. One target going dark is that target's problem.
    bins: dict[int, list[Row]] = defaultdict(list)
    for r in ping:
        if r.get("ts") is not None:
            bins[int(r["ts"] // bin_sec)].append(r)
    dead = {b for b, rows in bins.items()
            if rows and all((_num(r, "loss_pct") or 0) >= 100 for r in rows)}
    outages = []
    for first, last in _contiguous(dead, bin_sec):
        rows = [r for b in range(first, last + 1) for r in bins.get(b, [])]
        outages.append({
            "start": iso(first * bin_sec),
            "end": iso((last + 1) * bin_sec),
            "duration": _fmt_duration((last - first + 1) * bin_sec),
            "duration_sec": int((last - first + 1) * bin_sec),
            "samples": len(rows),
            "targets": sorted({str(r.get("target")) for r in rows}),
            "probes": sorted({str(r.get("probe_name")) for r in rows}),
        })
    outages.sort(key=lambda o: o["duration_sec"], reverse=True)

    # Reporting gaps, per probe. Explicitly NOT outages - see module docstring.
    gap_threshold = config.PING_INTERVAL_SEC * config.GAP_FACTOR
    gaps_per_probe: dict[str, list[dict[str, Any]]] = {}
    for name in sorted({str(r.get("probe_name")) for r in ping if r.get("probe_name")}):
        times = sorted({r["ts"] for r in ping
                        if r.get("probe_name") == name and r.get("ts") is not None})
        found = []
        edges = [window[0]] + times + [window[1]]
        for index, (prev, cur) in enumerate(zip(edges, edges[1:])):
            if cur - prev > gap_threshold:
                gap = {
                    "start": iso(prev), "end": iso(cur),
                    "duration": _fmt_duration(cur - prev),
                    "duration_sec": int(cur - prev),
                }
                if index == 0:
                    # Nothing was missed here - the window simply starts before
                    # this probe's first sample exists.
                    gap["before_first_sample"] = True
                    gap["note"] = ("window starts before this probe's earliest sample; "
                                   "no data was lost, there just isn't any yet")
                elif cur == window[1]:
                    gap["still_open"] = True
                    gap["note"] = "probe has not reported since - currently silent"
                found.append(gap)
        found.sort(key=lambda g: g["duration_sec"], reverse=True)
        gaps_per_probe[name] = found[:10]

    per_url: dict[str, Any] = {}
    for url in sorted({str(r.get("url")) for r in http if r.get("url")}):
        checks = sorted((r for r in http if str(r.get("url")) == url),
                        key=lambda r: r.get("ts") or 0)
        bad_bins: dict[int, list[Row]] = defaultdict(list)
        failures = 0
        for r in checks:
            code = r.get("status_code")
            if r.get("error") or code is None or int(code) >= 400:
                failures += 1
                if r.get("ts") is not None:
                    bad_bins[int(r["ts"] // bin_sec)].append(r)
        episodes = []
        for first, last in _contiguous(bad_bins, bin_sec):
            rows = [r for b in range(first, last + 1) for r in bad_bins.get(b, [])]
            reasons = sorted({str(r.get("error") or f"HTTP {r.get('status_code')}")[:120]
                              for r in rows})
            episodes.append({
                "start": iso(first * bin_sec),
                "end": iso((last + 1) * bin_sec),
                "duration": _fmt_duration((last - first + 1) * bin_sec),
                "duration_sec": int((last - first + 1) * bin_sec),
                "failed_checks": len(rows),
                "reasons": reasons[:3],
            })
        episodes.sort(key=lambda e: e["duration_sec"], reverse=True)
        latencies = [v for r in checks
                     if not r.get("error") and (v := _num(r, "latency_ms")) is not None]
        per_url[url] = {
            "checks": len(checks),
            "failed_checks": failures,
            "uptime_pct": _r((len(checks) - failures) / len(checks) * 100, 3) if checks else None,
            "latency_ms": _stats(latencies),
            "failure_episodes": len(episodes),
            "worst_failure_episodes": episodes[:5],
        }

    return {
        "connectivity_outages": outages[:20],
        "connectivity_outage_count": len(outages),
        "total_connectivity_outage_sec": sum(o["duration_sec"] for o in outages),
        "connectivity_outage_definition": (
            "a run of one-minute bins in which every ping sample from every "
            "target reported 100% loss"
        ),
        "probe_reporting_gaps": gaps_per_probe,
        "probe_reporting_gaps_note": (
            "A gap means the probe host stopped reporting - usually asleep or "
            "powered off - NOT that the internet was down. The probe queues "
            "samples in a local outbox during an outage and backfills them with "
            "their original timestamps, so genuine connectivity loss appears as "
            "100%-loss samples under connectivity_outages instead."
        ),
        "http_targets": per_url,
    }


# --------------------------------------------------------------------------- #
# findings + verdict
# --------------------------------------------------------------------------- #

def _findings(loss: dict, latency: dict, speed: dict, modem: dict, outages: dict) -> list[dict]:
    out: list[dict] = []

    def add(severity: str, code: str, title: str, quote: str, **evidence: Any) -> None:
        out.append({"severity": severity, "code": code, "title": title,
                    "quotable": quote, "evidence": evidence})

    # --- DOCSIS. Uncorrectables are the strongest single piece of evidence
    # available here: they mean bits arrived too corrupted to reconstruct,
    # which is a plant/line fault, not congestion and not the customer's wifi.
    for probe, data in (modem.get("per_probe") or {}).items():
        for direction in ("downstream", "upstream"):
            section = data.get(direction)
            if not section:
                continue
            fec = section["fec"]
            accrued = fec["uncorrectable_accrued_in_window"]
            if accrued > 0:
                channels = fec["channels_with_uncorrectable"]
                worst = channels[0] if channels else {}
                add(
                    MAJOR if accrued < 1000 else CRITICAL,
                    f"MODEM_UNCORRECTABLE_FEC_{direction.upper()}",
                    f"{accrued} uncorrectable FEC errors accrued on {direction} channels ({probe})",
                    f"{accrued} uncorrectable FEC errors on {len(channels)} {direction} "
                    f"channel(s) between {worst.get('first_at')} and {worst.get('last_at')}; "
                    f"worst was channel {worst.get('channel')} "
                    f"({worst.get('frequency')}) with "
                    f"{worst.get('uncorrectable_accrued_in_window')}.",
                    probe=probe, direction=direction, uncorrectable_accrued=accrued,
                    channels=channels[:5],
                )
            low_snr = section["snr_db"]["channels_below_threshold"]
            if low_snr:
                worst = low_snr[0]
                add(
                    MAJOR, f"MODEM_LOW_SNR_{direction.upper()}",
                    f"{len(low_snr)} {direction} channel(s) below {config.SNR_MIN_DB} dB SNR ({probe})",
                    f"{direction} channel {worst['channel']} ({worst['frequency']}) dropped to "
                    f"{worst['min_snr_db']} dB SNR at {worst['min_at']}, below the "
                    f"{config.SNR_MIN_DB} dB floor, on {worst['polls_below_threshold']} poll(s).",
                    probe=probe, direction=direction, channels=low_snr[:5],
                )
            oos = section["signal_dbmv"]["channels_out_of_spec"]
            if oos:
                worst = max(oos, key=lambda c: abs(c.get("worst_dbmv") or 0))
                add(
                    MAJOR, f"MODEM_SIGNAL_OUT_OF_SPEC_{direction.upper()}",
                    f"{len(oos)} {direction} channel(s) outside the "
                    f"{section['signal_dbmv']['spec_window_dbmv']} dBmV window ({probe})",
                    f"{direction} channel {worst['channel']} ({worst['frequency']}) measured "
                    f"{worst['worst_dbmv']} dBmV at {worst['worst_at']}, outside the "
                    f"{section['signal_dbmv']['spec_window_dbmv']} dBmV spec window.",
                    probe=probe, direction=direction, channels=oos[:5],
                )
            elif section["signal_dbmv"]["channels_marginal"]:
                marginal = section["signal_dbmv"]["channels_marginal"]
                add(
                    MINOR, f"MODEM_SIGNAL_MARGINAL_{direction.upper()}",
                    f"{len(marginal)} {direction} channel(s) beyond "
                    f"+-{config.DS_SIGNAL_MARGINAL_DBMV} dBmV ({probe})",
                    f"{len(marginal)} {direction} channel(s) sit beyond "
                    f"+-{config.DS_SIGNAL_MARGINAL_DBMV} dBmV - in spec but not comfortable.",
                    probe=probe, direction=direction, channels=marginal[:5],
                )

    # --- loss
    sustained_key = f"windows_of_{int(config.LOSS_WINDOW_MINUTES)}min_over_{config.LOSS_ALERT_PCT}pct"
    if loss.get(sustained_key):
        worst = (loss.get("worst_windows") or [{}])[0]
        add(
            MAJOR, "PACKET_LOSS_SUSTAINED",
            f"{loss[sustained_key]} window(s) of {int(config.LOSS_WINDOW_MINUTES)} min "
            f"averaging over {config.LOSS_ALERT_PCT}% packet loss",
            f"Packet loss averaged {worst.get('mean_loss_pct')}% over the "
            f"{int(config.LOSS_WINDOW_MINUTES)} minutes from {worst.get('start')} "
            f"(peak {worst.get('max_loss_pct')}% on a single check).",
            windows_over_threshold=loss[sustained_key],
            worst_windows=loss.get("worst_windows", [])[:3],
        )
    pct_lossy = loss.get("pct_samples_with_loss")
    if pct_lossy and pct_lossy >= config.LOSS_BACKGROUND_PCT:
        worst_ep = (loss.get("worst_loss_episodes") or [{}])[0]
        add(
            MAJOR if pct_lossy >= config.LOSS_BACKGROUND_PCT * 3 else MINOR,
            "PACKET_LOSS_BACKGROUND",
            f"{pct_lossy}% of ping checks lost packets",
            f"{loss['samples_with_loss']} of {loss['samples']} ping checks "
            f"({pct_lossy}%) lost packets; longest episode ran "
            f"{worst_ep.get('duration')} from {worst_ep.get('start')}.",
            pct_samples_with_loss=pct_lossy,
            samples_with_loss=loss.get("samples_with_loss"),
            samples=loss.get("samples"),
            per_target=loss.get("per_target"),
        )

    # --- outages
    if outages.get("connectivity_outage_count"):
        worst = outages["connectivity_outages"][0]
        total = outages["total_connectivity_outage_sec"]
        add(
            CRITICAL if total >= 300 else MAJOR, "CONNECTIVITY_OUTAGE",
            f"{outages['connectivity_outage_count']} total-loss outage(s), "
            f"{_fmt_duration(total)} in total",
            f"The connection dropped every packet to every target for "
            f"{worst['duration']} starting {worst['start']} "
            f"({outages['connectivity_outage_count']} such outage(s), "
            f"{_fmt_duration(total)} total).",
            outages=outages["connectivity_outages"][:5],
            total_sec=total,
        )

    # --- latency
    p95 = (latency.get("avg_ms") or {}).get("p95")
    high_key = f"pct_over_{int(config.LATENCY_HIGH_MS)}ms"
    if p95 is not None and p95 > config.LATENCY_HIGH_MS:
        add(
            MAJOR if p95 > config.LATENCY_HIGH_MS * 2 else MINOR, "LATENCY_HIGH",
            f"p95 latency {p95} ms, over the {int(config.LATENCY_HIGH_MS)} ms mark",
            f"Latency p95 was {p95} ms and p99 "
            f"{(latency.get('avg_ms') or {}).get('p99')} ms; "
            f"{latency.get(high_key)}% of checks exceeded "
            f"{int(config.LATENCY_HIGH_MS)} ms.",
            p50=(latency.get("avg_ms") or {}).get("p50"), p95=p95,
            p99=(latency.get("avg_ms") or {}).get("p99"),
            pct_over_threshold=latency.get(high_key),
        )

    # --- measurement validity. Not a fault in the line, so deliberately INFO
    # (it must not flip complaint_worthy), but it invalidates the numbers above
    # it, so the verdict repeats it as a caveat.
    for entry in latency.get("implausibly_fast_targets") or []:
        add(
            INFO, "MEASUREMENT_ICMP_ANSWERED_LOCALLY",
            f"Probe {entry['probe']} pings {entry['target']} in {entry['p50_ms']} ms - "
            f"not a real round trip to the internet",
            f"Ping to {entry['target']} from probe {entry['probe']} shows a "
            f"median of {entry['p50_ms']} ms, which is physically impossible over a cable "
            f"connection. Something on the path (router ping-acceleration, a VPN/exit "
            f"node, or Docker's NAT) is answering the ICMP locally, so the loss and "
            f"latency figures for this target do NOT measure the ISP's line and must not "
            f"be quoted as evidence. Fix the probe's network path before relying on them.",
            **entry,
        )

    # --- speed
    vs_plan = speed.get("download_vs_plan")
    if vs_plan and vs_plan.get("median_pct_of_plan") is not None:
        if vs_plan["median_pct_of_plan"] < 80:
            add(
                MAJOR, "SPEED_BELOW_PLAN",
                f"Median download {vs_plan['median_pct_of_plan']}% of the provisioned plan",
                f"Median download was {(speed['download_mbps'] or {}).get('p50')} Mbps against a "
                f"provisioned {config.PLAN_DOWN_MBPS} Mbps "
                f"({vs_plan['median_pct_of_plan']}%); "
                f"{vs_plan['tests_below_80pct']} of {speed['download_mbps']['n']} tests came in "
                f"under 80% of plan.",
                **vs_plan,
            )
        elif vs_plan["tests_below_50pct"]:
            add(
                MINOR, "SPEED_DIPS_BELOW_PLAN",
                f"{vs_plan['tests_below_50pct']} test(s) below half the provisioned plan",
                f"{vs_plan['tests_below_50pct']} speedtest(s) delivered under half of the "
                f"provisioned {config.PLAN_DOWN_MBPS} Mbps (worst "
                f"{vs_plan['worst_pct_of_plan']}% of plan).",
                **vs_plan,
            )
    inferred = speed.get("inferred_line_capability")
    if not vs_plan and inferred and inferred.get("tests_below_50pct_of_p95"):
        add(
            MINOR, "SPEED_INCONSISTENT",
            f"{inferred['tests_below_50pct_of_p95']} test(s) under half of what the line "
            f"managed at its best",
            f"Download ranged from {(speed['download_mbps'] or {}).get('min')} Mbps to "
            f"{(speed['download_mbps'] or {}).get('max')} Mbps; "
            f"{inferred['tests_below_50pct_of_p95']} test(s) fell below half the "
            f"{inferred['download_p95_mbps']} Mbps this line reached at p95. "
            f"(No provisioned plan configured, so this is self-relative.)",
            **inferred,
        )
    if speed.get("zero_throughput_tests"):
        zero = speed["zero_throughput_tests"]
        add(
            MINOR, "SPEED_ZERO_THROUGHPUT",
            f"{len(zero)} speedtest(s) measured 0 Mbps in both directions",
            f"{len(zero)} speedtest(s) completed without reporting an error yet moved no "
            f"data at all (first at {zero[0]['at']} against {zero[0]['server']}) - either "
            f"the link was dead for the duration of the test or the test server was.",
            tests=zero[:5],
        )
    if speed.get("failed_tests"):
        add(
            MINOR, "SPEEDTEST_FAILURES",
            f"{speed['failed_tests']} speedtest(s) failed outright",
            f"{speed['failed_tests']} of {speed['tests']} speedtests failed to complete.",
            failures=speed.get("failures"),
        )
    bb = speed.get("bufferbloat") or {}
    bb_p50 = (bb.get("loaded_minus_idle_ms") or {}).get("p50")
    if bb_p50 is not None and bb_p50 > config.BUFFERBLOAT_MS:
        add(
            MINOR, "BUFFERBLOAT",
            f"Median bufferbloat {bb_p50} ms under load",
            f"Latency rose by a median of {bb_p50} ms under load "
            f"(worst {(bb.get('loaded_minus_idle_ms') or {}).get('max')} ms), over the "
            f"{int(config.BUFFERBLOAT_MS)} ms mark.",
            **{k: v for k, v in bb.items() if k != "worst"},
        )

    # --- http targets
    for url, data in (outages.get("http_targets") or {}).items():
        uptime = data.get("uptime_pct")
        if uptime is not None and uptime < 99.0:
            worst = (data.get("worst_failure_episodes") or [{}])[0]
            add(
                MAJOR if uptime < 95 else MINOR, "HTTP_TARGET_UNAVAILABLE",
                f"{url} was reachable only {uptime}% of the time",
                f"{url} failed {data['failed_checks']} of {data['checks']} checks "
                f"({uptime}% uptime); longest outage {worst.get('duration')} from "
                f"{worst.get('start')} ({', '.join(worst.get('reasons') or [])}).",
                url=url, uptime_pct=uptime, failed_checks=data["failed_checks"],
                checks=data["checks"], episodes=data.get("worst_failure_episodes"),
            )

    out.sort(key=lambda f: _SEVERITY_ORDER[f["severity"]], reverse=True)
    return out


def _verdict(findings: list[dict], coverage: dict, window_hours: float) -> dict[str, Any]:
    actionable = [f for f in findings if f["severity"] in (CRITICAL, MAJOR)]
    severity = max((f["severity"] for f in findings),
                   key=lambda s: _SEVERITY_ORDER[s], default=INFO)

    per_probe = coverage.get("per_probe") or {}
    thin = [name for name, data in per_probe.items()
            if (data.get("ping_coverage_pct") or 0) < 50]

    if actionable:
        headline = (
            f"{len(actionable)} complaint-worthy issue(s) in the last "
            f"{_fmt_duration(window_hours * 3600)}: "
            + "; ".join(f["title"] for f in actionable[:3])
        )
        next_step = (
            "Open a ticket with the concrete figures in quotable_facts. "
            "Uncorrectable FEC errors, out-of-spec signal levels and low SNR are "
            "plant-side faults and normally justify a technician visit; ask for a "
            "line/signal check rather than a modem reboot."
        )
    elif minor := [f for f in findings if f["severity"] == MINOR]:
        headline = (
            f"Nothing clearly complaint-worthy in the last "
            f"{_fmt_duration(window_hours * 3600)}, but "
            f"{len(minor)} minor observation(s) worth watching."
        )
        next_step = ("Keep collecting. Re-run over a longer window if the "
                     "problem is intermittent.")
    else:
        headline = (f"Connection looks healthy over the last "
                    f"{_fmt_duration(window_hours * 3600)} - nothing to complain about.")
        next_step = "No action needed."

    return {
        "complaint_worthy": bool(actionable),
        "worst_severity": severity,
        "headline": headline,
        "issue_count": {
            "critical": sum(1 for f in findings if f["severity"] == CRITICAL),
            "major": sum(1 for f in findings if f["severity"] == MAJOR),
            "minor": sum(1 for f in findings if f["severity"] == MINOR),
            "informational": sum(1 for f in findings if f["severity"] == INFO),
        },
        "reasons": [f["code"] for f in actionable],
        # Only real findings belong here - these are meant to be read out to the
        # ISP verbatim, and a note about our own instrumentation is not evidence.
        "quotable_facts": [f["quotable"] for f in findings if f["severity"] != INFO],
        "recommended_next_step": next_step,
        "data_caveats": (
            [f["quotable"] for f in findings if f["code"].startswith("MEASUREMENT_")]
            + [f"probe {name} covered only {per_probe[name].get('ping_coverage_pct')}% of "
               f"the expected ping samples in this window (data runs "
               f"{per_probe[name].get('first_sample_at')} to "
               f"{per_probe[name].get('last_sample_at')}) - it was off, asleep, or not "
               f"deployed yet, so absence of evidence there is not evidence of absence"
               for name in thin]
            + ([f"{len(per_probe)} probes reported in this window "
                f"({', '.join(sorted(per_probe))}); loss/latency/speed figures pool them. "
                f"Pass probe=<name> to analyse one connection at a time."]
               if len(per_probe) > 1 else [])
        ),
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def incident_report(
    *,
    now: float,
    days: float,
    probe: str | None,
    ping: list[Row],
    speed: list[Row],
    modem: list[Row],
    http: list[Row],
    probes: list[Row],
    alerts: list[Row],
) -> dict[str, Any]:
    window_sec = days * 86400
    start = now - window_sec

    loss = _packet_loss(ping)
    latency = _latency(ping)
    speed_section = _speed(speed)

    modem_probes = sorted({str(r.get("probe_name")) for r in modem if r.get("probe_name")})
    modem_section = {
        "per_probe": {name: _modem_for_probe([r for r in modem if r.get("probe_name") == name])
                      for name in modem_probes},
        "note": "Analysed per probe on purpose: channel ids from different modems are "
                "not comparable, so mixing them would invent nonexistent channels.",
    }

    outages = _outages(ping, http, (start, now))
    coverage = _coverage(ping, speed, modem, http, probes, window_sec)
    findings = _findings(loss, latency, speed_section, modem_section, outages)

    return {
        "window": {
            "days": days,
            "start": iso(start),
            "end": iso(now),
            "timezone": config.TIMEZONE,
            "probe_filter": probe,
        },
        "verdict": _verdict(findings, coverage, days * 24),
        "findings": findings,
        "packet_loss": loss,
        "latency": latency,
        "speed": speed_section,
        "modem": modem_section,
        "outages": outages,
        "active_alerts": [
            {"key": a.get("key"), "message": a.get("message"),
             "last_notified": iso(a.get("last_notified"))}
            for a in alerts
        ],
        "data_coverage": coverage,
        "thresholds_used": {
            "plan_download_mbps": config.PLAN_DOWN_MBPS,
            "plan_upload_mbps": config.PLAN_UP_MBPS,
            "latency_high_ms": config.LATENCY_HIGH_MS,
            "loss_window_minutes": config.LOSS_WINDOW_MINUTES,
            "loss_alert_pct": config.LOSS_ALERT_PCT,
            "loss_background_pct": config.LOSS_BACKGROUND_PCT,
            "snr_min_db": config.SNR_MIN_DB,
            "downstream_signal_window_dbmv": [config.DS_SIGNAL_MIN_DBMV, config.DS_SIGNAL_MAX_DBMV],
            "upstream_signal_window_dbmv": [config.US_SIGNAL_MIN_DBMV, config.US_SIGNAL_MAX_DBMV],
            "bufferbloat_ms": config.BUFFERBLOAT_MS,
        },
    }
