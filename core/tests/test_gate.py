"""The gate is tested on synthetic latency streams whose true effect is known."""
import numpy as np

from dbpilot_core.gate import GatePolicy, decide

POLICY = GatePolicy(n_boot=400)


def stream(mean_ms: float, rate: float = 20, duration: float = 120, seed: int = 0, noise: float = 0.3):
    """Poisson arrivals with log-normal latencies, like a real request stream."""
    rng = np.random.default_rng(seed)
    n = rng.poisson(rate * duration)
    times = np.sort(rng.uniform(0, duration, n))
    latencies = rng.lognormal(np.log(mean_ms), noise, n)
    return list(zip(times.tolist(), latencies.tolist()))


def arms(factors: dict, base=None, seed=0):
    """Builds control and treatment where treatment latency = control × factor, per key."""
    base = base or {k: 10.0 for k in factors}
    control = {k: stream(base[k], seed=seed + i) for i, k in enumerate(factors)}
    treatment = {k: stream(base[k] * f, seed=seed + 100 + i) for i, (k, f) in enumerate(factors.items())}
    return control, treatment


A, B, C = ("a", "OLAP"), ("b", "OLTP"), ("c", "OLTP")


def test_approves_when_target_benefits_and_neighbours_are_unaffected():
    control, treatment = arms({A: 0.5, B: 1.0, C: 1.0})
    verdict = decide(control, treatment, "a", POLICY)
    assert verdict.decision == "APPROVE"
    assert verdict.effects["a/OLAP"]["status"] == "BENEFITS"
    assert verdict.effects["b/OLTP"]["status"] == "SAFE"


def test_rejects_when_target_benefits_but_a_neighbour_is_harmed():
    """The case the whole project is about."""
    control, treatment = arms({A: 0.5, B: 1.4, C: 1.0})
    verdict = decide(control, treatment, "a", POLICY)
    assert verdict.decision == "REJECT"
    assert verdict.effects["b/OLTP"]["status"] == "HARMED"
    assert any("b/OLTP" in r for r in verdict.reasons)


def test_aggregate_gate_approves_the_same_harmful_change():
    """An OLAP win hides an OLTP loss when only the total is judged."""
    base = {A: 400.0, B: 10.0, C: 10.0}
    control, treatment = arms({A: 0.5, B: 1.4, C: 1.0}, base=base)
    assert decide(control, treatment, "a", POLICY, mode="aggregate").decision == "APPROVE"
    assert decide(control, treatment, "a", POLICY, mode="per_tenant").decision == "REJECT"


def test_rejects_when_nobody_is_harmed_but_nothing_improves():
    control, treatment = arms({A: 1.0, B: 1.0})
    assert decide(control, treatment, "a", POLICY).decision == "REJECT"


def test_borderline_regression_is_inconclusive_not_safe():
    # A true +5% effect sits exactly on the margin: the interval must straddle it.
    control, treatment = arms({A: 0.5, B: 1.05})
    verdict = decide(control, treatment, "a", POLICY)
    assert verdict.decision == "INCONCLUSIVE"
    assert verdict.effects["b/OLTP"]["status"] == "UNCERTAIN"


def test_too_few_samples_is_inconclusive():
    control = {A: stream(10, seed=1), B: stream(10, rate=0.1, seed=2)}
    treatment = {A: stream(5, seed=3), B: stream(10, rate=0.1, seed=4)}
    verdict = decide(control, treatment, "a", POLICY)
    assert verdict.decision == "INCONCLUSIVE"
    assert any("too few samples" in r for r in verdict.reasons)


def test_identical_arms_are_never_approved_or_called_harmful():
    """A/A: with no real change, harm must (almost) never be 'shown'."""
    harmed = 0
    for seed in range(20):
        control, treatment = arms({A: 1.0, B: 1.0, C: 1.0}, seed=seed * 7)
        verdict = decide(control, treatment, "a", POLICY, seed=seed)
        assert verdict.decision != "APPROVE"
        harmed += any(e["status"] == "HARMED" for e in verdict.effects.values())
    assert harmed <= 1


def test_costly_to_undo_actions_face_a_tighter_margin():
    control, treatment = arms({A: 0.5, B: 1.02}, seed=5)
    assert decide(control, treatment, "a", POLICY, cheap_to_undo=True).decision == "APPROVE"
    assert decide(control, treatment, "a", POLICY, cheap_to_undo=False).decision != "APPROVE"


def test_budgets_override_latency_wins():
    control, treatment = arms({A: 0.5, B: 1.0})
    assert decide(control, treatment, "a", POLICY, wal_ratio=1.6).decision == "REJECT"
    assert decide(control, treatment, "a", POLICY, storage_delta_bytes=5 * 1024**3).decision == "REJECT"


def test_breaking_an_slo_that_control_met_is_harm():
    # +4% is inside the regression margin, but it pushes p99 across the tenant's SLO.
    control, treatment = arms({A: 0.5, B: 1.04}, seed=11)
    p99_control = float(np.percentile([s[1] for s in control[B]], 99))
    p99_treatment = float(np.percentile([s[1] for s in treatment[B]], 99))
    assert p99_treatment > p99_control
    threshold = (p99_control + p99_treatment) / 2
    verdict = decide(control, treatment, "a", POLICY, slo_ms={B: (99, threshold)})
    assert verdict.decision == "REJECT"
    assert any("SLO" in r for r in verdict.reasons)


def test_instance_wide_action_needs_some_benefit_and_no_harm():
    control, treatment = arms({A: 0.6, B: 1.0})
    assert decide(control, treatment, None, POLICY).decision == "APPROVE"
    control, treatment = arms({A: 0.6, B: 1.5})
    assert decide(control, treatment, None, POLICY).decision == "REJECT"


def test_contract_bounds_what_the_canary_may_observe():
    control, treatment = arms({A: 0.5, B: 1.0})
    verdict = decide(control, treatment, "a", POLICY)
    assert verdict.contract["a/OLAP"] == round(1.0 + POLICY.contract_tolerance, 3)
    assert verdict.contract["b/OLTP"] <= 1 + POLICY.max_regression + POLICY.contract_tolerance


def test_warmup_is_excluded():
    control = {A: stream(10, seed=1)}
    # Treatment is terrible for the first 30 s only (a cold cache), fine afterwards.
    cold = [(t, l * 20) for t, l in stream(10, duration=30, seed=2)]
    warm = [(t + 30, l * 0.5) for t, l in stream(10, duration=90, seed=3)]
    treatment = {A: cold + warm}
    with_warmup = GatePolicy(n_boot=400, warmup_s=30)
    assert decide(control, treatment, "a", with_warmup).decision == "APPROVE"
    assert decide(control, treatment, "a", POLICY).decision != "APPROVE"


# ── Several replays judged together; calibration ─────────────────────────────

def test_pooling_keeps_replays_apart_and_drops_each_warmup():
    from dbpilot_core.gate import pool

    acc: dict = {}
    pool(acc, {("t", "OLTP"): [(1.0, 10.0), (20.0, 11.0)]}, 1, warmup_s=5)
    pool(acc, {("t", "OLTP"): [(2.0, 12.0), (21.0, 13.0)]}, 2, warmup_s=5)
    times = [t for t, _ in acc[("t", "OLTP")]]
    assert [lat for _, lat in acc[("t", "OLTP")]] == [11.0, 13.0]       # warm-up samples of both replays dropped
    assert times[1] - times[0] >= 1_000_000 - 1                           # never share a time bucket


def test_more_replays_narrow_the_interval():
    """Why an inconclusive verdict is worth another replay."""
    from dbpilot_core.gate import pool

    def interval(blocks: int) -> float:
        control, treatment = {}, {}
        for b in range(1, blocks + 1):
            c, t = arms({A: 1.0, B: 1.0}, seed=b * 1000)
            pool(control, c, b, 0)
            pool(treatment, t, b, 0)
        e = decide(control, treatment, "a", POLICY).effects["b/OLTP"]
        return e["hi"] - e["lo"]

    assert interval(4) < interval(1) * 0.75


def test_calibration_needs_history_and_is_bounded():
    from dbpilot_core.gate import calibrated_tolerance

    assert calibrated_tolerance([], 0.10) == (0.10, 0)
    assert calibrated_tolerance([(1.0, 1.4)] * 7, 0.10) == (0.10, 7)                 # too little history
    assert calibrated_tolerance([(1.0, 1.02)] * 10, 0.10) == (0.10, 10)             # never below the default
    tolerance, n = calibrated_tolerance([(0.5, 0.6)] * 9 + [(1.0, 1.0)], 0.10)      # twin off by 20% nine times in ten
    assert n == 10 and abs(tolerance - 0.20) < 1e-9
    assert calibrated_tolerance([(1.0, 3.0)] * 10, 0.10)[0] == 0.5                  # capped
    assert calibrated_tolerance([(None, 1.0), (0.0, 1.0)] * 10, 0.10) == (0.10, 0)  # unusable pairs ignored
