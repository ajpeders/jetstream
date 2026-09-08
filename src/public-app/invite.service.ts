import { Injectable } from '@angular/core';

/** Outcome of a call to one of the two pre-auth doors. `ok` carries the parsed
 *  body (if any); everything else is the HTTP status the caller maps to copy.
 *  `network` is a thrown fetch — no status at all. */
export type DoorResult =
  | { kind: 'ok'; body: Record<string, unknown> }
  | { kind: 'status'; status: number; body: Record<string, unknown> }
  | { kind: 'network' };

/**
 * The front door's two endpoints. Plain fetch rather than HttpClient: this
 * bundle ships to anonymous visitors, so it stays as small as the page it
 * replaced — pulling in the HTTP module would double it for two POSTs.
 */
@Injectable({ providedIn: 'root' })
export class InviteService {
  /** Redeems a friend code. On success the server has already set the `lt`
   *  cookie; `can_register` in the body says whether the code may also mint
   *  an account. */
  redeem(code: string): Promise<DoorResult> {
    return post('/api/invite/redeem', { code });
  }

  /** Invite-gated signup. The code is re-validated server-side — replaying it
   *  from the redeem step is a convenience, not a trust boundary. */
  register(code: string, username: string, password: string): Promise<DoorResult> {
    return post('/api/auth/register', { code, username, password });
  }
}

async function post(url: string, payload: unknown): Promise<DoorResult> {
  let r: Response;
  try {
    r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch {
    return { kind: 'network' };
  }
  const body = (await r.json().catch(() => ({}))) as Record<string, unknown>;
  return r.ok ? { kind: 'ok', body } : { kind: 'status', status: r.status, body };
}
