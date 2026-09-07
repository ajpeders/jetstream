import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

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

@Injectable({ providedIn: 'root' })
export class RequestsService {
  private readonly http = inject(HttpClient);

  /**
   * Completing without emitting is the point: the hand-written poll bailed with
   * `if (!r.ok) return;`, leaving the previous render on screen. Emitting an
   * empty feed instead would blank the list on a transient blip and flicker the
   * manage buttons away, so a failed tick contributes nothing and the signal
   * holds its last good value.
   */
  list(sid: string): Observable<RequestFeed> {
    return this.http.get<Partial<RequestFeed>>('/api/requests', { params: { sid } }).pipe(
      map((r) => ({
        requests: Array.isArray(r.requests) ? r.requests : [],
        can_manage: !!r.can_manage,
      })),
      catchError(() => NO_EMIT),
    );
  }

  /** Toggles the caller's vote. Viewer-accessible — this is the viewers' lever. */
  vote(id: number, sid: string): Promise<Response> {
    return fetch(`/api/request/${id}/vote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sid }),
    });
  }

  /** Approve → queue. Control-tier gated by the /api/control/ branch of the
   *  viewer route gate, so a plain viewer never sees the button that calls it. */
  approve(id: number): Promise<Response> {
    return fetch(`/api/control/requests/${id}/approve`, { method: 'POST' });
  }

  deny(id: number): Promise<Response> {
    return fetch(`/api/control/requests/${id}`, { method: 'DELETE' });
  }
}
