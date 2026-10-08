"""The tenant-aware safety gate: turns measurements from control replays and
treatment replays into APPROVE, REJECT or INCONCLUSIVE.

The rule it encodes:

  * the tenant the action is for must be shown to benefit;
  * every other tenant must be shown not to be harmed.

"Shown" means the whole confidence interval is on the right side of the
threshold. An interval that straddles a threshold is INCONCLUSIVE, never a pass:
absence of evidence of harm is not evidence of safety (this is the difference
between a non-inferiority test and "the difference was not significant").

The unit of evidence is the replay pair: one control replay and one treatment
replay of the same captured window. The two arms of a pair run one after the
other, so they see different moments of the machine, and with identical arms the
metric still differs from pair to pair by far more than the scatter of requests
inside one replay suggests. Only several pairs show how large that difference
is. One pair therefore decides nothing, and harm must be seen with each arm
having run first before a change is rejected for it.
"""
import math
from dataclasses import asdict, dataclass, field
from typing import Literal

import numpy as np

Key = tuple[str, str]  # (tenant role, query class)
Sample = tuple[float, float]  # (seconds since replay start, latency in ms)
Decision = Literal["APPROVE", "REJECT", "INCONCLUSIVE"]

REPLAY_SPAN_S = 1_000_000.0  # pool() gives each replay its own range of times this wide


@dataclass(frozen=True)
class GatePolicy:
    metric: Literal["p95", "p99", "mean"] = "p95"
    min_benefit: float = 0.10       # target must improve by at least this fraction
    max_regression: float = 0.05    # any other tenant may get at most this much worse
    confidence: float = 0.95        # family-wise, across all tenant comparisons
    warmup_s: float = 0.0           # leading part of the run that is not measured
    min_samples: int = 30           # per arm, per key, below which a key cannot be judged
    min_pair_samples: int = 10      # per arm, per key, below which one replay pair is not used
    max_wal_ratio: float = 1.25     # write amplification: WAL per replay, treatment / control
    storage_budget_bytes: int = 2 * 1024**3
    stricter_when_costly_to_undo: float = 0.5  # shrinks max_regression for hard-to-reverse actions
    contract_tolerance: float = 0.10


@dataclass
class Effect:
    """Treatment relative to control for one (tenant, class). ratio < 1 means faster.

    `control` and `treatment` are the metric over all pooled samples. `ratio` is the
    geometric mean of the per-pair ratios and `lo`..`hi` its interval; with a single
    pair there is no interval."""

    n_control: int
    n_treatment: int
    control: float | None = None
    treatment: float | None = None
    ratio: float | None = None
    lo: float | None = None
    hi: float | None = None
    status: str = "NO_DATA"
    pair_ratios: dict[int, float] = field(default_factory=dict)  # replay number -> treatment / control


@dataclass
class Verdict:
    decision: Decision
    mode: str
    reasons: list[str]
    effects: dict[str, dict] = field(default_factory=dict)
    # What the canary must enforce: the largest production ratio still consistent with this verdict.
    contract: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def pool(into: dict[Key, list[Sample]], samples: dict[Key, list[Sample]], block: int, warmup_s: float) -> None:
    """Adds one replay's samples to an accumulated set, for a verdict over several replays.

    The times of replay number `block` are shifted into a range of their own, which
    is how compare() tells the replay pairs apart. The warm-up of each replay is
    dropped here, before the shift hides it.
    """
    offset = block * REPLAY_SPAN_S
    for key, values in samples.items():
        into.setdefault(key, []).extend((t + offset, latency) for t, latency in values if t >= warmup_s)


def calibrated_tolerance(pairs: list[tuple[float, float]], default: float, *, min_pairs: int = 8,
                         cap: float = 0.5) -> tuple[float, int]:
    """How far production may drift from the twin's prediction before the canary rolls back.

    `pairs` are (twin ratio, production ratio) for past changes of the same kind
    that reached production. With enough history the tolerance is the 90th
    percentile of the twin's relative error, never below the default and never
    above `cap`; with too little, the default. Returns (tolerance, pairs used).
    """
    errors = [abs(prod / twin - 1) for twin, prod in pairs if twin and prod and twin > 0]
    if len(errors) < min_pairs:
        return default, len(errors)
    return float(min(cap, max(default, np.percentile(errors, 90)))), len(errors)


def _metric(values: np.ndarray, metric: str) -> float:
    if metric == "mean":
        return float(values.mean())
    return float(np.percentile(values, 95 if metric == "p95" else 99))


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b), by its continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _incomplete_beta(b, a, 1 - x)  # the fraction converges quickly only on this side
    tiny = 1e-300
    c, d = 1.0, 1.0 / max(abs(1 - (a + b) * x / (a + 1)), tiny)
    h = d
    for m in range(1, 500):
        for numerator in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
                          -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1 + numerator * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1 + numerator / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return front * h / a


def t_quantile(p: float, df: int) -> float:
    """Quantile of Student's t distribution for 0.5 < p < 1, by bisection on its distribution function."""
    def cdf(t: float) -> float:
        return 1 - _incomplete_beta(df / 2, 0.5, df / (df + t * t)) / 2

    lo, hi = 0.0, 1.0
    while cdf(hi) < p:
        lo, hi = hi, hi * 2
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) < p else (lo, mid)
    return (lo + hi) / 2


def _by_replay(samples: list[Sample], policy: GatePolicy) -> dict[int, np.ndarray]:
    grouped: dict[int, list[float]] = {}
    for t, latency in samples:
        replay = int(t // REPLAY_SPAN_S)
        if t - replay * REPLAY_SPAN_S >= policy.warmup_s:
            grouped.setdefault(replay, []).append(latency)
    return {r: np.asarray(v) for r, v in grouped.items()}


def compare(control: list[Sample], treatment: list[Sample], policy: GatePolicy, alpha: float) -> Effect:
    """Ratio of the metric, treatment over control, with an interval from the replay pairs.

    For each replay pair the metric of the treatment replay is divided by that of
    the control replay. The interval is Student's t interval on the logarithms of
    those ratios, so its width comes from how much the pairs disagree: that covers
    the scatter of requests inside a replay and whatever differed between the two
    arms of a pair. With one pair that disagreement is unknown and there is no interval.
    """
    cr, tr = _by_replay(control, policy), _by_replay(treatment, policy)
    n_c, n_t = sum(len(v) for v in cr.values()), sum(len(v) for v in tr.values())
    effect = Effect(n_control=n_c, n_treatment=n_t)
    if n_c == 0 and n_t == 0:
        return effect
    if n_c < policy.min_samples or n_t < policy.min_samples:
        effect.status = "TOO_FEW_SAMPLES"
        return effect

    effect.control = _metric(np.concatenate(list(cr.values())), policy.metric)
    effect.treatment = _metric(np.concatenate(list(tr.values())), policy.metric)
    for replay in sorted(set(cr) & set(tr)):
        if min(len(cr[replay]), len(tr[replay])) >= policy.min_pair_samples:
            c, t = _metric(cr[replay], policy.metric), _metric(tr[replay], policy.metric)
            if c > 0 and t > 0:
                effect.pair_ratios[replay] = t / c
    if not effect.pair_ratios:
        effect.status = "TOO_FEW_SAMPLES"
        return effect
    logs = np.log(list(effect.pair_ratios.values()))
    effect.ratio = float(np.exp(logs.mean()))
    if len(logs) == 1:
        effect.status = "ONE_PAIR"
        return effect
    half = t_quantile(1 - alpha / 2, len(logs) - 1) * float(logs.std(ddof=1)) / math.sqrt(len(logs))
    # Two or three pairs that disagree give a half-width in the hundreds; keep exp() finite.
    effect.lo, effect.hi = math.exp(max(logs.mean() - half, -700.0)), math.exp(min(logs.mean() + half, 700.0))
    effect.status = "MEASURED"
    return effect


def _in_both_orders(replays: list[int], treatment_first: dict[int, bool] | None) -> bool:
    """Whether `replays` include one in which the treatment arm ran first and one in which it ran second."""
    return bool(treatment_first) and len({treatment_first[r] for r in replays if r in treatment_first}) == 2


def decide(
    control: dict[Key, list[Sample]],
    treatment: dict[Key, list[Sample]],
    target_tenant: str | None,
    policy: GatePolicy = GatePolicy(),
    *,
    mode: Literal["per_tenant", "aggregate"] = "per_tenant",
    cheap_to_undo: bool = True,
    wal_ratio: float | None = None,
    storage_delta_bytes: int = 0,
    slo_ms: dict[Key, tuple[int, float]] | None = None,
    treatment_first: dict[int, bool] | None = None,
) -> Verdict:
    """`slo_ms` maps (tenant, class) to (percentile, threshold in ms). `treatment_first`
    maps a replay number (the `block` given to pool()) to whether the treatment arm ran
    before the control arm in that replay; without it no harm can be confirmed."""
    reasons: list[str] = []

    # Budgets are absolute: no latency win buys them back.
    if storage_delta_bytes > policy.storage_budget_bytes:
        return Verdict("REJECT", mode, [f"storage grows by {storage_delta_bytes} bytes, over budget"])
    if wal_ratio is not None and wal_ratio > policy.max_wal_ratio:
        return Verdict("REJECT", mode, [f"write volume grows {wal_ratio:.2f}x, over the {policy.max_wal_ratio}x budget"])

    if mode == "aggregate":
        return _decide_aggregate(control, treatment, policy)

    keys = sorted(set(control) | set(treatment))
    # Bonferroni: with m comparisons each at alpha/m, the chance of any false "safe" stays below alpha.
    alpha = (1 - policy.confidence) / max(1, len(keys))
    margin = policy.max_regression * (1.0 if cheap_to_undo else policy.stricter_when_costly_to_undo)
    harm_limit, benefit_limit = 1 + margin, 1 - policy.min_benefit

    effects: dict[Key, Effect] = {k: compare(control.get(k, []), treatment.get(k, []), policy, alpha) for k in keys}
    harmed, uncertain, benefit_shown, benefit_ruled_out = [], [], [], []

    for key, e in effects.items():
        name = f"{key[0]}/{key[1]}"
        is_target = target_tenant is None or key[0] == target_tenant
        if e.status == "NO_DATA":
            continue
        if e.status == "TOO_FEW_SAMPLES":
            e.status = "UNCERTAIN"
            uncertain.append(f"{name}: too few samples to judge")
            continue
        if e.status == "ONE_PAIR":
            e.status = "UNCERTAIN"
            uncertain.append(f"{name}: {policy.metric} {e.ratio:.2f}x in one replay pair; a single pair cannot show "
                             "how much two identical arms differ")
            continue
        if e.lo > harm_limit:
            # The interval already rests on several pairs. Harm is final, so it must also have been
            # seen with each arm running first: an effect of the order would show in one order only.
            if _in_both_orders([r for r, ratio in e.pair_ratios.items() if ratio > harm_limit], treatment_first):
                e.status = "HARMED"
                harmed.append(f"{name}: {policy.metric} {e.ratio:.2f}x (interval {e.lo:.2f}-{e.hi:.2f})")
            else:
                e.status = "UNCERTAIN"
                uncertain.append(f"{name}: {policy.metric} {e.ratio:.2f}x (interval {e.lo:.2f}-{e.hi:.2f}), "
                                 "not yet seen with the arm order reversed")
        elif e.hi <= harm_limit:
            e.status = "SAFE"
        else:
            e.status = "UNCERTAIN"
            uncertain.append(f"{name}: {policy.metric} {e.ratio:.2f}x, interval {e.lo:.2f}-{e.hi:.2f} "
                             f"does not rule out a regression above {margin:.0%}")
        if is_target:
            if e.hi < benefit_limit:
                e.status = "BENEFITS"
                benefit_shown.append(name)
            elif e.lo >= benefit_limit:
                benefit_ruled_out.append(name)

        if slo_ms and key in slo_ms and e.status != "HARMED":
            pct, threshold = slo_ms[key]
            c_val = float(np.percentile([s[1] for s in control[key] if s[0] % REPLAY_SPAN_S >= policy.warmup_s], pct))
            t_val = float(np.percentile([s[1] for s in treatment[key] if s[0] % REPLAY_SPAN_S >= policy.warmup_s], pct))
            cr, tr = _by_replay(control[key], policy), _by_replay(treatment[key], policy)
            broken_in = [r for r in set(cr) & set(tr)
                         if np.percentile(cr[r], pct) <= threshold < np.percentile(tr[r], pct)]
            if c_val <= threshold < t_val and _in_both_orders(broken_in, treatment_first):
                e.status = "HARMED"
                harmed.append(f"{name}: p{pct} {t_val:.1f} ms breaks the {threshold:g} ms SLO that control met")

    measured_targets = [k for k, e in effects.items()
                        if (target_tenant is None or k[0] == target_tenant) and e.status != "NO_DATA"]
    if harmed:
        decision, reasons = "REJECT", ["harm shown: " + h for h in harmed]
    elif uncertain:
        decision, reasons = "INCONCLUSIVE", uncertain
    elif benefit_shown:
        decision, reasons = "APPROVE", [f"benefit shown for {', '.join(benefit_shown)}; no tenant harmed"]
    elif measured_targets and len(benefit_ruled_out) == len(measured_targets):
        decision, reasons = "REJECT", [f"no tenant harmed, but no benefit of at least {policy.min_benefit:.0%}"]
    else:
        decision, reasons = "INCONCLUSIVE", ["a benefit could be neither shown nor ruled out"]

    contract = {}
    for key, e in effects.items():
        if e.hi is not None:
            contract[f"{key[0]}/{key[1]}"] = round(min(max(e.hi, 1.0) + policy.contract_tolerance,
                                                        harm_limit + policy.contract_tolerance), 3)
    return Verdict(decision, mode, reasons, {f"{k[0]}/{k[1]}": asdict(e) for k, e in effects.items()}, contract)


def _decide_aggregate(control, treatment, policy: GatePolicy) -> Verdict:
    """The comparison point: judge only the workload as a whole, as single-tenant
    tuners do. Uses mean latency over all requests, i.e. total time spent."""
    pooled_c = [s for v in control.values() for s in v]
    pooled_t = [s for v in treatment.values() for s in v]
    mean_policy = GatePolicy(**{**asdict(policy), "metric": "mean"})
    e = compare(pooled_c, pooled_t, mean_policy, 1 - policy.confidence)
    effects = {"*/*": asdict(e)}
    if e.status == "ONE_PAIR":
        return Verdict("INCONCLUSIVE", "aggregate", ["a single replay pair cannot show how much two identical arms differ"],
                       effects)
    if e.status != "MEASURED":
        return Verdict("INCONCLUSIVE", "aggregate", ["too few samples"], effects)
    if e.hi < 1 - policy.min_benefit:
        return Verdict("APPROVE", "aggregate", [f"overall mean latency {e.ratio:.2f}x"], effects,
                       {"*/*": round(1.0 + policy.contract_tolerance, 3)})
    if e.lo >= 1 - policy.min_benefit:
        return Verdict("REJECT", "aggregate", [f"overall mean latency {e.ratio:.2f}x: no sufficient benefit"], effects)
    return Verdict("INCONCLUSIVE", "aggregate", ["overall benefit neither shown nor ruled out"], effects)
