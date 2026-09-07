import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

/** One private per-user VOD stream. */
export interface VodSession {
  session_id: string;
  username: string | null;
  title: string | null;
  start_offset: number | null;
  started_at: number | null;
  last_access: number | null;
}

export interface VodSessionFeed {
  sessions: VodSession[];
  /** VOD_MAX_SESSIONS — shown as "n of cap streams". */
  cap: number | null;
}

@Injectable({ providedIn: 'root' })
export class VodSessionsService {
  private readonly http = inject(HttpClient);

  /** A failed tick contributes nothing, so the table holds its last good
   *  render rather than flashing "No active streams" on a blip. */
  list(): Observable<VodSessionFeed> {
    return this.http.get<Partial<VodSessionFeed>>('/admin/api/vod/sessions').pipe(
      map((r) => ({
        sessions: Array.isArray(r.sessions) ? r.sessions : [],
        cap: r.cap ?? null,
      })),
      catchError(() => NO_EMIT),
    );
  }

  kill(sessionId: string): Promise<Response> {
    return fetch(`/admin/api/vod/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    });
  }
}
