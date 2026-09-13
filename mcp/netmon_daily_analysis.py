#!/usr/bin/env python3
"""
netmon_daily_analysis.py  —  runs ON the VPS, next to the netmon hub.

Pulls the last day of internet-quality samples from the local netmon hub API,
computes the metrics that actually matter for an ISP (Cogeco) complaint, appends
a one-line-per-day verdict to a persistent evidence ledger, and prints a report
to stdout for the Hermes cron agent to reason over.

Design goals (per Sumeet):
  - A complaint needs DATA. Accumulate evidence daily; only recommend filing a
    complaint when the accumulated evidence crosses a real, defensible threshold.
  - Never fabricate. If the hub is unreachable or a probe is stale, say so.

No third-party deps — stdlib only (urllib, json, statistics).
"""
import json
import os
import sys
import time
import statistics
import urllib.request
from datetime import datetime, timezone, timedelta

HUB = os.environ.get("NETMON_HUB_URL", "http://127.0.0.1:8000")
SECRET = os.environ.get("NETMON_PROXY_SECRET", "")
SECRET_HEADER = os.environ.get("NETMON_PROXY_SECRET_HEADER", "X-Netmon-Proxy-Secret")
LEDGER = os.environ.get("NETMON_LEDGER", os.path.expanduser("~/.hermes/state/netmon_evidence_ledger.jsonl"))
# The probe whose data is admissible. This must be a WIRED probe running the ping
# collector, and as of 2026-09-13 that is iris (Pi 4, wired to the Hitron) — not
# the iMac, which this defaulted to until now.
#
# Two reasons the old default would have produced a worthless complaint:
#   * the iMac's probe runs under Colima, whose user-mode network stack answers
#     ICMP locally instead of forwarding it. Its ping data averaged 0.58 ms to
#     1.1.1.1 where iris measures 19.0 ms — physically impossible from Montreal,
#     and the 38,958 fabricated rows have since been deleted from the hub.
#   * the iMac is now speed-only anyway (PING_INTERVAL_SEC=0), so there is no
#     latency or loss series there to analyse, real or otherwise.
# Taking Wi-Fi measurements to an ISP also invites the obvious rebuttal; the wired
# probe removes that argument entirely.
PROBE = os.environ.get("NETMON_PROBE", "iris")

# --- What counts as a problem worth citing to an ISP (conservative thresholds) ---
PLAN_DOWN_MBPS = float(os.environ.get("NETMON_PLAN_DOWN", "300"))  # provisioned plan; adjust to real plan
PLAN_UP_MBPS = float(os.environ.get("NETMON_PLAN_UP", "100"))
SPEED_BAD_FRAC = 0.60          # a test below 60% of plan is "far below provisioned"
LOSS_BAD_PCT = 2.0             # a ping sample with >2% loss is notable
LOSS_DAY_BAD_FRAC = 0.02       # >2% of the day's samples showing loss = a loss-y day
LATENCY_SPIKE_MS = 100.0       # ping avg over this = spike
SNR_MIN_DB = 35.0             # DOCSIS downstream SNR floor (below = marginal)
# Signal spec: downstream roughly -7..+7 dBmV is comfortable; flag outside +/-10
SIGNAL_ABS_MAX_DBMV = 10.0
GAP_MINUTES = 8.0             # ping runs ~1/min on iris; a >8 min gap = probe/connection dropout


def api(path):
    url = f"{HUB}{path}"
    req = urllib.request.Request(url)
    if SECRET:
        req.add_header(SECRET_HEADER, SECRET)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def analyze(days=1):
    hours = days * 24
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "probe": PROBE,
        "reachable": True,
        "problems": [],   # human-readable, citable findings
        "metrics": {},
    }

    # ---- probe liveness ----
    try:
        probes = api("/api/probes")
    except Exception as e:
        out["reachable"] = False
        out["error"] = f"hub unreachable: {e}"
        return out
    pinfo = next((p for p in probes if p["name"] == PROBE), None)
    out["metrics"]["probe_stale"] = bool(pinfo and pinfo.get("stale"))
    if pinfo:
        out["metrics"]["probe_last_seen"] = pinfo["last_seen"]

    # ---- ping / loss / latency ----
    ping = api(f"/api/ping?hours={hours}&probe={PROBE}")
    if ping:
        losses = [r["loss_pct"] for r in ping if r.get("loss_pct") is not None]
        lats = [r["avg_ms"] for r in ping if r.get("avg_ms") is not None]
        losed = [l for l in losses if l and l > 0]
        badloss = [l for l in losses if l and l > LOSS_BAD_PCT]
        out["metrics"]["ping"] = {
            "samples": len(ping),
            "pct_samples_with_any_loss": round(100 * len(losed) / len(losses), 3) if losses else 0,
            "pct_samples_over_thresh": round(100 * len(badloss) / len(losses), 3) if losses else 0,
            "avg_loss_pct": round(statistics.mean(losses), 4) if losses else 0,
            "max_loss_pct": round(max(losses), 2) if losses else 0,
            "latency_p50_ms": round(pct(lats, 50), 2) if lats else None,
            "latency_p95_ms": round(pct(lats, 95), 2) if lats else None,
            "latency_p99_ms": round(pct(lats, 99), 2) if lats else None,
            "latency_max_ms": round(max(lats), 2) if lats else None,
            "spikes_over_100ms": sum(1 for l in lats if l > LATENCY_SPIKE_MS),
        }
        frac_loss = (len(losed) / len(losses)) if losses else 0
        if frac_loss > LOSS_DAY_BAD_FRAC:
            worst = max(losses)
            out["problems"].append(
                f"Packet loss on {round(100*frac_loss,1)}% of samples "
                f"(worst {round(worst,1)}%) over {days}d — sustained loss, citable."
            )

        # ---- outage gaps from ping cadence ----
        ts = sorted(r["ts"] for r in ping)
        gaps = []
        for a, b in zip(ts, ts[1:]):
            gm = (b - a) / 60.0
            if gm > GAP_MINUTES:
                gaps.append((a, b, round(gm, 1)))
        out["metrics"]["outage_gaps"] = [
            {"from": datetime.fromtimestamp(a, timezone.utc).isoformat(),
             "to": datetime.fromtimestamp(b, timezone.utc).isoformat(),
             "minutes": gm}
            for a, b, gm in gaps
        ]
        if gaps:
            longest = max(g[2] for g in gaps)
            out["problems"].append(
                f"{len(gaps)} probe/connection dropout(s), longest {longest} min — possible outage(s)."
            )

    # ---- speed / bufferbloat ----
    speed = api(f"/api/speed?days={days}&probe={PROBE}")
    ok = [s for s in speed if not s.get("error")]
    if ok:
        downs = [s["download_mbps"] for s in ok if s.get("download_mbps")]
        ups = [s["upload_mbps"] for s in ok if s.get("upload_mbps")]
        idle = [s["idle_latency_ms"] for s in ok if s.get("idle_latency_ms") is not None]
        loaded = [s["loaded_latency_avg_ms"] for s in ok if s.get("loaded_latency_avg_ms") is not None]
        bb = None
        if idle and loaded:
            bb = round(statistics.mean(loaded) - statistics.mean(idle), 1)
        low_down = [d for d in downs if d < PLAN_DOWN_MBPS * SPEED_BAD_FRAC]
        out["metrics"]["speed"] = {
            "tests": len(ok),
            "down_min": round(min(downs), 1) if downs else None,
            "down_median": round(statistics.median(downs), 1) if downs else None,
            "up_min": round(min(ups), 1) if ups else None,
            "up_median": round(statistics.median(ups), 1) if ups else None,
            "tests_far_below_plan": len(low_down),
            "plan_down_mbps": PLAN_DOWN_MBPS,
            "bufferbloat_ms": bb,
        }
        if downs and len(low_down) >= max(2, 0.25 * len(downs)):
            out["problems"].append(
                f"{len(low_down)}/{len(downs)} speed tests below {int(SPEED_BAD_FRAC*100)}% of the "
                f"{PLAN_DOWN_MBPS:.0f} Mbps plan (min {round(min(downs),1)} Mbps) — chronic under-provisioning."
            )

    # ---- modem / DOCSIS: the smoking gun for a physical line fault ----
    modem = api(f"/api/modem?hours={hours}&probe={PROBE}")
    down_ch = [m for m in modem if m.get("direction") == "downstream"]
    if down_ch:
        # sum uncorrectable FEC across the window; any is notable, a lot is a fault
        total_uncorr = sum((m.get("uncorrectable") or 0) for m in down_ch)
        total_corr = sum((m.get("corrected") or 0) for m in down_ch)
        snrs = [m["snr_db"] for m in down_ch if m.get("snr_db") is not None]
        sigs = [m["signal_strength_dbmv"] for m in down_ch if m.get("signal_strength_dbmv") is not None]
        low_snr = [s for s in snrs if s < SNR_MIN_DB]
        bad_sig = [s for s in sigs if abs(s) > SIGNAL_ABS_MAX_DBMV]
        out["metrics"]["modem"] = {
            "downstream_samples": len(down_ch),
            "total_uncorrectable_fec": total_uncorr,
            "total_corrected_fec": total_corr,
            "snr_min_db": round(min(snrs), 1) if snrs else None,
            "snr_samples_below_floor": len(low_snr),
            "signal_out_of_spec_samples": len(bad_sig),
        }
        if total_uncorr > 0:
            out["problems"].append(
                f"{total_uncorr} uncorrectable FEC errors on downstream channels over {days}d — "
                f"physical line/signal fault, the strongest complaint evidence."
            )
        if low_snr:
            out["problems"].append(
                f"Downstream SNR dipped below {SNR_MIN_DB} dB on {len(low_snr)} samples "
                f"(min {round(min(snrs),1)} dB) — marginal signal quality."
            )
        if bad_sig:
            out["problems"].append(
                f"Downstream signal outside +/-{SIGNAL_ABS_MAX_DBMV} dBmV on {len(bad_sig)} samples — signal-level fault."
            )

    # ---- IPTV / http portal availability ----
    http = api(f"/api/http?hours={hours}&probe={PROBE}")
    if http:
        n = len(http)
        okc = sum(1 for r in http if not r.get("error") and (r.get("status_code") or 0) < 400)
        uptime = round(100 * okc / n, 2) if n else None
        out["metrics"]["http"] = {"checks": n, "uptime_pct": uptime}
        if uptime is not None and uptime < 99.0:
            out["problems"].append(
                f"IPTV portal reachability {uptime}% over {days}d (<99%) — service-affecting."
            )

    return out


def append_ledger(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    line = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "ts": rec["generated_at"],
        "reachable": rec["reachable"],
        "n_problems": len(rec.get("problems", [])),
        "problems": rec.get("problems", []),
        "metrics": rec.get("metrics", {}),
    }
    with open(LEDGER, "a") as f:
        f.write(json.dumps(line) + "\n")
    return line


def read_ledger(days=30):
    if not os.path.exists(LEDGER):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = []
    with open(LEDGER) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("date", "") >= cutoff:
                rows.append(r)
    return rows


def complaint_recommendation(history):
    """Decide whether accumulated evidence justifies a complaint.

    Conservative: we want a defensible case, not a hair-trigger. File when ANY of:
      - a physical fault signature (uncorrectable FEC / low SNR / bad signal) appears
        on >=2 distinct days in the trailing window, OR
      - sustained packet loss OR outage gaps on >=3 distinct days, OR
      - chronic under-provisioning on >=3 distinct days.
    """
    fault_days = set()
    loss_days = set()
    slow_days = set()
    for r in history:
        d = r.get("date")
        m = r.get("metrics", {})
        modem = m.get("modem", {})
        if (modem.get("total_uncorrectable_fec") or 0) > 0 \
           or (modem.get("snr_samples_below_floor") or 0) > 0 \
           or (modem.get("signal_out_of_spec_samples") or 0) > 0:
            fault_days.add(d)
        ping = m.get("ping", {})
        if (ping.get("pct_samples_with_any_loss") or 0) > 2.0 or m.get("outage_gaps"):
            loss_days.add(d)
        speed = m.get("speed", {})
        if (speed.get("tests_far_below_plan") or 0) >= 2:
            slow_days.add(d)

    reasons = []
    if len(fault_days) >= 2:
        reasons.append(f"Physical line-fault signature on {len(fault_days)} days: {sorted(fault_days)}")
    if len(loss_days) >= 3:
        reasons.append(f"Sustained loss/outages on {len(loss_days)} days: {sorted(loss_days)}")
    if len(slow_days) >= 3:
        reasons.append(f"Chronic slow speeds on {len(slow_days)} days: {sorted(slow_days)}")

    return {
        "file_complaint": bool(reasons),
        "reasons": reasons,
        "fault_days": sorted(fault_days),
        "loss_days": sorted(loss_days),
        "slow_days": sorted(slow_days),
    }


def main():
    days = int(os.environ.get("NETMON_WINDOW_DAYS", "1"))
    rec = analyze(days=days)

    print("=" * 64)
    print(f"NETMON DAILY ANALYSIS — {rec['generated_at']}  (probe={PROBE}, window={days}d)")
    print("=" * 64)

    if not rec["reachable"]:
        print(f"HUB UNREACHABLE: {rec.get('error')}")
        print("STATUS: DATA GAP — could not collect today. No verdict.")
        # still record the gap so we know monitoring itself failed
        try:
            append_ledger(rec)
        except Exception:
            pass
        return

    if rec["metrics"].get("probe_stale"):
        last = rec["metrics"].get("probe_last_seen")
        print(f"WARNING: probe '{PROBE}' is STALE (last_seen {last}). Data may be incomplete.")

    m = rec["metrics"]
    if "ping" in m:
        p = m["ping"]
        print(f"\nPING/LOSS: {p['samples']} samples | any-loss {p['pct_samples_with_any_loss']}% | "
              f"avg-loss {p['avg_loss_pct']}% | max-loss {p['max_loss_pct']}%")
        print(f"LATENCY:   p50 {p['latency_p50_ms']}ms  p95 {p['latency_p95_ms']}ms  "
              f"p99 {p['latency_p99_ms']}ms  max {p['latency_max_ms']}ms  spikes>100ms {p['spikes_over_100ms']}")
    if "speed" in m:
        s = m["speed"]
        print(f"SPEED:     down min/med {s['down_min']}/{s['down_median']} Mbps  "
              f"up min/med {s['up_min']}/{s['up_median']} Mbps  "
              f"below-plan {s['tests_far_below_plan']}/{s['tests']}  bufferbloat +{s['bufferbloat_ms']}ms")
    if "modem" in m:
        mo = m["modem"]
        print(f"MODEM:     uncorrectable-FEC {mo['total_uncorrectable_fec']}  corrected-FEC {mo['total_corrected_fec']}  "
              f"SNR-min {mo['snr_min_db']}dB  below-floor {mo['snr_samples_below_floor']}  "
              f"signal-oos {mo['signal_out_of_spec_samples']}")
    if "http" in m:
        h = m["http"]
        print(f"IPTV/HTTP: {h['checks']} checks  uptime {h['uptime_pct']}%")
    if m.get("outage_gaps"):
        print(f"OUTAGE GAPS: {len(m['outage_gaps'])}")
        for g in m["outage_gaps"]:
            print(f"   {g['from']} -> {g['to']}  ({g['minutes']} min)")

    print("\nTODAY'S PROBLEMS:")
    if rec["problems"]:
        for pr in rec["problems"]:
            print(f"  - {pr}")
    else:
        print("  (none — connection healthy today, nothing complaint-worthy)")

    append_ledger(rec)
    history = read_ledger(days=30)
    reco = complaint_recommendation(history)

    print(f"\nEVIDENCE LEDGER: {len(history)} days recorded (trailing 30d).")
    print("COMPLAINT RECOMMENDATION:")
    if reco["file_complaint"]:
        print("  >>> ENOUGH EVIDENCE TO FILE A COMPLAINT <<<")
        for r in reco["reasons"]:
            print(f"  - {r}")
        print("STATUS: COMPLAINT-READY")
    else:
        fd, ld, sd = len(reco["fault_days"]), len(reco["loss_days"]), len(reco["slow_days"])
        print(f"  Not yet. Trailing-30d problem days — fault:{fd} (need>=2), "
              f"loss/outage:{ld} (need>=3), slow:{sd} (need>=3).")
        print("STATUS: MONITORING — no complaint warranted yet")


if __name__ == "__main__":
    main()
