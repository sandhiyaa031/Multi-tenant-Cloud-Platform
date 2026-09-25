import csv, numpy as np, json
from pathlib import Path

ROOT = Path(r'd:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform/results/repro_study')
SLO = 2.0
OUT = ROOT / "raw_analysis.txt"

lines = []
all_data = {}

for mode in ['b0', 'b1', 'b2']:
    lines.append(f"\n{'='*60}")
    lines.append(f"MODE: {mode.upper()}")
    lines.append(f"{'='*60}")
    all_data[mode] = {}

    for run in ['run_A', 'run_B']:
        lats = []
        with open(ROOT / run / mode / 'interactive.csv') as f:
            for row in csv.DictReader(f):
                lats.append(float(row['latency_ms']))
        arr = np.array(sorted(lats))
        N = len(arr)
        p50 = float(np.percentile(arr, 50))
        p95 = float(np.percentile(arr, 95))
        p99 = float(np.percentile(arr, 99))
        mx  = float(arr[-1])
        p99_idx = int(N * 0.99)
        tail = arr[p99_idx:].tolist()
        viols = arr[arr > SLO].tolist()

        all_data[mode][run] = {
            'N': N, 'p50': round(p50,3), 'p95': round(p95,3),
            'p99': round(p99,3), 'max': round(mx,3),
            'viol_count': len(viols), 'viol_rate_pct': round(len(viols)/N*100, 4),
            'tail': [round(v,3) for v in tail],
            'gt5ms': int(sum(arr>5)), 'gt10ms': int(sum(arr>10)),
            'gt20ms': int(sum(arr>20)), 'gt50ms': int(sum(arr>50)),
            'mean': round(float(np.mean(arr)),3), 'std': round(float(np.std(arr)),3),
        }
        d = all_data[mode][run]

        lines.append(f"\n  {run}: N={N}")
        lines.append(f"  P50={d['p50']}ms  P95={d['p95']}ms  P99={d['p99']}ms  Max={d['max']}ms")
        lines.append(f"  Mean={d['mean']}ms  Std={d['std']}ms")
        lines.append(f"  Violations: {d['viol_count']} / {N} = {d['viol_rate_pct']}%")
        lines.append(f"  Requests >5ms:  {d['gt5ms']}")
        lines.append(f"  Requests >10ms: {d['gt10ms']}")
        lines.append(f"  Requests >20ms: {d['gt20ms']}")
        lines.append(f"  Requests >50ms: {d['gt50ms']}")
        lines.append(f"  P99-tail values ({len(tail)} items): {d['tail']}")

    # Cross-run comparison
    a = all_data[mode]['run_A']
    b = all_data[mode]['run_B']
    lines.append(f"\n  CROSS-RUN COMPARISON:")
    lines.append(f"  N:          A={a['N']}  B={b['N']}  delta={a['N']-b['N']}")
    lines.append(f"  P99 diff:   {abs(a['p99']-b['p99']):.3f}ms ({abs(a['p99']-b['p99'])/max(a['p99'],1e-9)*100:.1f}%)")
    lines.append(f"  Viol count: A={a['viol_count']}  B={b['viol_count']}  delta={a['viol_count']-b['viol_count']}")
    lines.append(f"  Viol rate:  A={a['viol_rate_pct']}%  B={b['viol_rate_pct']}%")
    # P99 in absolute request terms
    a_p99_req = int(a['N'] * 0.01)
    b_p99_req = int(b['N'] * 0.01)
    lines.append(f"  P99 = worst {a_p99_req} reqs (A) / {b_p99_req} reqs (B) out of {a['N']}/{b['N']}")

# B2 trace comparison
lines.append(f"\n{'='*60}")
lines.append("B2 CONTROLLER TRACE COMPARISON")
lines.append(f"{'='*60}")
for run in ['run_A', 'run_B']:
    rows = []
    with open(ROOT / run / 'b2' / 'b2_trace.csv') as f:
        for row in csv.DictReader(f):
            rows.append({'ts': float(row['timestamp']),
                         'vrate': float(row['violation_rate']),
                         'target': int(row['target_concurrency'])})
    targets = [r['target'] for r in rows]
    vrates  = [r['vrate']  for r in rows]
    scale_events = sum(1 for i in range(1, len(targets)) if targets[i] != targets[i-1])
    lines.append(f"\n  {run}: {len(rows)} intervals")
    lines.append(f"  target sequence: {targets}")
    lines.append(f"  viol_rate seq:   {[round(v,2) for v in vrates]}")
    lines.append(f"  scale events: {scale_events}")
    lines.append(f"  target min={min(targets)} max={max(targets)} final={targets[-1]}")

# Write and print
text = '\n'.join(lines)
OUT.write_text(text)
print(text)
print(f"\n\nWritten to {OUT}")
json.dump(all_data, open(ROOT/'raw_analysis.json','w'), indent=2)
