import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, of } from 'rxjs';
import { catchError } from 'rxjs/operators';

/** What the client captured at the moment the viewer hit ⚑ Report. */
export interface ReportClient {
  title?: string;
  path?: string;
  position_seconds?: number | null;
  playback_mode?: string;
  muted?: boolean | null;
  fullscreen?: boolean | null;
}

/** What the server knew about the encode at the same moment. */
export interface ReportStreamHealth {
  title?: string;
  encoder?: string;
  ffmpeg_alive?: boolean;
  viewers?: number | null;
  server_position_seconds?: number | null;
  segments_on_disk?: number | null;
  paused?: boolean;
}

/** A watcher-agent verdict. Historically written as either a bare string or a
 *  structured object, and both shapes are still on disk. */
export type ReportTriage =
  | string
  | {
      cause?: unknown;
      confidence?: unknown;
      reasoning?: unknown;
      suggested_action?: unknown;
    };

export interface Report {
  id: number;
  ts?: number;
  status?: string;
  message?: string;
  viewer_label?: string;
  ip?: string;
  user_agent?: string;
  client?: ReportClient;
  stream_health?: ReportStreamHealth;
  triage?: ReportTriage | null;
}

@Injectable({ providedIn: 'root' })
export class ReportsService {
  private readonly http = inject(HttpClient);

  /** Newest first — the endpoint already reverses. Errors keep the last render
   *  on screen rather than blanking the panel, as the old `catch {}` did. */
  list(): Observable<Report[]> {
    return this.http
      .get<Report[]>('/admin/api/reports')
      .pipe(catchError(() => of([])));
  }

  /* The mutations go through fetch rather than HttpClient so they compose with
   * the shared actions() helper, which works in terms of a Response. */

  setHandled(id: number, unhandle: boolean): Promise<Response> {
    return fetch(`/admin/api/reports/${id}/handled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unhandle }),
    });
  }

  remove(id: number): Promise<Response> {
    return fetch(`/admin/api/reports/${id}`, { method: 'DELETE' });
  }

  clearAll(): Promise<Response> {
    return fetch('/admin/api/reports', { method: 'DELETE' });
  }
}
