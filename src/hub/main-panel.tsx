import { FormEvent, useEffect, useRef, useState } from 'react';

import { changePassword, nowPlaying } from './api';

export function HubMain() {
  const [title, setTitle] = useState<string | null>(null);
  const [checkedNow, setCheckedNow] = useState(false);
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [ok, setOk] = useState('');
  const [busy, setBusy] = useState(false);
  const currentInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    void nowPlaying().then((result) => {
      if (!alive) return;
      setTitle(result?.playing && result.title ? result.title : null);
      setCheckedNow(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  const setPasswordOpen = (wantOpen: boolean) => {
    setOpen(wantOpen);
    setError('');
    if (wantOpen) {
      setOk('');
      requestAnimationFrame(() => currentInput.current?.focus());
    } else {
      setCurrent('');
      setNext('');
      setConfirm('');
    }
  };

  const togglePassword = () => setPasswordOpen(!open);

  const submitPassword = async (e: FormEvent) => {
    e.preventDefault();
    if (!current) {
      setError('Enter your current password.');
      currentInput.current?.focus();
      return;
    }
    if (next.length < 8) {
      setError('New password must be at least 8 characters.');
      document.getElementById('pw-new')?.focus();
      return;
    }
    if (next !== confirm) {
      setError("New passwords don't match.");
      document.getElementById('pw-confirm')?.focus();
      return;
    }
    if (next === current) {
      setError('New password must be different from your current one.');
      document.getElementById('pw-new')?.focus();
      return;
    }

    setError('');
    setBusy(true);
    try {
      const r = await changePassword(current, next);
      if (r.status === 401) {
        setError("That's not your current password.");
        setCurrent('');
        requestAnimationFrame(() => currentInput.current?.focus());
        return;
      }
      if (r.status === 429) {
        setError('Too many attempts. Try again in a few minutes.');
        return;
      }
      const body: { error?: string; message?: string } = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (body.error === 'bad_new_password') {
          setError(body.message || "That new password isn't allowed.");
        } else {
          setError("Couldn't change your password. Try again.");
        }
        return;
      }
      setPasswordOpen(false);
      setOk("Password changed. Your other devices have been signed out — you're still signed in here.");
    } catch {
      setError('Network error. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="lead">Two ways to watch.</p>
      <div className="choices">
        <a className="choice du-card" href="/">
          <span className="kicker">Live</span>
          <h2>Live stream</h2>
          <p>Watch what everyone's watching, together.</p>
          <span className="now" id="now-playing">
            {title ? <>Now playing: <strong>{title}</strong></> : checkedNow ? 'Nothing playing right now.' : '\u00a0'}
          </span>
          <span className="go">Join the stream →</span>
        </a>
        <a className="choice du-card" href="/library">
          <span className="kicker">On demand</span>
          <h2>My library</h2>
          <p>Browse everything and watch on demand, privately.</p>
          <span className="go">Open the library →</span>
        </a>
      </div>

      <section className="account">
        <button className="du-btn" id="pw-toggle" type="button" aria-expanded={open} aria-controls="pw-form" onClick={togglePassword}>
          {open ? 'Cancel' : 'Change password'}
        </button>
        <p className="pw-msg ok" id="pw-ok" role="status">{ok}</p>
        <form id="pw-form" className={open ? 'open' : undefined} noValidate autoComplete="on" onSubmit={submitPassword}>
          <label htmlFor="pw-current">Current password
            <input className="du-input" id="pw-current" name="current_password" type="password" autoComplete="current-password" ref={currentInput} value={current} onChange={(e) => setCurrent(e.currentTarget.value)} />
          </label>
          <label htmlFor="pw-new">New password
            <input className="du-input" id="pw-new" name="new_password" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.currentTarget.value)} />
          </label>
          <label htmlFor="pw-confirm">Confirm new password
            <input className="du-input" id="pw-confirm" name="confirm_password" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.currentTarget.value)} />
          </label>
          <p className="pw-msg error" id="pw-error" role="alert">{error}</p>
          <button className="du-btn du-btn-primary" id="pw-submit" type="submit" disabled={busy}>
            Update password
          </button>
        </form>
      </section>
    </>
  );
}
