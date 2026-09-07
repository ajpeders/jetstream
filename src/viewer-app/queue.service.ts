import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

/** One "up next" entry. /api/queue is deliberately read-only for viewers —
 *  adding and reordering stay on the admin/friend control surface. `ref` is
 *  present only for FILE items; URL and yt-dlp items have no useful cover art. */
export interface QueueItem {
  title: string | null;
  type: string | null;
  duration: number | null;
  is_live: boolean;
  ref: string | null;
}

@Injectable({ providedIn: 'root' })
export class QueueService {
  private readonly http = inject(HttpClient);

  /** Swallows failures the way the old `catch {}` did: a transient blip should
   *  leave the last-rendered queue on screen, not blank it. */
  list(): Observable<QueueItem[]> {
    return this.http.get<{ queue?: QueueItem[] }>('/api/queue').pipe(
      map((r) => r.queue ?? []),
      catchError(() => of([])),
    );
  }
}
