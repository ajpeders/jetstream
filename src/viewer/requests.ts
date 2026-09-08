import { getJson } from '@/lib';

/** One pending request in the community wishlist. `voted` is per-caller — the
 *  server resolves it from the `sid` the read carries. */
export interface MediaRequest {
  id: number;
  title: string | null;
  requester: string | null;
  votes: number;
  voted: boolean;
}

/** /api/requests answers the list and the caller's permission in one payload,
 *  so they are polled together — `can_manage` gates the Queue/deny controls
 *  and can change mid-session (a friend token arriving on a later poll). */
export interface RequestFeed {
  requests: MediaRequest[];
  can_manage: boolean;
}

/** Resolves `undefined` on failure: the hand-written poll bailed with
 *  `if (!r.ok) return;`, leaving the previous render on screen. An empty feed
 *  instead would blank the list on a blip and flicker the manage buttons. */
export async function listRequests(sid: string): Promise<RequestFeed | undefined> {
  const r = await getJson<Partial<RequestFeed>>(`/api/requests?sid=${encodeURIComponent(sid)}`);
  if (!r) return undefined;
  return {
    requests: Array.isArray(r.requests) ? r.requests : [],
    can_manage: !!r.can_manage,
  };
}

/** Toggles the caller's vote. Viewer-accessible — this is the viewers' lever. */
export function vote(id: number, sid: string): Promise<Response> {
  return fetch(`/api/request/${id}/vote`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid }),
  });
}

/** Approve → queue. Control-tier gated by the /api/control/ branch of the
 *  viewer route gate, so a plain viewer never sees the button that calls it. */
export function approve(id: number): Promise<Response> {
  return fetch(`/api/control/requests/${id}/approve`, { method: 'POST' });
}

export function deny(id: number): Promise<Response> {
  return fetch(`/api/control/requests/${id}`, { method: 'DELETE' });
}
