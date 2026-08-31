#!/usr/bin/env python3
"""Tests for the incident-report analysis.

These exist because the live connection is (happily) healthy, so the code paths
that matter most for an ISP complaint - uncorrectable FEC, low SNR, out-of-spec
signal, sustained loss, total-loss outages - never run against real data. Every
sample here is SYNTHETIC and constructed to trip exactly one detector.

Run with the venv python, no test framework needed:
    .venv/bin/python tests/test_analysis.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netmon_mcp import analysis  # noqa: E402

NOW = time.time()
HOUR = 3600.0


def report(**kwargs):
    base = dict(now=NOW, days=1, probe="test", ping=[], speed=[], modem=[],
                http=[], probes=[], alerts=[])
    base.update(kwargs)
    return analysis.incident_report(**base)


def codes(rep):
    return {f["code"] for f in rep["findings"]}


def find(rep, code):
    matches = [f for f in rep["findings"] if f["code"] == code]
    assert matches, f"expected finding {code}, got {sorted(codes(rep))}"
    return matches[0]


def ping_row(ts, target="1.1.1.1", loss=0.0, avg=20.0, probe="test"):
    return {"probe_name": probe, "ts": ts, "target": target, "sent": 4,
            "received": 4 - int(4 * loss / 100), "loss_pct": loss,
            "min_ms": avg * 0.8, "avg_ms": avg, "max_ms": avg * 1.5, "mdev_ms": 1.0}


def modem_row(ts, channel="1", uncorrectable=0, corrected=0, snr=40.0, signal=5.0,
              direction="downstream", probe="test"):
    return {"probe_name": probe, "ts": ts, "direction": direction,
            "channel_id": channel, "port_id": channel, "frequency": "411000000",
            "modulation": "QAM256", "signal_strength_dbmv": signal, "snr_db": snr,
            "corrected": corrected, "uncorrectable": uncorrectable, "error": None}


# --------------------------------------------------------------------------- #

def test_healthy_connection_is_not_complaint_worthy():
    ping = [ping_row(NOW - HOUR + i * 60) for i in range(60)]
    modem = [modem_row(NOW - HOUR + i * 900) for i in range(4)]
    rep = report(ping=ping, modem=modem)
    assert rep["verdict"]["complaint_worthy"] is False
    assert rep["packet_loss"]["pct_samples_with_loss"] == 0.0
    assert rep["modem"]["per_probe"]["test"]["downstream"]["fec"][
        "uncorrectable_accrued_in_window"] == 0


def test_uncorrectable_fec_is_differenced_not_read_as_a_lifetime_total():
    # Channel 1 starts at a large lifetime total and grows by 500 inside the
    # window; channel 2 sits at an even bigger total but never moves. Only the
    # first is a fault, and only 500 of it happened here.
    modem = []
    for i, unc in enumerate((900_000, 900_200, 900_500)):
        modem.append(modem_row(NOW - 3 * 900 + i * 900, channel="1", uncorrectable=unc))
        modem.append(modem_row(NOW - 3 * 900 + i * 900, channel="2", uncorrectable=5_000_000))
    rep = report(modem=modem)
    fec = rep["modem"]["per_probe"]["test"]["downstream"]["fec"]
    assert fec["uncorrectable_accrued_in_window"] == 500
    assert [c["channel"] for c in fec["channels_with_uncorrectable"]] == ["1"]
    assert fec["channels_with_uncorrectable"][0]["lifetime_total_at_last_poll"] == 900_500
    finding = find(rep, "MODEM_UNCORRECTABLE_FEC_DOWNSTREAM")
    assert finding["severity"] == analysis.MAJOR
    assert rep["verdict"]["complaint_worthy"] is True
    assert "500 uncorrectable FEC errors" in finding["quotable"]


def test_counter_reset_is_not_read_as_negative_errors():
    modem = [
        modem_row(NOW - 2 * 900, channel="1", uncorrectable=4000),
        modem_row(NOW - 900, channel="1", uncorrectable=0),      # modem rebooted
        modem_row(NOW, channel="1", uncorrectable=7),
    ]
    rep = report(modem=modem)
    fec = rep["modem"]["per_probe"]["test"]["downstream"]["fec"]
    assert fec["uncorrectable_accrued_in_window"] == 7, "reset must not subtract"
    assert len(fec["counter_resets"]) == 1
    assert fec["counter_resets"][0]["from"] == 4000


def test_low_snr_and_out_of_spec_signal_are_flagged_with_timestamps():
    modem = [modem_row(NOW - 900, snr=40.0, signal=5.0),
             modem_row(NOW, snr=29.5, signal=-19.0)]
    rep = report(modem=modem)
    snr = find(rep, "MODEM_LOW_SNR_DOWNSTREAM")
    assert "29.5 dB" in snr["quotable"] and analysis.iso(NOW)[:16] in snr["quotable"]
    signal = find(rep, "MODEM_SIGNAL_OUT_OF_SPEC_DOWNSTREAM")
    assert "-19.0 dBmV" in signal["quotable"]
    assert rep["verdict"]["worst_severity"] == analysis.MAJOR


def test_upstream_signal_uses_its_own_spec_window():
    # 5 dBmV is fine downstream and badly out of spec upstream.
    rep = report(modem=[modem_row(NOW - 900, direction="upstream", signal=5.0, snr=None),
                        modem_row(NOW, direction="upstream", signal=5.0, snr=None)])
    assert "MODEM_SIGNAL_OUT_OF_SPEC_UPSTREAM" in codes(rep)
    assert "MODEM_SIGNAL_OUT_OF_SPEC_DOWNSTREAM" not in codes(rep)


def test_sustained_loss_window_matches_the_hubs_own_alert_rule():
    ping = [ping_row(NOW - HOUR + i * 60) for i in range(60)]
    bad_start = NOW - 20 * 60
    ping += [ping_row(bad_start + i * 60, loss=25.0) for i in range(15)]
    rep = report(ping=ping)
    loss = rep["packet_loss"]
    assert loss["windows_of_15min_over_10.0pct"] >= 1
    assert loss["worst_windows"] and loss["worst_windows"][0]["mean_loss_pct"] > 10
    finding = find(rep, "PACKET_LOSS_SUSTAINED")
    assert finding["severity"] == analysis.MAJOR
    assert rep["verdict"]["complaint_worthy"] is True
    assert loss["worst_loss_episodes"][0]["duration_sec"] == 15 * 60


def test_sustained_loss_is_found_when_it_straddles_a_clock_aligned_boundary():
    # Regression: with fixed 15-minute buckets, a solid 15-minute episode
    # centred on a bucket edge got split in half and each half averaged back
    # under the threshold, so the hub would have pushed an alert the report
    # denied. The window has to slide.
    edge = ((NOW - HOUR) // (15 * 60)) * (15 * 60)
    ping = [ping_row(edge - 40 * 60 + i * 60) for i in range(20)]          # clean
    ping += [ping_row(edge - 7 * 60 + i * 60, loss=25.0) for i in range(15)]
    ping += [ping_row(edge + 10 * 60 + i * 60) for i in range(20)]         # clean
    rep = report(ping=ping)
    loss = rep["packet_loss"]
    assert loss["windows_of_15min_over_10.0pct"] == 1, loss["worst_windows"]
    assert loss["worst_windows"][0]["mean_loss_pct"] == 25.0
    assert find(rep, "PACKET_LOSS_SUSTAINED")["severity"] == analysis.MAJOR


def test_overlapping_bad_windows_collapse_to_one_episode():
    # One 30-minute episode is one problem, not the ~30 sliding windows that
    # each contain part of it.
    ping = [ping_row(NOW - HOUR + i * 60, loss=40.0) for i in range(30)]
    ping += [ping_row(NOW - 30 * 60 + i * 60) for i in range(30)]
    loss = report(ping=ping)["packet_loss"]
    assert loss["windows_of_15min_over_10.0pct"] == 2, "30 min holds 2 disjoint 15s"
    assert len(loss["worst_windows"]) == 2


def test_total_loss_outage_needs_every_target_dead():
    start = NOW - 30 * 60
    ping = []
    for i in range(30):
        ts = start + i * 60
        dead = 5 <= i < 12
        ping.append(ping_row(ts, "1.1.1.1", loss=100.0 if dead else 0.0))
        # 8.8.8.8 keeps answering for one of the dead minutes, so that minute is
        # a single-target problem and must not count as a connectivity outage.
        ping.append(ping_row(ts, "8.8.8.8", loss=100.0 if (dead and i != 7) else 0.0))
    rep = report(ping=ping)
    outages = rep["outages"]["connectivity_outages"]
    assert len(outages) == 2, outages
    assert sum(o["duration_sec"] for o in outages) == 6 * 60
    assert find(rep, "CONNECTIVITY_OUTAGE")["severity"] == analysis.CRITICAL


def test_reporting_gap_is_not_reported_as_an_outage():
    # Samples only in the last 10 minutes: a big hole, but no loss recorded.
    ping = [ping_row(NOW - 10 * 60 + i * 60) for i in range(10)]
    rep = report(ping=ping)
    assert rep["outages"]["connectivity_outage_count"] == 0
    gaps = rep["outages"]["probe_reporting_gaps"]["test"]
    assert gaps and gaps[0]["before_first_sample"] is True
    assert "CONNECTIVITY_OUTAGE" not in codes(rep)


def test_http_target_downtime_is_measured_and_quoted():
    checks = []
    for i in range(60):
        ts = NOW - HOUR + i * 60
        failing = 10 <= i < 16
        checks.append({"probe_name": "test", "ts": ts, "url": "http://portal.example",
                       "status_code": None if failing else 200,
                       "latency_ms": None if failing else 250.0,
                       "error": "Name or service not known" if failing else None})
    rep = report(http=checks)
    target = rep["outages"]["http_targets"]["http://portal.example"]
    assert target["failed_checks"] == 6
    assert target["uptime_pct"] == 90.0
    assert target["worst_failure_episodes"][0]["duration_sec"] == 6 * 60
    assert find(rep, "HTTP_TARGET_UNAVAILABLE")["severity"] == analysis.MAJOR


def test_latency_percentiles_and_high_latency_counting():
    ping = [ping_row(NOW - HOUR + i * 60, avg=20.0) for i in range(90)]
    ping += [ping_row(NOW - 10 * 60 + i * 60, avg=400.0) for i in range(10)]
    rep = report(ping=ping)
    stats = rep["latency"]["avg_ms"]
    assert stats["p50"] == 20.0
    assert stats["p99"] == 400.0
    assert rep["latency"]["samples_over_100ms"] == 10
    assert find(rep, "LATENCY_HIGH")["severity"] == analysis.MAJOR


def test_implausibly_fast_ping_is_flagged_per_probe_and_never_complaint_worthy():
    ping = [ping_row(NOW - HOUR + i * 60, avg=0.5, probe="fake") for i in range(30)]
    ping += [ping_row(NOW - HOUR + i * 60, avg=25.0, probe="real") for i in range(30)]
    rep = report(ping=ping)
    suspect = rep["latency"]["implausibly_fast_targets"]
    assert [s["probe"] for s in suspect] == ["fake"]
    finding = find(rep, "MEASUREMENT_ICMP_ANSWERED_LOCALLY")
    assert finding["severity"] == analysis.INFO
    assert rep["verdict"]["complaint_worthy"] is False
    # Surfaced as a caveat, never as something to read out to the ISP.
    assert any("physically impossible" in c for c in rep["verdict"]["data_caveats"])
    assert not any("physically impossible" in q for q in rep["verdict"]["quotable_facts"])


def test_zero_throughput_test_is_called_out_and_kept_in_the_stats():
    speed = [{"probe_name": "test", "ts": NOW - HOUR, "download_mbps": 0.0,
              "upload_mbps": 0.0, "idle_latency_ms": 30.0, "loaded_latency_avg_ms": 31.0,
              "loaded_latency_max_ms": 40.0, "server_name": "s1", "error": None},
             {"probe_name": "test", "ts": NOW, "download_mbps": 300.0,
              "upload_mbps": 30.0, "idle_latency_ms": 30.0, "loaded_latency_avg_ms": 31.0,
              "loaded_latency_max_ms": 40.0, "server_name": "s2", "error": None}]
    rep = report(speed=speed)
    assert rep["speed"]["download_mbps"]["min"] == 0.0
    assert len(rep["speed"]["zero_throughput_tests"]) == 1
    assert "SPEED_ZERO_THROUGHPUT" in codes(rep)


def test_plan_shortfall_is_computed_when_the_plan_is_configured(monkeypatched=None):
    from netmon_mcp import config
    original = config.PLAN_DOWN_MBPS
    config.PLAN_DOWN_MBPS = 1000.0
    try:
        speed = [{"probe_name": "test", "ts": NOW - i * HOUR, "download_mbps": 400.0,
                  "upload_mbps": 30.0, "idle_latency_ms": 30.0,
                  "loaded_latency_avg_ms": 31.0, "loaded_latency_max_ms": 40.0,
                  "server_name": "s", "error": None} for i in range(5)]
        rep = report(speed=speed)
        vs_plan = rep["speed"]["download_vs_plan"]
        assert vs_plan["median_pct_of_plan"] == 40.0
        assert vs_plan["tests_below_80pct"] == 5
        assert find(rep, "SPEED_BELOW_PLAN")["severity"] == analysis.MAJOR
        assert rep["verdict"]["complaint_worthy"] is True
    finally:
        config.PLAN_DOWN_MBPS = original


def test_bufferbloat_delta_is_loaded_minus_idle():
    speed = [{"probe_name": "test", "ts": NOW - i * HOUR, "download_mbps": 300.0,
              "upload_mbps": 30.0, "idle_latency_ms": 20.0,
              "loaded_latency_avg_ms": 320.0, "loaded_latency_max_ms": 600.0,
              "server_name": "s", "error": None} for i in range(5)]
    rep = report(speed=speed)
    assert rep["speed"]["bufferbloat"]["loaded_minus_idle_ms"]["p50"] == 300.0
    assert "BUFFERBLOAT" in codes(rep)


def test_modem_channels_are_never_mixed_across_probes():
    modem = [modem_row(NOW - 900, channel="1", uncorrectable=0, probe="a"),
             modem_row(NOW, channel="1", uncorrectable=90, probe="a"),
             modem_row(NOW - 900, channel="1", uncorrectable=0, probe="b"),
             modem_row(NOW, channel="1", uncorrectable=0, probe="b")]
    rep = report(probe=None, modem=modem)
    per_probe = rep["modem"]["per_probe"]
    assert set(per_probe) == {"a", "b"}
    assert per_probe["a"]["downstream"]["fec"]["uncorrectable_accrued_in_window"] == 90
    assert per_probe["b"]["downstream"]["fec"]["uncorrectable_accrued_in_window"] == 0
    assert find(rep, "MODEM_UNCORRECTABLE_FEC_DOWNSTREAM")["evidence"]["probe"] == "a"


def test_empty_hub_produces_a_report_rather_than_an_exception():
    rep = report()
    assert rep["packet_loss"] == {"samples": 0}
    assert rep["findings"] == []
    assert rep["verdict"]["complaint_worthy"] is False


def test_percentile_interpolates():
    assert analysis._percentile([1, 2, 3, 4], 50) == 2.5
    assert analysis._percentile([10], 99) == 10.0
    assert analysis._percentile([], 50) is None


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a raised error is a failure too
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
