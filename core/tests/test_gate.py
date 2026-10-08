"""The gate is tested on synthetic latency streams whose true effect is known."""
import numpy as np

from dbpilot_core.gate import GatePolicy, decide, pool, t_quantile

POLICY = GatePolicy()
PAIRS = 4
ORDER = {b: b % 2 == 0 for b in range(1, 13)}  # the arm that runs first alternates, as in the engine


def stream(mean_ms: float, rate: float = 20, duration: float = 120, seed: int = 0, noise: float = 0.3):
    """Poisson arrivals with log-normal latencies, like a real request stream."""
    rng = np.random.default_rng(seed)
    n = rng.poisson(rate * duration)
    times = np.sort(rng.uniform(0, duration, n))
    latencies = rng.lognormal(np.log(mean_ms), noise, n)
    return list(zip(times.tolist(), latencies.tolist()))


def arms(factors: dict, base=None, seed=0, pairs=PAIRS, rate=20, arm_shift=None):
    """Builds `pairs` replay pairs, pooled as the engine pools them. In each pair the
    treatment latency = control latency x factor, per key. `arm_shift` maps a replay
    number to a factor applied to every key of its treatment arm: a machine that was
    slower or faster while that arm ran."""
    base = base or {k: 10.0 for k in factors}
    control, treatment = {}, {}
    for block in range(1, pairs + 1):
        shift = (arm_shift or {}).get(block, 1.0)
        s = seed + block * 1000
        pool(control, {k: stream(base[k], rate, seed=s + i) for i, k in enumerate(factors)}, block, 0)
        pool(treatment, {k: stream(base[k] * f * shift, rate, seed=s + 100 + i) for i, (k, f) in enumerate(factors.items())},
             block, 0)
    return control, treatment


def judge(control, treatment, target, policy=POLICY, **options):
    return decide(control, treatment, target, policy, treatment_first=ORDER, **options)


A, B, C = ("a", "OLAP"), ("b", "OLTP"), ("c", "OLTP")


def test_approves_when_target_benefits_and_neighbours_are_unaffected():
    control, treatment = arms({A: 0.5, B: 1.0, C: 1.0}, pairs=8, rate=100)
    verdict = judge(control, treatment, "a")
    assert verdict.decision == "APPROVE"
    assert verdict.effects["a/OLAP"]["status"] == "BENEFITS"
    assert verdict.effects["b/OLTP"]["status"] == "SAFE"


def test_rejects_when_target_benefits_but_a_neighbour_is_harmed():
    """The case the whole project is about."""
    control, treatment = arms({A: 0.5, B: 1.4, C: 1.0})
    verdict = judge(control, treatment, "a")
    assert verdict.decision == "REJECT"
    assert verdict.effects["b/OLTP"]["status"] == "HARMED"
    assert any("b/OLTP" in r for r in verdict.reasons)


def test_aggregate_gate_approves_the_same_harmful_change():
    """An OLAP win hides an OLTP loss when only the total is judged."""
    base = {A: 400.0, B: 10.0, C: 10.0}
    control, treatment = arms({A: 0.5, B: 1.4, C: 1.0}, base=base)
    assert judge(control, treatment, "a", mode="aggregate").decision == "APPROVE"
    assert judge(control, treatment, "a", mode="per_tenant").decision == "REJECT"


def test_rejects_when_nobody_is_harmed_but_nothing_improves():
    control, treatment = arms({A: 1.0, B: 1.0}, pairs=8, rate=100)
    assert judge(control, treatment, "a").decision == "REJECT"


def test_borderline_regression_is_inconclusive_not_safe():
    # A true +5% effect sits exactly on the margin: the interval must straddle it.
    control, treatment = arms({A: 0.5, B: 1.05})
    verdict = judge(control, treatment, "a")
    assert verdict.decision == "INCONCLUSIVE"
    assert verdict.effects["b/OLTP"]["status"] == "UNCERTAIN"


def test_too_few_samples_is_inconclusive():
    control, treatment = {}, {}
    pool(control, {A: stream(10, seed=1), B: stream(10, rate=0.1, seed=2)}, 1, 0)
    pool(treatment, {A: stream(5, seed=3), B: stream(10, rate=0.1, seed=4)}, 1, 0)
    verdict = judge(control, treatment, "a")
    assert verdict.decision == "INCONCLUSIVE"
    assert any("too few samples" in r for r in verdict.reasons)


def test_identical_arms_are_never_approved_or_called_harmful():
    """A/A: with no real change, harm must (almost) never be 'shown'."""
    harmed = 0
    for seed in range(20):
        control, treatment = arms({A: 1.0, B: 1.0, C: 1.0}, seed=seed * 7)
        verdict = judge(control, treatment, "a")
        assert verdict.decision != "APPROVE"
        harmed += any(e["status"] == "HARMED" for e in verdict.effects.values())
    assert harmed <= 1


def test_costly_to_undo_actions_face_a_tighter_margin():
    control, treatment = arms({A: 0.5, B: 1.03}, seed=5, pairs=8, rate=200)
    assert judge(control, treatment, "a", cheap_to_undo=True).decision == "APPROVE"
    assert judge(control, treatment, "a", cheap_to_undo=False).decision != "APPROVE"


def test_budgets_override_latency_wins():
    control, treatment = arms({A: 0.5, B: 1.0})
    assert judge(control, treatment, "a", wal_ratio=1.6).decision == "REJECT"
    assert judge(control, treatment, "a", storage_delta_bytes=5 * 1024**3).decision == "REJECT"


def test_breaking_an_slo_that_control_met_is_harm():
    # +4% is inside the regression margin, but it pushes p99 across the tenant's SLO in every pair.
    control, treatment = arms({A: 0.5, B: 1.04}, seed=11, pairs=8, rate=200)
    threshold = 10.0 * np.exp(0.3 * 2.326) * 1.02  # the true p99 of the control stream, plus 2%
    verdict = judge(control, treatment, "a", slo_ms={B: (99, threshold)})
    assert verdict.decision == "REJECT"
    assert any("SLO" in r for r in verdict.reasons)


def test_instance_wide_action_needs_some_benefit_and_no_harm():
    control, treatment = arms({A: 0.6, B: 1.0}, pairs=8, rate=100)
    assert judge(control, treatment, None).decision == "APPROVE"
    control, treatment = arms({A: 0.6, B: 1.5})
    assert judge(control, treatment, None).decision == "REJECT"


def test_contract_bounds_what_the_canary_may_observe():
    control, treatment = arms({A: 0.5, B: 1.0})
    verdict = judge(control, treatment, "a")
    assert verdict.contract["a/OLAP"] == round(1.0 + POLICY.contract_tolerance, 3)
    assert verdict.contract["b/OLTP"] <= 1 + POLICY.max_regression + POLICY.contract_tolerance


def test_warmup_is_excluded():
    control, treatment = {}, {}
    for block in range(1, PAIRS + 1):
        pool(control, {A: stream(10, seed=block)}, block, 0)
        # Treatment is terrible for the first 30 s only (a cold cache), fine afterwards.
        cold = [(t, l * 20) for t, l in stream(10, duration=30, seed=block + 50)]
        warm = [(t + 30, l * 0.5) for t, l in stream(10, duration=90, seed=block + 90)]
        pool(treatment, {A: cold + warm}, block, 0)
    assert judge(control, treatment, "a", GatePolicy(warmup_s=30)).decision == "APPROVE"
    assert judge(control, treatment, "a").decision != "APPROVE"


# ── Replay pairs are the unit of evidence ────────────────────────────────────

def test_one_replay_pair_decides_nothing_however_large_the_effect():
    """With one pair there is no way to tell an effect of the action from the two arms
    having run at different moments."""
    for factors in ({A: 0.5, B: 1.0}, {A: 0.5, B: 3.0}):
        control, treatment = arms(factors, pairs=1)
        verdict = judge(control, treatment, "a")
        assert verdict.decision == "INCONCLUSIVE"
        assert all(e["status"] == "UNCERTAIN" and e["lo"] is None for e in verdict.effects.values())
    assert judge(*arms({A: 0.5, B: 1.0}, pairs=1), "a", mode="aggregate").decision == "INCONCLUSIVE"


def test_one_disturbed_arm_is_not_harm():
    """The machine was three times slower while one treatment arm ran. Every request in that
    arm is slow, so the scatter inside the replay says the slowdown is certain; the other
    pairs say it is not the action."""
    control, treatment = arms({A: 1.0, B: 1.0, C: 1.0}, pairs=3, arm_shift={2: 3.0})
    verdict = judge(control, treatment, "a")
    assert verdict.decision == "INCONCLUSIVE"
    assert not any(e["status"] == "HARMED" for e in verdict.effects.values())


def test_harm_must_be_seen_in_both_arm_orders():
    control, treatment = arms({A: 0.5, B: 1.4})
    assert judge(control, treatment, "a").decision == "REJECT"
    # The same measurements, but the treatment arm always ran second: the slowdown could
    # belong to running second, so it is not yet harm.
    same_order = decide(control, treatment, "a", POLICY, treatment_first={b: False for b in range(1, PAIRS + 1)})
    assert same_order.decision == "INCONCLUSIVE"
    assert any("arm order reversed" in r for r in same_order.reasons)
    assert decide(control, treatment, "a", POLICY).decision == "INCONCLUSIVE"  # order unknown: cannot confirm


def test_interval_reflects_disagreement_between_pairs():
    """Pairs that agree give a narrow interval; the same pairs with arm-to-arm shifts of a
    few percent give a wide one, although every replay has the same number of requests."""
    def width(arm_shift):
        e = judge(*arms({A: 1.0, B: 1.0}, pairs=6, arm_shift=arm_shift), "a").effects["b/OLTP"]
        return e["hi"] - e["lo"]

    steady = width(None)
    shifted = width({1: 1.10, 2: 0.92, 3: 1.06, 4: 0.90, 5: 1.12, 6: 0.95})
    assert shifted > 2 * steady


def test_pooling_keeps_replays_apart_and_drops_each_warmup():
    acc: dict = {}
    pool(acc, {("t", "OLTP"): [(1.0, 10.0), (20.0, 11.0)]}, 1, warmup_s=5)
    pool(acc, {("t", "OLTP"): [(2.0, 12.0), (21.0, 13.0)]}, 2, warmup_s=5)
    times = [t for t, _ in acc[("t", "OLTP")]]
    assert [lat for _, lat in acc[("t", "OLTP")]] == [11.0, 13.0]       # warm-up samples of both replays dropped
    assert times[1] - times[0] >= 1_000_000 - 1                           # each replay in a range of its own


def test_more_replays_narrow_the_interval():
    """Why an inconclusive verdict is worth another replay."""
    def interval(blocks: int) -> float:
        e = judge(*arms({A: 1.0, B: 1.0}, pairs=blocks), "a").effects["b/OLTP"]
        return e["hi"] - e["lo"]

    assert interval(8) < interval(3) * 0.5


def test_t_quantile_matches_published_values():
    for p, df, expected in ((0.975, 1, 12.706), (0.975, 2, 4.303), (0.975, 5, 2.571), (0.975, 10, 2.228),
                            (0.975, 30, 2.042), (0.995, 3, 5.841), (0.9995, 2, 31.599), (0.9995, 9, 4.781)):
        assert abs(t_quantile(p, df) - expected) < 0.002 * expected


def test_calibration_needs_history_and_is_bounded():
    from dbpilot_core.gate import calibrated_tolerance

    assert calibrated_tolerance([], 0.10) == (0.10, 0)
    assert calibrated_tolerance([(1.0, 1.4)] * 7, 0.10) == (0.10, 7)                 # too little history
    assert calibrated_tolerance([(1.0, 1.02)] * 10, 0.10) == (0.10, 10)             # never below the default
    tolerance, n = calibrated_tolerance([(0.5, 0.6)] * 9 + [(1.0, 1.0)], 0.10)      # twin off by 20% nine times in ten
    assert n == 10 and abs(tolerance - 0.20) < 1e-9
    assert calibrated_tolerance([(1.0, 3.0)] * 10, 0.10)[0] == 0.5                  # capped
    assert calibrated_tolerance([(None, 1.0), (0.0, 1.0)] * 10, 0.10) == (0.10, 0)  # unusable pairs ignored
