import { FormEvent, useEffect, useMemo, useState } from 'react';

import { fmtAgo, publishRefresh, useActions, usePolled } from '@/lib';
import {
  createUser,
  listUsers,
  removeUser,
  setUserDisabled,
  setUserPassword,
  type UserAccount,
} from './services';

declare global {
  interface Window {
    jetstream?: Record<string, () => void>;
    jetstreamShowToast?: (msg: string) => void;
    showToast?: (msg: string) => void;
  }
}

export function UsersPanel() {
  const act = useActions();
  const poll = usePolled<UserAccount[]>(listUsers, { initial: [], intervalMs: 10000 });
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [formError, setFormError] = useState('');
  const now = useMemo(() => Date.now() / 1000, [poll.data]);

  useEffect(() => {
    publishRefresh('users', poll.refresh);
    return () => {
      if (window.jetstream?.users === poll.refresh) delete window.jetstream.users;
    };
  }, [poll.refresh]);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setFormError('');
    const nextUsername = username.trim();
    if (!nextUsername || !password) return;
    try {
      const res = await createUser(nextUsername, password);
      const body: { error?: string; message?: string } = await res.json().catch(() => ({}));
      if (!res.ok) {
        setFormError(body.error === 'duplicate' ? 'That username is already taken.' : body.message || body.error || `HTTP ${res.status}`);
        return;
      }
      setUsername('');
      setPassword('');
      notify(`User ${nextUsername} created`);
      poll.refresh();
    } catch (err) {
      notify(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const resetPassword = async (u: UserAccount) => {
    const pw = prompt(`New password for ${u.username} (min 8 chars). Their sessions get logged out.`);
    if (pw == null) return;
    await act.run(`pw:${u.id}`, () => setUserPassword(u.id, pw), {
      ok: `Password reset for ${u.username}`,
      fail: 'Password reset failed',
    });
  };
  const toggleDisabled = async (u: UserAccount) => {
    const disable = !u.disabled;
    await act.run(`dis:${u.id}`, () => setUserDisabled(u.id, disable), {
      ok: disable ? `${u.username} disabled — sessions revoked` : `${u.username} enabled`,
      fail: 'Update failed',
    });
    poll.refresh();
    window.jetstream?.vodSessions?.();
  };
  const remove = async (u: UserAccount) => {
    if (!confirm(`Delete user ${u.username}? Their sessions are revoked and any active stream is killed.`)) return;
    await act.run(`del:${u.id}`, () => removeUser(u.id), {
      ok: `Deleted ${u.username}`,
      fail: 'Delete failed',
    });
    poll.refresh();
    window.jetstream?.vodSessions?.();
  };

  return (
    <>
      <h2>User accounts</h2>
      <form autoComplete="off" onSubmit={create}>
        <input type="text" placeholder="username (a-z 0-9 _ . -)" maxLength={32} autoComplete="off" required value={username} onChange={(e) => setUsername(e.currentTarget.value)} />
        <input type="password" placeholder="password (min 8)" minLength={8} autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
        <button type="submit" disabled={act.busy('create')}>Create user</button>
      </form>
      <div className="form-err" style={{ display: formError ? 'block' : 'none' }}>{formError}</div>
      <ul>
        {poll.data.length ? poll.data.map((u) => (
          <li key={u.id} className={u.disabled ? 'disabled' : undefined}>
            <span className="uname">{u.username}</span>
            {u.active_vod && <span className="udot" title="Streaming now"></span>}
            {u.disabled && <span className="uflag" style={flagStyle}>DISABLED</span>}
            {u.invited_by && <span className="uvia" title="Self-registered with this invite code">via {u.invited_by}</span>}
            <span className="umeta">{meta(u, now)}</span>
            <button type="button" disabled={act.busy(`pw:${u.id}`)} onClick={() => resetPassword(u)}>Password</button>
            <button type="button" disabled={act.busy(`dis:${u.id}`)} onClick={() => toggleDisabled(u)}>{u.disabled ? 'Enable' : 'Disable'}</button>
            <button type="button" className="danger" disabled={act.busy(`del:${u.id}`)} onClick={() => remove(u)}>Delete</button>
          </li>
        )) : <li><span className="umeta">No user accounts yet.</span></li>}
      </ul>
    </>
  );
}

function meta(u: UserAccount, now: number): string {
  return `created ${when(u.created, now)} · last login ${when(u.last_login, now)}`;
}

function when(ts: number | null | undefined, now: number): string {
  return ts ? fmtAgo(now - ts) : 'never';
}

function notify(message: string): void {
  (window.jetstreamShowToast || window.showToast)?.(message);
}

const flagStyle = {
  flexShrink: 0,
  fontSize: '0.65rem',
  letterSpacing: '0.04em',
  fontWeight: 600,
  padding: '0.08rem 0.4rem',
  borderRadius: '3px',
  background: '#3a1818',
  color: '#ff8a8a',
  border: '1px solid #5a2424',
};
