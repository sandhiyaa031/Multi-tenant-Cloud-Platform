"""The tenant-aware safety gate: turns measurements from a control run and a
treatment run into APPROVE, REJECT or INCONCLUSIVE.

The rule it encodes:

  * the tenant the action is for must be shown to benefit;
  * every other tenant must be shown not to be harmed.

"Shown" means the whole confidence interval is on the right side of the
threshold. An interval that straddles a threshold is INCONCLUSIVE, never a pass:
absence of evidence of harm is not evidence of safety (this is the difference
between a non-inferiority test and "the difference was not significant").
"""
from dataclasses import asdict, dataclass, field
from typing import Literal

import numpy as np

Key = tuple[str, str]  # (tenant role, query class)
Sample = tuple[float, float]  # (seconds since replay start, latency in ms)
Decision = Literal["APPROVE", "REJECT", "INCONCLUSIVE"]


@dataclass(frozen=True)
class GatePolicy:
    metric: Literal["p95", "p99", "mean"] = "p95"
    min_benefit: float = 0.10       # target must improve by at least this fraction
    max_regression: float = 0.05    # any other tenant may get at most this much worse
    confidence: float = 0.95        # family-wise, across all tenant comparisons
    bucket_s: float = 5.0           # block length for the bootstrap
    warmup_s: float = 0.0           # leading part of the run that is not measured
    min_samples: int = 30           # per arm, per key, below which a key cannot be judged
    n_boot: int = 2000
    max_wal_ratio: float = 1.25     # write amplification: WAL per replay, treatment / control
    storage_budget_bytes: int = 2 * 1024**3
    stricter_when_costly_to_undo: float = 0.5  # shrinks max_regression for hard-to-reverse actions
    contract_tolerance: float = 0.10


@dataclass
class Effect:
    """Treatment relative to control for one (tenant, class). ratio < 1 means faster."""

    n_control: int
    n_treatment: int
    control: float | None = None
    treatment: float | None = None
    ratio: float | None = None
    lo: float | None = None
    hi: float | None = None
    status: str = "NO_DATA"


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


def _metric(values: np.ndarray, metric: str) -> float:
    if metric == "mean":
        return float(values.mean())
    return float(np.percentile(values, 95 if metric == "p95" else 99))


def _buckets(samples: list[Sample], policy: GatePolicy) -> dict[int, np.ndarray]:
    grouped: dict[int, list[float]] = {}
    for t, latency in samples:
        if t >= policy.warmup_s:
            grouped.setdefault(int(t // policy.bucket_s), []).append(latency)
    return {b: np.asarray(v) for b, v in grouped.items()}


def compare(control: list[Sample], treatment: list[Sample], policy: GatePolicy, alpha: float,
            rng: np.random.Generator) -> Effect:
    """Ratio of the metric, treatment over control, with a block-bootstrap interval.

    Latencies close together in time are correlated (a slow moment slows many
    requests), so resampling individual requests would understate the
    uncertainty. Instead whole time buckets are resampled, and the same buckets
    are drawn for both arms because both replays follow the same schedule.
    """
    cb, tb = _buckets(control, policy), _buckets(treatment, policy)
    n_c, n_t = sum(len(v) for v in cb.values()), sum(len(v) for v in tb.values())
    effect = Effect(n_control=n_c, n_treatment=n_t)
    if n_c == 0 and n_t == 0:
        return effect
    common = sorted(set(cb) & set(tb))
    if n_c < policy.min_samples or n_t < policy.min_samples or len(common) < 4:
        effect.status = "TOO_FEW_SAMPLES"
        return effect

    c_all = np.concatenate([cb[b] for b in common])
    t_all = np.concatenate([tb[b] for b in common])
    effect.control, effect.treatment = _metric(c_all, policy.metric), _metric(t_all, policy.metric)
    effect.ratio = effect.treatment / effect.control if effect.control > 0 else None
    if effect.ratio is None:
        effect.status = "TOO_FEW_SAMPLES"
        return effect

    ratios = np.empty(policy.n_boot)
    for i in range(policy.n_boot):
        draw = rng.choice(len(common), size=len(common), replace=True)
        c = _metric(np.concatenate([cb[common[j]] for j in draw]), policy.metric)
        t = _metric(np.concatenate([tb[common[j]] for j in draw]), policy.metric)
        ratios[i] = t / c if c > 0 else np.nan
    effect.lo, effect.hi = (float(x) for x in np.nanpercentile(ratios, [100 * alpha / 2, 100 * (1 - alpha / 2)]))
    effect.status = "MEASURED"
    return effect


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
    seed: int = 0,
) -> Verdict:
    """`slo_ms` maps (tenant, class) to (percentile, threshold in ms)."""
    rng = np.random.default_rng(seed)
    reasons: list[str] = []

    # Budgets are absolute: no latency win buys them back.
    if storage_delta_bytes > policy.storage_budget_bytes:
        return Verdict("REJECT", mode, [f"storage grows by {storage_delta_bytes} bytes, over budget"])
    if wal_ratio is not None and wal_ratio > policy.max_wal_ratio:
        return Verdict("REJECT", mode, [f"write volume grows {wal_ratio:.2f}x, over the {policy.max_wal_ratio}x budget"])

    if mode == "aggregate":
        return _decide_aggregate(control, treatment, policy, rng)

    keys = sorted(set(control) | set(treatment))
    # Bonferroni: with m comparisons each at alpha/m, the chance of any false "safe" stays below alpha.
    alpha = (1 - policy.confidence) / max(1, len(keys))
    margin = policy.max_regression * (1.0 if cheap_to_undo else policy.stricter_when_costly_to_undo)
    harm_limit, benefit_limit = 1 + margin, 1 - policy.min_benefit

    effects: dict[Key, Effect] = {k: compare(control.get(k, []), treatment.get(k, []), policy, alpha, rng) for k in keys}
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
        if e.lo > harm_limit:
            e.status = "HARMED"
            harmed.append(f"{name}: {policy.metric} {e.ratio:.2f}x (interval {e.lo:.2f}-{e.hi:.2f})")
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
            c_val = float(np.percentile([s[1] for s in control[key] if s[0] >= policy.warmup_s], pct))
            t_val = float(np.percentile([s[1] for s in treatment[key] if s[0] >= policy.warmup_s], pct))
            if c_val <= threshold < t_val:
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


def _decide_aggregate(control, treatment, policy: GatePolicy, rng) -> Verdict:
    """The comparison point: judge only the workload as a whole, as single-tenant
    tuners do. Uses mean latency over all requests, i.e. total time spent."""
    pooled_c = [s for v in control.values() for s in v]
    pooled_t = [s for v in treatment.values() for s in v]
    mean_policy = GatePolicy(**{**asdict(policy), "metric": "mean"})
    e = compare(pooled_c, pooled_t, mean_policy, 1 - policy.confidence, rng)
    effects = {"*/*": asdict(e)}
    if e.status != "MEASURED":
        return Verdict("INCONCLUSIVE", "aggregate", ["too few samples"], effects)
    if e.hi < 1 - policy.min_benefit:
        return Verdict("APPROVE", "aggregate", [f"overall mean latency {e.ratio:.2f}x"], effects,
                       {"*/*": round(1.0 + policy.contract_tolerance, 3)})
    if e.lo >= 1 - policy.min_benefit:
        return Verdict("REJECT", "aggregate", [f"overall mean latency {e.ratio:.2f}x: no sufficient benefit"], effects)
    return Verdict("INCONCLUSIVE", "aggregate", ["overall benefit neither shown nor ruled out"], effects)
