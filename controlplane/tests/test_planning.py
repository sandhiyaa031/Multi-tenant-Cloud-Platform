"""Reading aids: how many pairs a verdict would have needed, and how often the canary's rule
fires on telemetry in which nothing changed."""
import math
import statistics
from datetime import datetime, timedelta, timezone

import pytest

from app.planning import MAX_PAIRS, canary_false_alarms, pairs_needed, twin_planning
from dbpilot_core.gate import t_quantile

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def windows(series: dict[str, list[tuple[float, int]]], every_s: int = 20) -> list[dict]:
    n = len(next(iter(series.values())))
    return [{"end": T0 + timedelta(seconds=every_s * i), "keys": {k: v[i] for k, v in series.items()}} for i in range(n)]


# ── pairs needed ─────────────────────────────────────────────────────────────

def test_one_pair_gives_no_estimate():
    assert pairs_needed([1.02], 1.05, 5) is None


def test_the_estimate_satisfies_the_gates_own_condition_and_one_pair_fewer_does_not():
    ratios = [0.95, 1.06, 0.99, 1.04, 0.93, 1.02, 1.07, 0.96]   # pairs that disagree by about 5%
    got = pairs_needed(ratios, 1.05, 5)
    assert got["reachable"] and got["needed"] > len(ratios)
    logs = [math.log(r) for r in ratios]
    mean = sum(logs) / len(logs)

    def upper(n):
        return mean + t_quantile(1 - 0.05 / (n * 5) / 2, n - 1) * got["sd_log"] / math.sqrt(n)

    assert upper(got["needed"]) <= math.log(1.05) < upper(got["needed"] - 1)


def _brute_force(ratios, limit, comparisons):
    logs = [math.log(r) for r in ratios]
    mean, sd = sum(logs) / len(logs), statistics.stdev(logs)
    for n in range(2, MAX_PAIRS + 1):
        if mean + t_quantile(1 - 0.05 / (n * comparisons) / 2, n - 1) * sd / math.sqrt(n) <= math.log(limit):
            return n
    return None


@pytest.mark.parametrize("ratios", [
    [0.95, 1.06, 0.99, 1.04, 0.93, 1.02, 1.07, 0.96], [1.000, 1.002, 0.999, 1.001], [0.90, 1.12, 0.95, 1.08],
    [0.98, 1.01, 1.03, 0.97, 1.00], [0.80, 1.30, 0.70, 1.40, 0.90, 1.10], [1.0, 1.3, 0.8, 1.2, 0.9, 1.15, 0.85]])
@pytest.mark.parametrize("comparisons", [1, 5])
def test_the_fast_search_finds_exactly_what_a_scan_of_every_size_finds(ratios, comparisons):
    assert pairs_needed(ratios, 1.05, comparisons)["needed"] == _brute_force(ratios, 1.05, comparisons)


def test_the_estimate_is_fast_enough_to_sit_in_a_page_request():
    import time
    started = time.monotonic()
    for _ in range(5):                                     # a verdict has about five keys
        pairs_needed([0.8, 1.3, 0.7, 1.4, 0.9, 1.1, 1.2, 0.85], 1.05, 5)
    assert time.monotonic() - started < 0.6


def test_pairs_that_agree_need_few_and_noisier_pairs_need_more():
    steady = pairs_needed([1.000, 1.002, 0.999, 1.001], 1.05, 5)["needed"]
    noisy = pairs_needed([0.90, 1.12, 0.95, 1.08], 1.05, 5)["needed"]
    assert steady <= 4 < noisy


def test_a_ratio_already_above_the_limit_cannot_be_shown_below_it():
    got = pairs_needed([1.20, 1.25, 1.18], 1.05, 5)
    assert got["reachable"] is False and got["needed"] is None


def test_planning_covers_every_measured_key_of_a_per_tenant_verdict_only():
    verdict = {"mode": "per_tenant", "effects": {
        "t_a/OLTP": {"pair_ratios": {"1": 1.01, "2": 0.98, "3": 1.03}},
        "t_b/OLTP": {"pair_ratios": {"1": 1.00}},            # one pair: nothing to estimate from
        "t_c/OLAP": {"pair_ratios": {}}}}
    assert set(twin_planning(verdict)) == {"t_a/OLTP"}
    assert twin_planning({"mode": "aggregate", "effects": verdict["effects"]}) == {}


# ── canary false alarms ──────────────────────────────────────────────────────

def test_steady_telemetry_raises_no_alarm():
    got = canary_false_alarms(windows({"t_a/OLTP": [(20.0, 200)] * 12}), [])
    assert got["sequences"] == 7 and got["alarms"] == 0
    assert got["keys"]["t_a/OLTP"]["median_txns_per_window"] == 200


def test_a_class_whose_p95_jumps_by_itself_is_reported_as_a_false_alarm():
    # Nothing changed, yet two of three windows after a quiet baseline sit 30% higher.
    jumpy = [(100.0, 7)] * 3 + [(130.0, 7), (131.0, 7), (100.0, 7)]
    got = canary_false_alarms(windows({"t_a/OLAP": jumpy, "t_b/OLTP": [(20.0, 300)] * 6}), [])
    assert got["sequences"] == 1 and got["alarms"] == 1
    assert got["keys"]["t_a/OLAP"]["alarms"] == 1 and got["keys"]["t_b/OLTP"]["alarms"] == 0


def test_windows_around_a_real_change_are_not_counted_as_noise():
    jumpy = [(100.0, 50)] * 3 + [(130.0, 50)] * 3
    change = [T0 + timedelta(seconds=55)]                      # a change was applied before the jump
    assert canary_false_alarms(windows({"t_a/OLAP": jumpy}), change)["sequences"] == 0


def test_windows_with_too_few_transactions_are_not_judged():
    sparse = [(100.0, 50)] * 3 + [(200.0, 2)] * 3
    assert canary_false_alarms(windows({"t_a/OLAP": sparse}), [])["alarms"] == 0


def test_a_gap_in_telemetry_breaks_the_run():
    w = windows({"t_a/OLTP": [(20.0, 200)] * 6})
    for late in w[3:]:
        late["end"] += timedelta(minutes=30)
    assert canary_false_alarms(w, [])["sequences"] == 0


# ── through the API ──────────────────────────────────────────────────────────

def test_canary_noise_endpoint_answers_for_a_cluster_without_telemetry(client, org):
    r = client.get(f"/api/v1/clusters/{org.cluster()}/canary-noise", headers=org.admin)
    assert r.status_code == 200
    assert r.json()["sequences"] == 0 and r.json()["keys"] == {}
