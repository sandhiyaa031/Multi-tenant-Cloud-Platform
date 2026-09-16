import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Database, Shield, Building, User, ArrowRight, CheckCircle } from 'lucide-react';

const API = 'http://localhost:8000';

export default function Register() {
    const navigate = useNavigate();
    const [orgName, setOrgName] = useState('');
    const [adminEmail, setAdminEmail] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [success, setSuccess] = useState(false);
    const [error, setError] = useState('');

    const handleRegister = async (e: React.FormEvent) => {
        e.preventDefault();
        setIsLoading(true);
        setError('');

        try {
            const res = await fetch(`${API}/api/auth/register`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ org_name: orgName.trim() }),
            });

            const data = await res.json();

            if (res.status === 201) {
                // Registration succeeded — store token and auto-login
                localStorage.setItem('tenant_token', data.token);
                localStorage.setItem('tenant', data.org_name);
                setSuccess(true);
                setTimeout(() => navigate('/dashboard'), 2000);
            } else if (res.status === 409) {
                setError(`"${orgName}" already exists. Please login instead.`);
            } else {
                setError(data.detail || 'Registration failed. Check the backend logs.');
            }
        } catch {
            setError('Could not reach backend. Is Uvicorn running on port 8000?');
        } finally {
            setIsLoading(false);
        }
    };

    if (success) {
        return (
            <div style={{ display: 'flex', minHeight: '100vh', alignItems: 'center', justifyContent: 'center' }}>
                <motion.div
                    initial={{ scale: 0.9, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    className="glass-panel"
                    style={{ textAlign: 'center', padding: '50px', borderRadius: '16px', maxWidth: '400px' }}
                >
                    <CheckCircle size={56} color="#22c55e" style={{ margin: '0 auto 20px', display: 'block' }} />
                    <h2 style={{ fontSize: '1.5rem', marginBottom: '10px' }}>Tenant Provisioned</h2>
                    <p style={{ color: 'var(--text-muted)' }}>
                        <strong style={{ color: '#fff' }}>{orgName}</strong> has been inserted into{' '}
                        <code>app.organizations</code>. Redirecting to dashboard…
                    </p>
                </motion.div>
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', minHeight: '100vh', alignItems: 'center', justifyContent: 'center', padding: '20px' }}>
            <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.4 }}
                className="glass-panel"
                style={{ width: '100%', maxWidth: '420px', padding: '40px', borderRadius: '16px' }}
            >
                <div style={{ textAlign: 'center', marginBottom: '32px' }}>
                    <Database size={32} color="var(--accent)" style={{ margin: '0 auto 16px', display: 'block' }} />
                    <h2 style={{ fontSize: '1.75rem', fontWeight: '700', marginBottom: '8px' }}>New Tenant Signup</h2>
                    <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>
                        Creates a real row in <code>app.organizations</code>
                    </p>
                </div>

                {error && (
                    <motion.div
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        style={{ background: 'var(--error-bg)', color: 'var(--error)', padding: '12px', borderRadius: '8px', marginBottom: '20px', fontSize: '0.9rem', border: '1px solid rgba(239,68,68,0.2)' }}
                    >
                        {error}
                    </motion.div>
                )}

                <form onSubmit={handleRegister} style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
                    <div>
                        <label style={{ display: 'block', marginBottom: '6px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>Organization Name</label>
                        <div style={{ position: 'relative' }}>
                            <Building size={16} color="var(--text-muted)" style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)' }} />
                            <input
                                type="text" className="input-field" style={{ paddingLeft: '38px' }}
                                placeholder="e.g. org_a, Acme Corp, SecTeam1"
                                value={orgName}
                                onChange={e => setOrgName(e.target.value)}
                                required
                            />
                        </div>
                        <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
                            This becomes the tenant name you use to login.
                        </div>
                    </div>

                    <div>
                        <label style={{ display: 'block', marginBottom: '6px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>Admin Email (display only)</label>
                        <div style={{ position: 'relative' }}>
                            <User size={16} color="var(--text-muted)" style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)' }} />
                            <input
                                type="email" className="input-field" style={{ paddingLeft: '38px' }}
                                placeholder="admin@example.com"
                                value={adminEmail}
                                onChange={e => setAdminEmail(e.target.value)}
                            />
                        </div>
                    </div>

                    <button
                        type="submit"
                        className="primary-button"
                        disabled={isLoading}
                        style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '8px' }}
                    >
                        {isLoading ? 'Inserting into PostgreSQL…' : 'Create Tenant'}
                        {!isLoading && <ArrowRight size={16} />}
                    </button>
                </form>

                <div style={{ textAlign: 'center', marginTop: '24px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>
                    Already have a tenant?{' '}
                    <Link to="/login" style={{ color: 'var(--accent)', textDecoration: 'none', fontWeight: '500' }}>Login</Link>
                </div>
            </motion.div>
        </div>
    );
}
