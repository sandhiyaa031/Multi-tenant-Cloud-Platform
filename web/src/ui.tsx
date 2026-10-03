// Shared presentation pieces. Nothing here invents data: every component renders what it is given.
import { useState, type ReactNode } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt, type Loaded } from "./api";

export function Card({ title, sub, action, children }: { title?: string; sub?: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || action) && (
        <div className="card-head">
          <div><h2>{title}</h2>{sub && <div className="sub">{sub}</div>}</div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function PageHead({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{children && <p>{children}</p>}</div>
      {action}
    </div>
  );
}

// Renders loading, error and empty states so pages never show a made-up placeholder value.
export function Load<T>({ of, empty, children }: { of: Loaded<T>; empty?: string; children: (data: T) => ReactNode }) {
  if (of.error) return <div className="error">{of.error}</div>;
  if (of.data === undefined) return <div className="muted">Loading…</div>;
  if (empty && Array.isArray(of.data) && of.data.length === 0) return <div className="empty">{empty}</div>;
  return <>{children(of.data)}</>;
}

type Tone = "good" | "warning" | "serious" | "critical" | "info" | "";
const STATE_TONE: Record<string, Tone> = {
  APPLIED: "good", APPROVE: "good", HELD: "good", SAFE: "good", BENEFITS: "good", ACTIVE: "good", APPROVED: "info",
  PROPOSED: "info", VERIFYING: "info", CANARY: "info", AWAITING_APPROVAL: "warning", INCONCLUSIVE: "warning",
  UNCERTAIN: "warning", ROLLBACK_REQUESTED: "warning", ADVISORY: "", SKIPPED: "", NO_DATA: "", PENDING: "",
  REJECTED: "serious", REJECT: "serious", ROLLED_BACK: "serious", HARMED: "critical", FAILED: "critical", DEGRADED: "serious",
};
export function Badge({ children, tone }: { children: string; tone?: Tone }) {
  const t = tone ?? STATE_TONE[children] ?? "";
  return <span className={`badge ${t}`}><span className="dot" />{children.replace(/_/g, " ").toLowerCase()}</span>;
}

// Colour follows the tenant, not its position in whatever list is on screen.
const SLOTS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
export function tenantColors(roles: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  [...new Set(roles)].sort().forEach((r, i) => { out[r] = i < SLOTS.length ? SLOTS[i] : "var(--text-muted)"; });
  return out;
}
export function Swatch({ color }: { color: string }) { return <span className="swatch" style={{ background: color }} />; }

export function Segmented<T extends string | number>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="segmented">
      {options.map((o) => <button key={String(o.value)} className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>{o.label}</button>)}
    </div>
  );
}

export const RANGES = [{ value: 15, label: "15 min" }, { value: 60, label: "1 h" }, { value: 360, label: "6 h" }];

function ChartTooltip({ active, payload, label, format }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="t">{fmt.time(new Date(label).toISOString())}</div>
      {payload.map((p: any) => (
        <div key={p.dataKey}><Swatch color={p.color} />{p.name}: <strong>{format(p.value)}</strong></div>
      ))}
    </div>
  );
}

// One line per series, one y-axis, time on x. `rows` are {t: epoch ms, [series]: value}.
export function TimeSeries({ rows, series, colors, format, reference }: {
  rows: Record<string, number>[]; series: string[]; colors: Record<string, string>; format: (v: number) => string;
  reference?: { value: number; label: string };
}) {
  if (rows.length < 2) return <div className="empty">Not enough data points yet. The collector records one point per interval.</div>;
  return (
    <>
      <div className="chart">
        <ResponsiveContainer>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} stroke="var(--text-muted)" tickLine={false}
              tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} fontSize={12} />
            <YAxis stroke="var(--text-muted)" tickLine={false} axisLine={false} tickFormatter={format} fontSize={12} width={64} />
            <Tooltip content={<ChartTooltip format={format} />} cursor={{ stroke: "var(--text-muted)", strokeDasharray: "3 3" }} />
            {reference && <ReferenceLine y={reference.value} stroke="var(--critical)" strokeDasharray="5 4"
              label={{ value: reference.label, fill: "var(--text-secondary)", fontSize: 12, position: "insideTopRight" }} />}
            {series.map((s) => (
              <Line key={s} type="linear" dataKey={s} name={s} stroke={colors[s] ?? "var(--text-muted)"} strokeWidth={2}
                dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {series.length > 1 && <div className="legend">{series.map((s) => <span key={s}><Swatch color={colors[s] ?? "var(--text-muted)"} />{s}</span>)}</div>}
    </>
  );
}

// Pivots long rows (one per series per timestamp) into the wide shape TimeSeries takes.
export function pivot<T>(rows: T[], time: (r: T) => string, series: (r: T) => string, value: (r: T) => number): Record<string, number>[] {
  const byTime = new Map<number, Record<string, number>>();
  for (const r of rows) {
    const t = new Date(time(r)).getTime();
    const row = byTime.get(t) ?? { t };
    row[series(r)] = value(r);
    byTime.set(t, row);
  }
  const sorted = [...byTime.values()].sort((a, b) => a.t - b.t);
  // Where the collector recorded nothing for a while, break the line instead of drawing
  // a straight stroke across the gap, which would suggest measurements that do not exist.
  const steps = sorted.slice(1).map((r, i) => r.t - sorted[i].t).sort((a, b) => a - b);
  const usual = steps[Math.floor(steps.length / 2)] ?? 0;
  const out: Record<string, number>[] = [];
  sorted.forEach((row, i) => {
    if (i > 0 && usual > 0 && row.t - sorted[i - 1].t > 3 * usual) out.push({ t: sorted[i - 1].t + usual });
    out.push(row);
  });
  return out;
}

const EFFECT_TONE: Record<string, string> = { BENEFITS: "var(--good)", SAFE: "var(--text-secondary)", UNCERTAIN: "var(--warning)", HARMED: "var(--critical)" };

// Twin verdict as a forest plot: each tenant's measured ratio with its confidence interval,
// against the lines the gate decides on. Left of 1.0 is faster.
export function EffectsPlot({ effects, benefitLimit = 0.9, harmLimit = 1.05 }: { effects: Record<string, any>; benefitLimit?: number; harmLimit?: number }) {
  const [hover, setHover] = useState<string | null>(null);
  const keys = Object.keys(effects).filter((k) => effects[k].ratio != null).sort();
  if (keys.length === 0) return <div className="empty">The twin run produced no comparable measurements.</div>;
  const lo = Math.min(0.5, ...keys.map((k) => effects[k].lo)) - 0.05;
  const hi = Math.max(1.5, ...keys.map((k) => effects[k].hi)) + 0.05;
  const W = 640, left = 150, right = 110, rowH = 30, top = 26;
  const x = (v: number) => left + ((v - lo) / (hi - lo)) * (W - left - right);
  const H = top + keys.length * rowH + 26;
  return (
    <div className="table-wrap">
      <svg className="forest" viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W }} role="img" aria-label="Per-tenant effect of the change, with confidence intervals">
        {/* The three decision lines sit close together, so they are named in the caption, not on the plot. */}
        {[benefitLimit, 1, harmLimit].map((v) => (
          <line key={v} x1={x(v)} x2={x(v)} y1={top - 6} y2={H - 22} className="axis" strokeDasharray={v === 1 ? "" : "4 4"} />
        ))}
        <text x={x(1)} y={12} textAnchor="middle" style={{ fill: "var(--text-muted)", fontSize: 11 }}>1.0×</text>
        {keys.map((k, i) => {
          const e = effects[k]; const y = top + i * rowH + rowH / 2; const color = EFFECT_TONE[e.status] ?? "var(--text-secondary)";
          return (
            <g key={k} onMouseEnter={() => setHover(k)} onMouseLeave={() => setHover(null)}>
              <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill={hover === k ? "var(--surface-2)" : "transparent"} />
              <text x={8} y={y + 4}>{k}</text>
              <line x1={x(e.lo)} x2={x(e.hi)} y1={y} y2={y} stroke={color} strokeWidth={2} strokeLinecap="round" />
              <circle cx={x(e.ratio)} cy={y} r={5} fill={color} stroke="var(--surface-1)" strokeWidth={2} />
              <text x={W - right + 10} y={y + 4} style={{ fill: "var(--text-primary)" }}>
                {e.ratio.toFixed(2)}× <tspan style={{ fill: "var(--text-muted)" }}>[{e.lo.toFixed(2)}, {e.hi.toFixed(2)}]</tspan>
              </text>
            </g>
          );
        })}
        <text x={x(lo + 0.02)} y={H - 6} style={{ fill: "var(--text-muted)", fontSize: 11 }}>← faster</text>
        <text x={x(hi - 0.02)} y={H - 6} textAnchor="end" style={{ fill: "var(--text-muted)", fontSize: 11 }}>slower →</text>
      </svg>
      <div className="muted" style={{ fontSize: 12.5 }}>
        Solid line: no change. Dashed left: the target must be entirely left of {benefitLimit.toFixed(2)}× to count as a benefit.
        Dashed right: every other tenant must be entirely left of {harmLimit.toFixed(2)}× to count as unharmed.
      </div>
    </div>
  );
}

export function KeyValue({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table><tbody>
      {rows.map(([k, v]) => <tr key={k}><td className="muted" style={{ width: 200 }}>{k}</td><td>{v}</td></tr>)}
    </tbody></table>
  );
}
