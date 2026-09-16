import { useEffect, useState, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
    Database, LogOut, Activity, Shield, Search,
    CheckCircle, BarChart2, Wifi, Settings, Clock
} from 'lucide-react';

const API = 'http://localhost:8000';

interface ControllerStatus {
    state: 'HEALTHY' | 'THROTTLED';
    l_current_p95_ms: number;
    l_slo_ms: number;
    i_active: number;
    a_active: number;
    consecutive_healthy_windows: number;
}

interface Observation {
    query_type: string;
    latency_ms: number;
    queue_time_ms: number;
    timestamp: string;
}

interface Decision {
    action_taken: string;
    trigger_metric: string;
    timestamp: string;
}

interface SecurityEvent {
    ts: string;
    uid: string;
    id_resp_h: string;
    proto: string;
    orig_bytes: number;
    conn_state: string;
}

const SIDEBAR_ITEMS = [
    { key: 'overview', label: 'Overview', icon: Activity },
    { key: 'investigate', label: 'IP Investigate', icon: Search },
    { key: 'telemetry', label: 'Live Telemetry', icon: Wifi },
    { key: 'results', label: 'Experiment Results', icon: BarChart2 },
    { key: 'settings', label: 'Settings', icon: Settings },
];

export default function Dashboard() {
    const navigate = useNavigate();
    const [tenant, setTenant] = useState('');
    const [token, setToken] = useState('');
    const [activeView, setActiveView] = useState('overview');

    // Controller state
    const [status, setStatus] = useState<ControllerStatus | null>(null);
    const [latencyHistory, setLatencyHistory] = useState<number[]>([]);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    // IP Investigation
    const [ipQuery, setIpQuery] = useState('');
    const [events, setEvents] = useState<SecurityEvent[]>([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [searchError, setSearchError] = useState('');

    // Experiment results
    const [observations, setObservations] = useState<Observation[]>([]);
    const [decisions, setDecisions] = useState<Decision[]>([]);

    useEffect(() => {
        const t = localStorage.getItem('tenant_token') || '';
        const o = localStorage.getItem('tenant') || '';
        if (!t || !o) { navigate('/login'); return; }
        setToken(t);
        setTenant(o);
    }, [navigate]);

    // Live polling for controller status
    const fetchStatus = useCallback(async () => {
        if (!token) return;
        try {
            const res = await fetch(`${API}/api/controller_status`, {
                headers: { Authorization: `Bearer ${token}` }
            });
            if (!res.ok) return;
            const data: ControllerStatus = await res.json();
            setStatus(data);
            setLatencyHistory(prev => {
                const next = [...prev, data.l_current_p95_ms].slice(-60); // keep 60 ticks
                return next;
            });
        } catch { /* server not ready yet */ }
    }, [token]);

    useEffect(() => {
        if (!token) return;
        fetchStatus();
        pollRef.current = setInterval(fetchStatus, 2000);
        return () => { if (pollRef.current) clearInterval(pollRef.current); };
    }, [token, fetchStatus]);

    // Load experiment results when switching to that view
    useEffect(() => {
        if (activeView !== 'results' || !token) return;
        fetch(`${API}/api/experiment/results?limit=100`, {
            headers: { Authorization: `Bearer ${token}` }
        })
            .then(r => r.json())
            .then(d => {
                setObservations(d.observations || []);
                setDecisions(d.controller_decisions || []);
            })
            .catch(() => { });
    }, [activeView, token]);

    const handleInvestigate = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!ipQuery.trim()) return;
        setSearchLoading(true);
        setSearchError('');
        setEvents([]);
        try {
            const res = await fetch(
                `${API}/api/investigate?ip=${encodeURIComponent(ipQuery.trim())}`,
                { headers: { Authorization: `Bearer ${token}` } }
            );
            const data = await res.json();
            if (data.status === 'success') {
                setEvents(data.data);
                if (data.data.length === 0) setSearchError('No events found for that IP.');
            } else {
                setSearchError('Query failed.');
            }
        } catch {
            setSearchError('Could not reach backend.');
        } finally {
            setSearchLoading(false);
        }
    };

    const handleLogout = () => {
        localStorage.clear();
        navigate('/login');
    };

    // ── Views ───────────────────────────────────────────────────────────────

    const ViewOverview = () => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            {/* Status cards */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '16px' }}>
                {[
                    { label: 'Controller State', value: status?.state ?? '…', icon: Shield, color: status?.state === 'THROTTLED' ? '#ef4444' : '#22c55e' },
                    { label: 'P95 Latency', value: status ? `${status.l_current_p95_ms.toFixed(1)} ms` : '…', icon: Clock, color: status && status.l_current_p95_ms > status.l_slo_ms ? '#ef4444' : '#22c55e' },
                    { label: 'Interactive Active', value: status?.i_active ?? '…', icon: Activity, color: 'var(--accent)' },
                    { label: 'Analytical Active', value: status?.a_active ?? '…', icon: BarChart2, color: '#f59e0b' },
                ].map(({ label, value, icon: Icon, color }) => (
                    <div key={label} className="glass-panel" style={{ padding: '20px', borderRadius: '12px' }}>
                        <Icon size={20} color={color} style={{ marginBottom: '10px' }} />
                        <div style={{ fontSize: '1.6rem', fontWeight: 'bold', color }}>{value}</div>
                        <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '4px' }}>{label}</div>
                    </div>
                ))}
            </div>

            {/* P95 sparkline */}
            <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
                <div style={{ marginBottom: '12px', fontWeight: '600' }}>Live P95 Interactive Latency (last 60s)</div>
                <Sparkline data={latencyHistory} slo={status?.l_slo_ms ?? 100} />
                <div style={{ marginTop: '8px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    Polls every 2s · Red dashed line = SLO threshold ({status?.l_slo_ms ?? 100}ms)
                </div>
            </div>

            {/* Controller event log */}
            <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
                <div style={{ marginBottom: '12px', fontWeight: '600' }}>Tenant Isolation Proof</div>
                {status ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        <CheckCircle size={32} color="#22c55e" />
                        <p style={{ color: 'var(--text-muted)', lineHeight: '1.6', margin: 0 }}>
                            You are locked into the <strong>{tenant.toUpperCase()}</strong> partition.
                            Row-Level Security is enforced at the transaction layer — no WHERE clause needed in application code.
                            The controller is currently <strong style={{ color: status.state === 'THROTTLED' ? '#ef4444' : '#22c55e' }}>{status.state}</strong>.
                        </p>
                    </div>
                ) : (
                    <p style={{ color: 'var(--text-muted)' }}>Connecting to backend…</p>
                )}
            </div>
        </div>
    );

    const ViewInvestigate = () => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
                <div style={{ fontWeight: '600', marginBottom: '16px' }}>IP Timeline Investigation</div>
                <form onSubmit={handleInvestigate} style={{ display: 'flex', gap: '12px' }}>
                    <input
                        type="text"
                        className="input-field"
                        placeholder="Enter source IP (e.g. 147.32.84.165)"
                        value={ipQuery}
                        onChange={e => setIpQuery(e.target.value)}
                        style={{ flex: 1 }}
                    />
                    <button type="submit" className="primary-button" disabled={searchLoading} style={{ whiteSpace: 'nowrap' }}>
                        {searchLoading ? 'Searching…' : 'Investigate'}
                    </button>
                </form>
                {searchError && <div style={{ color: '#ef4444', marginTop: '10px', fontSize: '0.9rem' }}>{searchError}</div>}
            </div>

            {events.length > 0 && (
                <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', overflowX: 'auto' }}>
                    <div style={{ fontWeight: '600', marginBottom: '16px' }}>{events.length} Events Found</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                        <thead>
                            <tr style={{ color: 'var(--text-muted)', textAlign: 'left', borderBottom: '1px solid rgba(148,163,184,0.1)' }}>
                                {['Timestamp', 'UID', 'Dest IP', 'Proto', 'Bytes', 'State'].map(h => (
                                    <th key={h} style={{ padding: '8px 12px', fontWeight: '600' }}>{h}</th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {events.map((ev, i) => (
                                <tr key={i} style={{ borderBottom: '1px solid rgba(148,163,184,0.05)' }}>
                                    <td style={{ padding: '8px 12px', color: 'var(--text-muted)' }}>{new Date(ev.ts).toLocaleTimeString()}</td>
                                    <td style={{ padding: '8px 12px', fontFamily: 'monospace', fontSize: '0.75rem' }}>{ev.uid.slice(0, 12)}…</td>
                                    <td style={{ padding: '8px 12px' }}>{ev.id_resp_h}</td>
                                    <td style={{ padding: '8px 12px' }}><span style={{ background: 'rgba(56,189,248,0.1)', padding: '2px 8px', borderRadius: '4px', color: 'var(--accent)' }}>{ev.proto}</span></td>
                                    <td style={{ padding: '8px 12px' }}>{ev.orig_bytes?.toLocaleString() ?? '—'}</td>
                                    <td style={{ padding: '8px 12px', color: ev.conn_state === 'S0' ? '#ef4444' : '#22c55e' }}>{ev.conn_state}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );

    const ViewTelemetry = () => (
        <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
            <div style={{ fontWeight: '600', marginBottom: '20px' }}>Live Controller Telemetry</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '24px' }}>
                {[
                    ['Current P95', `${status?.l_current_p95_ms.toFixed(2) ?? '—'} ms`],
                    ['SLO Threshold', `${status?.l_slo_ms ?? '—'} ms`],
                    ['Resume Threshold', '70 ms'],
                    ['Healthy Windows', `${status?.consecutive_healthy_windows ?? 0} / 3`],
                    ['Interactive Slots Used', `${status?.i_active ?? 0}`],
                    ['Analytical Slots Used', `${status?.a_active ?? 0}`],
                ].map(([label, val]) => (
                    <div key={label} style={{ background: 'rgba(15,23,42,0.6)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(148,163,184,0.1)' }}>
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', marginBottom: '4px' }}>{label}</div>
                        <div style={{ fontWeight: '600', fontSize: '1.1rem' }}>{val}</div>
                    </div>
                ))}
            </div>
            <Sparkline data={latencyHistory} slo={status?.l_slo_ms ?? 100} />
        </div>
    );

    const Sparkline = ({ data, slo }: { data: number[]; slo: number }) => {
        const w = 300, h = 80;
        if (data.length < 2) return <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', padding: '20px 0' }}>Waiting for data (run the scenario_runner.py to generate traffic)…</div>;
        const max = Math.max(...data, slo * 1.1);
        const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - (v / max) * h}`).join(' ');
        const sloY = h - (slo / max) * h;
        return (
            <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', maxWidth: '420px', display: 'block' }}>
                <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="2" />
                <line x1={0} y1={sloY} x2={w} y2={sloY} stroke="#ef4444" strokeWidth="1" strokeDasharray="4 3" />
                <text x={w - 4} y={sloY - 4} fill="#ef4444" fontSize="9" textAnchor="end">SLO {slo}ms</text>
            </svg>
        );
    };

    const ViewResults = () => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', overflowX: 'auto' }}>
                <div style={{ fontWeight: '600', marginBottom: '16px' }}>Controller Decisions (THROTTLE / RESUME events)</div>
                {decisions.length === 0 ? (
                    <p style={{ color: 'var(--text-muted)' }}>No decisions yet. Run <code>python backend/workloads/scenario_runner.py</code> to generate traffic and trigger the controller.</p>
                ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                        <thead>
                            <tr style={{ color: 'var(--text-muted)', textAlign: 'left', borderBottom: '1px solid rgba(148,163,184,0.1)' }}>
                                {['Action', 'Trigger Metric', 'Time'].map(h => <th key={h} style={{ padding: '8px 12px' }}>{h}</th>)}
                            </tr>
                        </thead>
                        <tbody>
                            {decisions.map((d, i) => (
                                <tr key={i} style={{ borderBottom: '1px solid rgba(148,163,184,0.05)' }}>
                                    <td style={{ padding: '8px 12px', color: d.action_taken === 'THROTTLE' ? '#ef4444' : '#22c55e', fontWeight: '600' }}>{d.action_taken}</td>
                                    <td style={{ padding: '8px 12px', fontFamily: 'monospace', fontSize: '0.8rem' }}>{d.trigger_metric}</td>
                                    <td style={{ padding: '8px 12px', color: 'var(--text-muted)' }}>{new Date(d.timestamp).toLocaleTimeString()}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>

            <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', overflowX: 'auto' }}>
                <div style={{ fontWeight: '600', marginBottom: '16px' }}>Recent Query Observations ({observations.length})</div>
                {observations.length === 0 ? (
                    <p style={{ color: 'var(--text-muted)' }}>No observations yet. Make some API calls to populate this table.</p>
                ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                        <thead>
                            <tr style={{ color: 'var(--text-muted)', textAlign: 'left', borderBottom: '1px solid rgba(148,163,184,0.1)' }}>
                                {['Type', 'Latency', 'Queue Wait', 'Time'].map(h => <th key={h} style={{ padding: '8px 12px' }}>{h}</th>)}
                            </tr>
                        </thead>
                        <tbody>
                            {observations.slice(0, 50).map((o, i) => (
                                <tr key={i} style={{ borderBottom: '1px solid rgba(148,163,184,0.05)' }}>
                                    <td style={{ padding: '8px 12px' }}><span style={{ background: o.query_type === 'interactive' ? 'rgba(34,197,94,0.1)' : 'rgba(245,158,11,0.1)', color: o.query_type === 'interactive' ? '#22c55e' : '#f59e0b', padding: '2px 8px', borderRadius: '4px' }}>{o.query_type}</span></td>
                                    <td style={{ padding: '8px 12px', color: o.latency_ms > 100 ? '#ef4444' : 'inherit' }}>{o.latency_ms.toFixed(1)} ms</td>
                                    <td style={{ padding: '8px 12px', color: 'var(--text-muted)' }}>{o.queue_time_ms.toFixed(1)} ms</td>
                                    <td style={{ padding: '8px 12px', color: 'var(--text-muted)' }}>{new Date(o.timestamp).toLocaleTimeString()}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>
        </div>
    );

    const VIEWS: Record<string, React.FC> = {
        overview: ViewOverview,
        investigate: ViewInvestigate,
        telemetry: ViewTelemetry,
        results: ViewResults,
        settings: () => <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', color: 'var(--text-muted)' }}>Settings panel — coming soon.</div>,
    };

    const ActiveView = VIEWS[activeView] ?? ViewOverview;

    return (
        <div style={{ display: 'flex', minHeight: '100vh', flexDirection: 'column', padding: '24px' }}>
            {/* Navbar */}
            <motion.nav
                initial={{ y: -50, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ duration: 0.4 }}
                className="glass-panel"
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 28px', borderRadius: '14px', marginBottom: '24px' }}
            >
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <Database size={22} color="var(--accent)" />
                    <span style={{ fontWeight: '700', fontSize: '1.1rem' }}>DBPilot Cloud</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                    {status && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.8rem', color: status.state === 'THROTTLED' ? '#ef4444' : '#22c55e' }}>
                            <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: status.state === 'THROTTLED' ? '#ef4444' : '#22c55e', boxShadow: `0 0 8px ${status.state === 'THROTTLED' ? '#ef4444' : '#22c55e'}` }} />
                            {status.state} · {status.l_current_p95_ms.toFixed(0)}ms p95
                        </div>
                    )}
                    <div style={{ fontSize: '0.85rem', color: 'var(--accent)', background: 'rgba(56,189,248,0.1)', padding: '5px 12px', borderRadius: '20px', border: '1px solid rgba(56,189,248,0.2)' }}>
                        {tenant.toUpperCase()}
                    </div>
                    <button onClick={handleLogout} style={{ background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.85rem' }}>
                        <LogOut size={14} /> Logout
                    </button>
                </div>
            </motion.nav>

            {/* Body */}
            <div style={{ display: 'flex', gap: '20px', flex: 1 }}>
                {/* Sidebar */}
                <motion.div
                    initial={{ x: -40, opacity: 0 }}
                    animate={{ x: 0, opacity: 1 }}
                    transition={{ duration: 0.4, delay: 0.1 }}
                    className="glass-panel"
                    style={{ width: '220px', borderRadius: '14px', padding: '20px', flexShrink: 0 }}
                >
                    <div style={{ color: 'var(--text-muted)', fontSize: '0.7rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '14px' }}>Control Plane</div>
                    {SIDEBAR_ITEMS.map(({ key, label, icon: Icon }) => {
                        const active = activeView === key;
                        return (
                            <div
                                key={key}
                                onClick={() => setActiveView(key)}
                                style={{
                                    display: 'flex', alignItems: 'center', gap: '10px',
                                    padding: '10px 12px', borderRadius: '8px', cursor: 'pointer',
                                    background: active ? 'var(--accent-glow)' : 'transparent',
                                    color: active ? '#fff' : 'var(--text-muted)',
                                    borderLeft: `3px solid ${active ? 'var(--accent)' : 'transparent'}`,
                                    marginBottom: '4px', fontSize: '0.9rem', fontWeight: active ? '600' : '400',
                                    transition: 'all 0.15s',
                                }}
                            >
                                <Icon size={15} />
                                {label}
                            </div>
                        );
                    })}
                </motion.div>

                {/* Content */}
                <motion.div
                    key={activeView}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3 }}
                    style={{ flex: 1 }}
                >
                    <ActiveView />
                </motion.div>
            </div>
        </div>
    );
}
