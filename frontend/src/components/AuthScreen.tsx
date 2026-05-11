import { useState } from 'react';
import { api, type User } from '../api';
import { getBrand } from '../brand';

type Mode = 'login' | 'signup';

export function AuthScreen({ allowSignup, onAuthed }: { allowSignup: boolean; onAuthed: (u: User) => void }) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const user = mode === 'login'
        ? await api.login({ email, password })
        : await api.signup({ email, password, display_name: displayName || undefined });
      onAuthed(user);
    } catch (e: any) {
      setErr(e.message || 'Authentication failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          {(() => { const b = getBrand(); return (<>
            <img
              src={b.logo}
              srcSet={`${b.logo} 1x, ${b.logo2x} 2x`}
              alt={b.alt}
              className="login-logo"
            />
            <h1>{b.name}</h1>
          </>); })()}
        </div>
        <p className="login-tagline">
          {mode === 'login'
            ? 'Sign in to manage your agents.'
            : 'Create an account to get started.'}
        </p>

        {mode === 'signup' && (
          <div className="field">
            <label className="field-label">Display name</label>
            <input
              className="input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="optional"
              maxLength={120}
            />
          </div>
        )}

        <div className="field">
          <label className="field-label">Email</label>
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
          />
        </div>

        <div className="field">
          <label className="field-label">Password</label>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={mode === 'signup' ? 8 : 1}
          />
          {mode === 'signup' && <div className="help">At least 8 characters.</div>}
        </div>

        {err && <div className="error">{err}</div>}

        <button type="submit" className="btn btn-primary" style={{ width: '100%', marginTop: 8 }} disabled={busy}>
          {busy ? (mode === 'login' ? 'Signing in…' : 'Creating account…') : (mode === 'login' ? 'Sign in' : 'Create account')}
        </button>

        {allowSignup && (
          <div className="auth-toggle">
            {mode === 'login' ? (
              <>No account? <button type="button" className="link" onClick={() => { setMode('signup'); setErr(null); }}>Sign up</button></>
            ) : (
              <>Already have an account? <button type="button" className="link" onClick={() => { setMode('login'); setErr(null); }}>Sign in</button></>
            )}
          </div>
        )}
      </form>
    </div>
  );
}
