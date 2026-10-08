# Evidence

Raw outputs of pilot and diagnostic runs, with the protocol each one was run under. Every protocol or criteria file was
written (and, from P0.1e on, hashed) before its run. These are pilot observations and diagnostics on one laptop; none of
them is an evaluation result, and nothing here supports a claim about how well DBPilot works.

| Folder | What was run | Outcome |
|---|---|---|
| `p01/` | 5 production trials (S7, S10, no verification), an action-sensitivity screen, a capacity sweep | No action harmed a tenant at class-level p95. The row-security helper functions are parallel-unsafe, so the parallelism scenarios never changed a tenant query |
| `p01d/` | 3 null-action trials at load scale 5 | Not fit: 0 of 3 valid. A hot-row lock convoy on each tenant's warehouses collapses the run |
| `p01e/` | No-action characterization, OLTP at half rate plus analytical load of 10/20/30/40 queries/s | No level met the frozen criteria. CPU and New-Order p95 rise smoothly with analytical load |
| `p01f/` | A/A repeat of two levels after switching the power mode | The same load needed about 30% less CPU. The power-source log later showed `p01e` also ran on AC, so the cause is the power mode or machine state, not battery against AC |
| `p01g/` | Full repeat of `p01e` under AC and Best performance | No level qualified; the same profile gave 220-396% CPU across sessions. The charger dropped during two levels |
| `p01h/` | 35-minute synthetic CPU soak with speed canaries, database idle | Speed varied by 5-24% and followed CPU frequency; AC stayed on. The machine is not a controlled environment for quantitative runs |
| `e1/` | 30 no-action (A/A) twin runs judged by the unmodified gate | 28 valid. See below |

## E1 in brief (`e1/analysis.txt` is the full output of the frozen analysis)

- With identical arms, the gate as the engine uses it (up to three 55 s replays) returned INCONCLUSIVE in 7 of 9 cases and
  a false "harm shown" REJECT in 2 of 9. It never reached the correct decisive outcome. The aggregate gate on the same
  samples was correct in 8 of 9.
- In no grouping of 1, 3, 6, 10, 15 or 28 replays was every tenant shown safe at the 5% margin.
- Run-to-run variation of the p95 ratio is 1.2 to 13.5 times what the gate's interval assumes; identical arms differed by
  -18% to +26% in typical request latency (standard deviation 11%), and the tenants moved together.
- The capture includes the pooler's own `SET application_name` statements, which are replayed as transactions of the
  tenant that uses two connection classes.

## E1 re-judged with the pair-based gate (`e1/rejudge.txt`, produced by `e1/rejudge.py`; no new measurement)

After E1 the gate was changed to take its interval from the disagreement between replay pairs, to need both arm orders
before calling harm, and the capture to drop the pooler's statements. On the same 28 runs the false rejections are gone
(0 in 3,400 reordered sequences), and every verdict is INCONCLUSIVE at 3, 5, 9, 14 and 28 pairs: identical arms differ
by 5-12% from pair to pair on this laptop, so no tenant can be shown safe at a 5% margin in a practical number of pairs.
`e1/analyze.py` is the frozen script for the gate as it was at commit a8c6813 and does not run against the current gate.

Large raw files are gzip-compressed (`run_L*.json.gz`, `host.jsonl.gz`). Scripts in these folders were run from
`results/<folder>/`, which is not tracked; paths inside them refer to that location.
