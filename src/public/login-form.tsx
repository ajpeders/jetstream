import { FormEvent, useMemo, useState } from 'react';

export function LoginForm() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const next = useMemo(() => safeNext(), []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const r = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (r.ok) {
        location.href = next;
        return;
      }
      if (r.status === 401) setError('Wrong username or password.');
      else if (r.status === 429) setError('Too many attempts — wait a minute.');
      else setError('Sign-in failed — try again.');
    } catch {
      setError('Network error — try again.');
    }
    setBusy(false);
  };

  return (
    <>
      <h1>jetstream</h1>
      <p className="sub">Sign in to browse the library and watch on demand.</p>
      <form id="login-form" onSubmit={submit}>
        <label htmlFor="username">Username</label>
        <input
          className="du-input"
          id="username"
          name="username"
          type="text"
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          required
          autoFocus
          value={username}
          onChange={(e) => setUsername(e.currentTarget.value)}
        />
        <label htmlFor="password">Password</label>
        <input
          className="du-input"
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.currentTarget.value)}
        />
        <p id="error" className={error ? 'show' : undefined} role="alert">{error}</p>
        <button className="du-btn du-btn-primary" id="submit-btn" type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="back"><a href="/">← Back to the live stream</a></p>
    </>
  );
}

function safeNext(): string {
  const next = new URLSearchParams(location.search).get('next') || '';
  if (next.startsWith('/') && !next.startsWith('//')) return next;
  return '/home';
}
