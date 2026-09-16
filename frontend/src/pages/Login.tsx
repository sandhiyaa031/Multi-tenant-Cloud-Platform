import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Database, Lock, Building, ArrowRight } from 'lucide-react';

export default function Login() {
    const [org, setOrg] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const navigate = useNavigate();

    const handleLogin = async (e: React.FormEvent) => {
        e.preventDefault();
        setIsLoading(true);
        setError('');

        try {
            const response = await fetch('http://localhost:8000/api/auth/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ org_name: org }),
            });

            const data = await response.json();

            if (response.ok && data.token) {
                localStorage.setItem('tenant_token', data.token);
                localStorage.setItem('tenant', data.org_name || org);
                navigate('/dashboard');
            } else {
                setError(data.detail || 'Organization not found. Register first or check the name.');
            }
        } catch (err) {
            setError('Failed to connect to backend server. Is Python Uvicorn running on port 8000?');
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div style={{ display: 'flex', minHeight: '100vh', alignItems: 'center', justifyContent: 'center', padding: '20px' }}>
            <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5 }}
                className="glass-panel"
                style={{ width: '100%', maxWidth: '420px', padding: '40px', borderRadius: '16px' }}
            >
                <div style={{ textAlign: 'center', marginBottom: '32px' }}>
                    <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '16px' }}>
                        <div style={{ background: 'var(--accent-glow)', padding: '12px', borderRadius: '12px' }}>
                            <Database size={32} color="var(--accent)" />
                        </div>
                    </div>
                    <h2 style={{ fontSize: '1.75rem', fontWeight: '700', marginBottom: '8px' }}>DBPilot Cloud</h2>
                    <p style={{ color: 'var(--text-muted)', fontSize: '0.95rem' }}>Secure Multi-Tenant Authentication</p>
                </div>

                {error && (
                    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} style={{ background: 'var(--error-bg)', color: 'var(--error)', padding: '12px', borderRadius: '8px', marginBottom: '20px', fontSize: '0.9rem', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
                        {error}
                    </motion.div>
                )}

                <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
                    <div>
                        <label style={{ display: 'block', marginBottom: '8px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>Organization ID</label>
                        <div style={{ position: 'relative' }}>
                            <Building size={18} color="var(--text-muted)" style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)' }} />
                            <input
                                type="text"
                                className="input-field"
                                style={{ paddingLeft: '40px' }}
                                placeholder="Enter Organization (e.g., org_a)"
                                value={org}
                                onChange={(e) => setOrg(e.target.value)}
                                required
                            />
                        </div>
                    </div>

                    <div>
                        <label style={{ display: 'block', marginBottom: '8px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>Admin Password</label>
                        <div style={{ position: 'relative' }}>
                            <Lock size={18} color="var(--text-muted)" style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)' }} />
                            <input
                                type="password"
                                className="input-field"
                                style={{ paddingLeft: '40px' }}
                                placeholder="••••••••"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                            />
                        </div>
                    </div>

                    <button type="submit" className="primary-button" disabled={isLoading} style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '10px' }}>
                        {isLoading ? 'Authenticating...' : 'Access Workspace'}
                        {!isLoading && <ArrowRight size={18} />}
                    </button>
                </form>

                <div style={{ textAlign: 'center', marginTop: '24px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>
                    Don't have an enterprise account? <Link to="/register" style={{ color: 'var(--accent)', textDecoration: 'none', fontWeight: '500' }}>Register Organization</Link>
                </div>
            </motion.div>
        </div>
    );
}
