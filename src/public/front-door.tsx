import { FormEvent, useEffect, useRef, useState } from 'react';

import { describe, redeem, register } from './invite';

/** A (enter friend code) → B (accepted; watch or register) → C (register form).
 *  B is only reached when the redeemed code carries can_register; a viewer-tier
 *  code goes straight to "/" because the redeem response already cookied them. */
type View = 'code' | 'choice' | 'register';

/** Mirrors the server's rules so mistakes surface without a round trip; the
 *  server remains the authority. */
const USERNAME_RE = /^[a-z0-9_.-]{3,32}$/;

/** Mounts into the page's <div class="card du-card" jet-front-door>, so
 *  home.css keeps matching every id and class below unchanged. */
export function FrontDoor() {
  const [view, setViewState] = useState<View>('code');
  const [busy, setBusy] = useState(false);
  // Convenience only: the real ?t= invite flow is handled server-side before
  // this page ever renders, so prefill and let the visitor press Watch.
  const [code, setCode] = useState(() => new URLSearchParams(location.search).get('t') ?? '');
  const [codeError, setCodeError] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [password2, setPassword2] = useState('');
  const [registerError, setRegisterError] = useState('');
  /** The redeemed code is replayed on register — the server re-validates it,
   *  so this is a convenience, not a trust boundary. */
  const acceptedCode = useRef('');
  const host = useRef<HTMLDivElement>(null);

  // Land focus on each state's first control, once it exists in the DOM.
  useEffect(() => {
    host.current?.querySelector<HTMLElement>('input, a.action')?.focus();
  }, [view]);

  const show = (next: View) => {
    setCodeError('');
    setRegisterError('');
    setViewState(next);
  };

  const onRedeem = async (e: FormEvent) => {
    e.preventDefault();
    setCodeError('');
    const trimmed = code.trim();
    if (!trimmed) {
      setCodeError('Enter your friend code.');
      return;
    }
    setBusy(true);
    const r = await redeem(trimmed);
    setBusy(false);
    if (r.kind === 'ok') {
      acceptedCode.current = trimmed;
      if (r.body['can_register']) show('choice');
      else location.href = '/';
      return;
    }
    setCodeError(describe(r, {
      401: "That code isn't valid.",
      429: 'Too many tries — wait a minute.',
    }, 'Something went wrong — try again.'));
  };

  const onRegister = async (e: FormEvent) => {
    e.preventDefault();
    setRegisterError('');
    const name = username.trim().toLowerCase();
    if (!USERNAME_RE.test(name)) {
      setRegisterError('Username must be 3–32 characters, using letters, numbers, _ . or - only.');
      return;
    }
    if (password.length < 8) {
      setRegisterError('Password must be at least 8 characters.');
      return;
    }
    if (password !== password2) {
      setRegisterError("Passwords don't match.");
      return;
    }
    setBusy(true);
    const r = await register(acceptedCode.current, name, password);
    setBusy(false);
    if (r.kind === 'ok') {
      location.href = '/home';
      return;
    }
    if (r.kind === 'status' && r.status === 400) {
      const err = r.body['error'];
      if (err === 'bad_username' || err === 'bad_password') {
        const msg = r.body['message'];
        setRegisterError(typeof msg === 'string' && msg ? msg : "That won't work — try something else.");
        return;
      }
    }
    setRegisterError(describe(r, {
      409: 'That username is taken.',
      403: "That code can't create accounts.",
      401: "That code isn't valid.",
      429: 'Too many tries — wait a minute.',
    }, "Couldn't create the account — try again."));
  };

  return (
    <div ref={host} style={{ display: 'contents' }}>
      <h1>jetstream</h1>

      {view === 'code' && (
        <section id="state-code">
          <p className="sub">A private stream for friends. Enter the code you were sent to start watching.</p>
          <form id="code-form" onSubmit={onRedeem}>
            <label htmlFor="code">Friend code</label>
            <input className="du-input" id="code" name="code" type="text" autoComplete="one-time-code"
                   autoCapitalize="none" autoCorrect="off" spellCheck={false} required
                   value={code} onChange={(e) => setCode(e.target.value)} />
            <p id="code-error" className={'error' + (codeError ? ' show' : '')} role="alert">{codeError}</p>
            <button className="du-btn du-btn-primary" id="code-btn" type="submit" disabled={busy}>
              {busy ? 'Checking…' : 'Watch'}
            </button>
          </form>
          <p className="divider">Already have an account? <a href="/login">Sign in</a></p>
        </section>
      )}

      {view === 'choice' && (
        <section id="state-choice">
          <p className="ok">Code accepted.</p>
          <p className="sub">You're in. Head straight to the stream, or make an account first — it's optional, and you can always do it later.</p>
          <a className="action du-btn du-btn-primary" id="go-watch" href="/">Watch the live stream →</a>
          <button className="quiet du-btn" id="go-register" type="button" onClick={() => show('register')}>
            Create an account — unlocks the on-demand library
          </button>
        </section>
      )}

      {view === 'register' && (
        <section id="state-register">
          <p className="sub">Create an account to browse the library and watch anything on demand, privately.</p>
          <form id="register-form" onSubmit={onRegister}>
            <label htmlFor="username">Username</label>
            <input id="username" name="username" type="text" autoComplete="username"
                   autoCapitalize="none" autoCorrect="off" spellCheck={false} required
                   value={username} onChange={(e) => setUsername(e.target.value)} />
            <p className="hint">3–32 characters: lowercase letters, numbers, and <code>_ . -</code></p>
            <label htmlFor="password">Password</label>
            <input id="password" name="password" type="password" autoComplete="new-password" required
                   value={password} onChange={(e) => setPassword(e.target.value)} />
            <p className="hint">At least 8 characters.</p>
            <label htmlFor="password2">Confirm password</label>
            <input id="password2" name="password2" type="password" autoComplete="new-password" required
                   value={password2} onChange={(e) => setPassword2(e.target.value)} />
            <p id="register-error" className={'error' + (registerError ? ' show' : '')} role="alert">{registerError}</p>
            <button id="register-btn" type="submit" disabled={busy}>
              {busy ? 'Creating…' : 'Create account'}
            </button>
          </form>
          <p className="back"><button id="register-back" type="button" onClick={() => show('choice')}>← Back</button></p>
        </section>
      )}
    </div>
  );
}
