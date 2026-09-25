"""
paired_run_analysis.py — Phase 0 Reproducibility Investigation

Runs B0/B1/B2 TWICE, snapshots raw files from each run into:
  results/repro_study/run_A/{b0,b1,b2}/
  results/repro_study/run_B/{b0,b1,b2}/

Then runs full diagnostic analysis on both runs without touching experiment code.

Usage:
    python scripts/paired_run_analysis.py            # run both + analyse
    python scripts/paired_run_analysis.py --analyse-only  # analyse existing repro_study/
"""

import asyncio
import csv
import json
import random
import shutil
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPRO_DIR = PROJECT_ROOT / "results" / "repro_study"
SLO_MS = 2.0


# ── Experiment runner (imports, doesn't copy) ────────────────────────────────

async def run_one_pass(label: str) -> dict[str, Path]:
    """
    Runs B0/B1/B2 via the existing run_final_experiments module,
    then snapshots the raw CSVs into repro_study/{label}/.
    Returns {mode: snapshot_dir}.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "backend" / "scripts"))
    import importlib
    import run_final_experiments as rfe
    importlib.reload(rfe)          # ensure clean state on second pass

    import json as _json
    with open(PROJECT_ROOT / "backend" / "scripts" / "interactive_targets.json") as f:
        rfe.IP_POOL = _json.load(f)

    random.seed(42)

    print(f"\n{'='*55}")
    print(f"  PAIRED RUN — {label}")
    print(f"{'='*55}")

    snapshot_dirs = {}
    for mode in ["b0", "b1", "b2"]:
        result = await rfe.run_experiment(mode)
        # Snapshot raw files before next mode overwrites
        src = PROJECT_ROOT / "results" / "final" / mode
        dst = REPRO_DIR / label / mode
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            shutil.copy2(f, dst / f.name)
        snapshot_dirs[mode] = dst
        print(f"  [{label}] {mode.upper()} → {dst}")

    return snapshot_dirs


# ── Raw-data analysis ─────────────────────────────────────────────────────────

def load_latencies(path: Path) -> list[float]:
    """Returns latency_ms values from an interactive.csv (header: latency_ms)."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return [float(row["latency_ms"]) for row in reader]


def percentile(data: list[float], p: float) -> float:
    arr = sorted(data)
    idx = int(len(arr) * p / 100)
    idx = min(idx, len(arr) - 1)
    return arr[idx]


def slo_violation_rate(data: list[float], slo_ms: float) -> tuple[int, float]:
    viols = sum(1 for v in data if v > slo_ms)
    return viols, viols / len(data) * 100 if data else 0.0


def tail_depth(data: list[float], p: float) -> list[float]:
    """Returns the actual values that constitute the tail above percentile p."""
    threshold = percentile(data, p)
    return sorted([v for v in data if v >= threshold])


def analyse_b2_trace(path: Path) -> dict:
    """Parses b2_trace.csv and returns controller decision stats."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "ts": float(row["timestamp"]),
                "viol_rate": float(row["violation_rate"]),
                "target": int(row["target_concurrency"]),
            })
    if not rows:
        return {"n_intervals": 0}

    targets = [r["target"] for r in rows]
    scale_events = sum(1 for i in range(1, len(targets)) if targets[i] != targets[i-1])
    return {
        "n_intervals": len(rows),
        "target_min": min(targets),
        "target_max": max(targets),
        "target_final": targets[-1],
        "scale_events": scale_events,
        "target_sequence": targets,
    }


def full_analysis(run_a_dirs: dict[str, Path], run_b_dirs: dict[str, Path]):
    print(f"\n{'='*65}")
    print("  DIAGNOSTIC ANALYSIS — ROOT CAUSE INVESTIGATION")
    print(f"{'='*65}")

    results = {}

    for mode in ["b0", "b1", "b2"]:
        a_lats = load_latencies(run_a_dirs[mode] / "interactive.csv")
        b_lats = load_latencies(run_b_dirs[mode] / "interactive.csv")

        a_p50 = percentile(a_lats, 50)
        a_p95 = percentile(a_lats, 95)
        a_p99 = percentile(a_lats, 99)
        b_p50 = percentile(b_lats, 50)
        b_p95 = percentile(b_lats, 95)
        b_p99 = percentile(b_lats, 99)

        a_viols, a_viol_pct = slo_violation_rate(a_lats, SLO_MS)
        b_viols, b_viol_pct = slo_violation_rate(b_lats, SLO_MS)

        # P99 tail: what are the actual top-1% values?
        a_tail = tail_depth(a_lats, 99)  # values at or above P99
        b_tail = tail_depth(b_lats, 99)

        print(f"\n{'─'*65}")
        print(f"  MODE: {mode.upper()}")
        print(f"{'─'*65}")
        print(f"  REQUEST COUNTS        Run A: {len(a_lats):>6}   Run B: {len(b_lats):>6}   Δ={len(a_lats)-len(b_lats):+}")
        print(f"")
        print(f"  LATENCY PERCENTILES (ms):")
        print(f"  {'Metric':<20} {'Run A':>10} {'Run B':>10} {'%Δ':>8}")
        print(f"  {'─'*50}")
        for label, va, vb in [("P50", a_p50, b_p50), ("P95", a_p95, b_p95), ("P99", a_p99, b_p99)]:
            denom = max(abs(va), 1e-9)
            pct = abs(va - vb) / denom * 100
            flag = " ← UNSTABLE" if pct > 10 else ""
            print(f"  {label:<20} {va:>10.3f} {vb:>10.3f} {pct:>7.1f}%{flag}")

        print(f"")
        print(f"  SLO VIOLATIONS (>{SLO_MS}ms):")
        denom = max(a_viol_pct, 1e-9)
        viol_pct_diff = abs(a_viol_pct - b_viol_pct) / denom * 100
        flag = " ← UNSTABLE" if viol_pct_diff > 10 else ""
        print(f"  {'Violations (count)':<20} {a_viols:>10}   {b_viols:>10}")
        print(f"  {'Violation rate (%)':<20} {a_viol_pct:>10.3f}   {b_viol_pct:>10.3f}   {viol_pct_diff:>6.1f}%{flag}")

        print(f"")
        print(f"  P99 TAIL ANALYSIS (requests at or above P99 threshold):")
        print(f"  Run A → {len(a_tail)} requests, range [{min(a_tail):.2f}, {max(a_tail):.2f}] ms")
        print(f"     values: {[round(v,2) for v in a_tail[:20]]}{'...' if len(a_tail)>20 else ''}")
        print(f"  Run B → {len(b_tail)} requests, range [{min(b_tail):.2f}, {max(b_tail):.2f}] ms")
        print(f"     values: {[round(v,2) for v in b_tail[:20]]}{'...' if len(b_tail)>20 else ''}")

        # Distribution shape — check if variance is concentrated in tail or spread
        a_arr = np.array(a_lats)
        b_arr = np.array(b_lats)
        print(f"")
        print(f"  DISTRIBUTION SHAPE:")
        print(f"  {'Metric':<22} {'Run A':>10} {'Run B':>10}")
        print(f"  {'Mean (ms)':<22} {np.mean(a_arr):>10.3f} {np.mean(b_arr):>10.3f}")
        print(f"  {'Std Dev (ms)':<22} {np.std(a_arr):>10.3f} {np.std(b_arr):>10.3f}")
        print(f"  {'Max (ms)':<22} {np.max(a_arr):>10.3f} {np.max(b_arr):>10.3f}")
        print(f"  {'% > 5ms':<22} {sum(1 for v in a_lats if v>5)/len(a_lats)*100:>10.2f} {sum(1 for v in b_lats if v>5)/len(b_lats)*100:>10.2f}")
        print(f"  {'% > 10ms':<22} {sum(1 for v in a_lats if v>10)/len(a_lats)*100:>10.2f} {sum(1 for v in b_lats if v>10)/len(b_lats)*100:>10.2f}")
        print(f"  {'% > 20ms':<22} {sum(1 for v in a_lats if v>20)/len(a_lats)*100:>10.2f} {sum(1 for v in b_lats if v>20)/len(b_lats)*100:>10.2f}")

        # B2-specific: controller trace comparison
        if mode == "b2":
            trace_a = analyse_b2_trace(run_a_dirs[mode] / "b2_trace.csv")
            trace_b = analyse_b2_trace(run_b_dirs[mode] / "b2_trace.csv")
            print(f"")
            print(f"  B2 CONTROLLER TRACE:")
            print(f"  {'Metric':<28} {'Run A':>10} {'Run B':>10}")
            print(f"  {'Scale events':<28} {trace_a['scale_events']:>10} {trace_b['scale_events']:>10}")
            print(f"  {'Target min':<28} {trace_a['target_min']:>10} {trace_b['target_min']:>10}")
            print(f"  {'Target max':<28} {trace_a['target_max']:>10} {trace_b['target_max']:>10}")
            print(f"  {'Target final':<28} {trace_a['target_final']:>10} {trace_b['target_final']:>10}")
            print(f"  {'Control intervals':<28} {trace_a['n_intervals']:>10} {trace_b['n_intervals']:>10}")
            print(f"  Run A target sequence: {trace_a.get('target_sequence', [])}")
            print(f"  Run B target sequence: {trace_b.get('target_sequence', [])}")

        results[mode] = {
            "a_count": len(a_lats), "b_count": len(b_lats),
            "a_p99": a_p99, "b_p99": b_p99,
            "a_viols": a_viols, "b_viols": b_viols,
            "a_viol_pct": a_viol_pct, "b_viol_pct": b_viol_pct,
        }

    # ── Summary diagnosis ──────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  DIAGNOSIS SUMMARY")
    print(f"{'='*65}")

    b0 = results["b0"]
    b1 = results["b1"]
    b2 = results["b2"]

    print(f"""
  [B0 P99] Run A={b0['a_p99']:.2f}ms  Run B={b0['b_p99']:.2f}ms
  Request counts: A={b0['a_count']}  B={b0['b_count']}
  P99 is the ~{int(b0['a_count']*0.01)}-th worst request per run.
  A difference of >{abs(b0['a_p99']-b0['b_p99']):.1f}ms in P99 = moved by ~{max(1,int(b0['a_count']*0.01))} outlier events.
  See tail analysis above to determine if this is 1-spike or structural.

  [B1 SLO VIOLATIONS] Run A={b1['a_viols']} ({b1['a_viol_pct']:.2f}%)  Run B={b1['b_viols']} ({b1['b_viol_pct']:.2f}%)
  Absolute count difference: {abs(b1['a_viols']-b1['b_viols'])} requests.
  At 50 QPS × 30s = ~1500 requests, each violation = 0.067% of viol_rate.
  So {abs(b1['a_viols']-b1['b_viols'])} extra violations × 0.067% = ~{abs(b1['a_viols']-b1['b_viols'])*0.067:.2f}% rate shift.

  [B2 ANA TPS] See controller trace above for whether decisions differed.
  Analytical completions are integer counts — see trace for scaling path.
""")

    # Write machine-readable diagnosis
    diag_path = REPRO_DIR / "diagnosis.json"
    with open(diag_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Machine-readable diagnosis written to: {diag_path}")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    analyse_only = "--analyse-only" in sys.argv

    if not analyse_only:
        run_a_dirs = await run_one_pass("run_A")
        print("\n  [Pause 5s between runs to let Postgres settle...]")
        await asyncio.sleep(5)
        run_b_dirs = await run_one_pass("run_B")
    else:
        # Load from existing snapshots
        run_a_dirs = {m: REPRO_DIR / "run_A" / m for m in ["b0", "b1", "b2"]}
        run_b_dirs = {m: REPRO_DIR / "run_B" / m for m in ["b0", "b1", "b2"]}
        for d in list(run_a_dirs.values()) + list(run_b_dirs.values()):
            if not d.exists():
                print(f"[ERROR] Missing snapshot dir: {d}")
                print("Run without --analyse-only first.")
                sys.exit(1)

    full_analysis(run_a_dirs, run_b_dirs)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
