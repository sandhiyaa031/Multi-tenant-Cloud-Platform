"""Turns trial records into the tables and figures of the evaluation.

    python -m evaluation.report /results/matrix.jsonl --out /results/report

Everything written here is computed from the trial file. A cell with no trials
is shown as empty, never filled in.
"""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HARM = 1.10
STATES = ["APPLIED", "ROLLED_BACK", "REJECTED", "INCONCLUSIVE", "FAILED"]
STATE_COLOUR = {"APPLIED": "#199e70", "ROLLED_BACK": "#c98500", "REJECTED": "#d95926",
                "INCONCLUSIVE": "#8a8f98", "FAILED": "#3d4350"}
CLASS_COLOUR = {"OLTP": "#3987e5", "OLAP": "#d95926"}
INK, GRID = "#1f2430", "#d9dce3"


def load(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def fmt(value, digits=2, suffix=""):
    return "" if value is None else f"{value:.{digits}f}{suffix}"


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def direction(ratio: float) -> str:
    return "slower" if ratio > 1.05 else "faster" if ratio < 0.95 else "neutral"


def table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=INK, labelsize=8)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def build(rows: list[dict], out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    trials = [r for r in rows if "final_state" in r]
    errors = [r for r in rows if "error" in r]
    scenarios = list(dict.fromkeys(r["scenario"] for r in trials))
    configs = list(dict.fromkeys(r["config"] for r in trials))
    by = defaultdict(list)
    for r in trials:
        by[(r["scenario"], r["config"])].append(r)

    md = ["# DBPilot evaluation report\n",
          f"Computed from {len(trials)} completed trials"
          + (f" ({len(errors)} further trials failed to run and are listed at the end)" if errors else "") + ".\n",
          f"A tenant counts as harmed when its client-side p95 while the change was live exceeded {HARM:.2f}x of the "
          "mean of the periods before and after it. The target tenant of an action is not counted as harmed by it.\n"]

    # ── 1. Outcomes ──────────────────────────────────────────────────────────
    md.append("## 1. Outcomes by scenario and configuration\n")
    body = []
    for s in scenarios:
        for c in configs:
            group = by.get((s, c), [])
            if not group:
                continue
            states = Counter(r["final_state"] for r in group)
            reached = [r for r in group if r.get("reached_production")]
            harmful = [r for r in reached if r.get("tenants_harmed")]
            body.append([s, c, len(group), ", ".join(f"{k} {v}" for k, v in states.most_common()),
                         len(reached), len(harmful),
                         fmt(median([r.get("exposure_s") for r in harmful]), 0, " s"),
                         fmt(median([r.get("seconds_to_decision") for r in group]), 0, " s")])
    md.append(table(["Scenario", "Configuration", "Trials", "Final states", "Reached production",
                     "Harmed a tenant", "Median exposure when harmful", "Median time to decision"], body))

    # ── 2. Ground truth ──────────────────────────────────────────────────────
    md.append("## 2. What each action does in production (trials without verification)\n")
    md.append("Client-side p95 ratio per tenant and class; median over trials, with the range.\n")
    truth: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in trials:
        if r["config"] == "C2_no_verification" and r.get("reached_production"):
            for key, ratio in (r.get("client_ratios") or {}).items():
                truth[r["scenario"]][key].append(ratio)
    body = []
    for s in scenarios:
        for key in sorted(truth.get(s, {})):
            v = truth[s][key]
            body.append([s, key, len(v), fmt(median(v)), f"{min(v):.2f}-{max(v):.2f}",
                         "yes" if median(v) > HARM else ""])
    md.append(table(["Scenario", "Tenant / class", "Trials", "Median ratio", "Range", f"Median above {HARM:.2f}"], body)
              if body else "_No trials without verification reached production._\n")

    # ── 3. Per-tenant gate against aggregate gate ────────────────────────────
    md.append("## 3. Per-tenant gate and aggregate gate on the same twin measurements\n")
    md.append("Each twin-verified trial is judged by both gates on identical samples. The proposal follows the gate "
              "of its configuration; the other verdict is recorded for comparison.\n")
    body = []
    for s in scenarios:
        per, agg = Counter(), Counter()
        for r in trials:
            if r["scenario"] != s or not r.get("twin_decision"):
                continue
            mine, other = r["twin_decision"], r.get("twin_shadow_decision")
            if r["config"] == "C4_twin_per_tenant":
                per[mine] += 1
                if other:
                    agg[other] += 1
            elif r["config"] == "C4_twin_aggregate":
                agg[mine] += 1
                if other:
                    per[other] += 1
        if per or agg:
            harmed_keys = [k for k, v in truth.get(s, {}).items() if median(v) > HARM]
            body.append([s, ", ".join(f"{k} {v}" for k, v in per.most_common()),
                         ", ".join(f"{k} {v}" for k, v in agg.most_common()),
                         ", ".join(sorted(harmed_keys)) if s in truth else "not measured"])
    md.append(table(["Scenario", "Per-tenant gate", "Aggregate gate",
                     "Tenants slowed in production (section 2, includes target)"], body)
              if body else "_No twin-verified trials._\n")

    # ── 4. Twin accuracy ─────────────────────────────────────────────────────
    md.append("## 4. Twin prediction against production\n")
    twin: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in trials:
        for key, ratio in (r.get("twin_ratios") or {}).items():
            if ratio is not None:
                twin[r["scenario"]][key].append(ratio)
    pairs = [(s, k, median(twin[s][k]), median(truth[s][k]))
             for s in scenarios for k in sorted(twin.get(s, {})) if k in truth.get(s, {})]
    if pairs:
        agree = sum(direction(t) == direction(p) for _, _, t, p in pairs)
        rho = spearman([t for *_, t, _ in pairs], [p for *_, p in pairs])
        md.append(f"{len(pairs)} (scenario, tenant, class) pairs have both a twin prediction and a production "
                  f"measurement. Direction (faster / neutral / slower, at 5%) agrees in {agree} of {len(pairs)}. "
                  f"Spearman rank correlation: {fmt(rho)}.\n")
        md.append(table(["Scenario", "Tenant / class", "Twin (median)", "Production (median)", "Direction agrees"],
                        [[s, k, fmt(t), fmt(p), "yes" if direction(t) == direction(p) else "no"] for s, k, t, p in pairs]))
    else:
        md.append("_No pair has both a twin prediction and a production measurement._\n")

    # ── 5. Verification cost ─────────────────────────────────────────────────
    md.append("## 5. Verification cost\n")
    body = []
    for c in configs:
        group = [r for r in trials if r["config"] == c]
        seconds = [r["seconds_to_decision"] for r in group if r.get("seconds_to_decision") is not None]
        looks = [r["twin_looks"] for r in group if r.get("twin_looks")]
        body.append([c, len(group), fmt(median(seconds), 0, " s"), fmt(max(seconds), 0, " s") if seconds else "",
                     fmt(median(looks), 1) if looks else "", sum(r.get("twin_replay_errors") or 0 for r in group),
                     sum(r.get("twin_transactions") or 0 for r in group)])
    md.append(table(["Configuration", "Trials", "Median time to decision", "Longest", "Median twin replays",
                     "Replay errors", "Transactions replayed"], body))

    # ── Figures ──────────────────────────────────────────────────────────────
    figures = []
    if trials:
        fig, axes = plt.subplots(1, len(configs), figsize=(3.2 * len(configs) + 1.6, 0.42 * len(scenarios) + 1.6),
                                 sharey=True, squeeze=False)
        for ax, c in zip(axes[0], configs):
            left = [0] * len(scenarios)
            for state in STATES:
                counts = [sum(r["final_state"] == state for r in by.get((s, c), [])) for s in scenarios]
                ax.barh(scenarios, counts, left=left, color=STATE_COLOUR[state], edgecolor="white", linewidth=1.5,
                        height=0.6, label=state)
                left = [a + b for a, b in zip(left, counts)]
            ax.set_title(c, fontsize=9, color=INK, loc="left")
            ax.set_xlabel("trials", fontsize=8, color=INK)
            ax.xaxis.get_major_locator().set_params(integer=True)
            style(ax)
        axes[0][0].invert_yaxis()
        handles, labels = axes[0][0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=len(STATES), frameon=False, fontsize=8)
        fig.suptitle("Final state of each trial", fontsize=10, color=INK, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0.07, 1, 0.95))
        fig.savefig(out / "outcomes.png", dpi=160)
        plt.close(fig)
        figures.append(("outcomes.png", "Final state of each trial, by scenario and configuration."))

    measured = [s for s in scenarios if truth.get(s)]
    if measured:
        fig, axes = plt.subplots(len(measured), 1, figsize=(6.4, 1.5 * len(measured) + 0.8), sharex=True, squeeze=False)
        for ax, s in zip(axes[:, 0], measured):
            keys = sorted(truth[s])
            for i, key in enumerate(keys):
                v = truth[s][key]
                colour = CLASS_COLOUR.get(key.split("/")[1], INK)
                ax.plot([min(v), max(v)], [i, i], color=colour, linewidth=2, solid_capstyle="round")
                ax.plot(median(v), i, "o", color=colour, markersize=7, markeredgecolor="white", markeredgewidth=1.5)
            ax.axvline(1.0, color=INK, linewidth=0.8)
            ax.axvline(HARM, color=INK, linewidth=0.8, linestyle=(0, (3, 3)))
            ax.set_yticks(range(len(keys)), keys)
            ax.set_ylim(-0.6, len(keys) - 0.4)
            ax.invert_yaxis()
            ax.set_xscale("log")
            ax.set_title(s, fontsize=9, color=INK, loc="left")
            style(ax)
        axes[-1, 0].set_xlabel(f"client p95 while live / mean(before, after)   (dashed: harm threshold {HARM:.2f})",
                               fontsize=8, color=INK)
        fig.suptitle("Effect of each action in production, without verification (median and range)",
                     fontsize=10, color=INK, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        fig.savefig(out / "ground_truth.png", dpi=160)
        plt.close(fig)
        figures.append(("ground_truth.png", "Effect of each action in production. Blue: OLTP. Orange: OLAP."))

    if pairs:
        fig, ax = plt.subplots(figsize=(4.8, 4.6))
        lo = min(min(t, p) for *_, t, p in pairs) * 0.8
        hi = max(max(t, p) for *_, t, p in pairs) * 1.25
        ax.plot([lo, hi], [lo, hi], color=INK, linewidth=0.8)
        for cls, colour in CLASS_COLOUR.items():
            pts = [(t, p) for _, k, t, p in pairs if k.endswith("/" + cls)]
            if pts:
                ax.scatter(*zip(*pts), s=42, color=colour, edgecolor="white", linewidth=1.5, label=cls, zorder=3)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_xlabel("twin: treatment p95 / control p95", fontsize=8, color=INK)
        ax.set_ylabel("production: p95 live / mean(before, after)", fontsize=8, color=INK)
        ax.legend(frameon=False, fontsize=8, loc="upper left")
        style(ax)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_title("Twin prediction against production (line: perfect agreement)", fontsize=10, color=INK, loc="left")
        fig.tight_layout()
        fig.savefig(out / "twin_vs_production.png", dpi=160)
        plt.close(fig)
        figures.append(("twin_vs_production.png", "Each point is one tenant and class in one scenario."))

    md.append("## Figures\n")
    for name, caption in figures:
        md.append(f"![{caption}]({name})\n\n{caption}\n")
    if errors:
        md.append("## Trials that failed to run\n")
        md.append(table(["Scenario", "Configuration", "Repetition", "Error"],
                        [[r["scenario"], r["config"], r.get("rep", ""), r["error"][:200]] for r in errors]))
    text = "\n".join(md)
    (out / "REPORT.md").write_text(text, encoding="utf-8")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(prog="evaluation.report")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--out", default="/results/report")
    args = parser.parse_args()
    print(build(load(args.files), Path(args.out)))


if __name__ == "__main__":
    main()
