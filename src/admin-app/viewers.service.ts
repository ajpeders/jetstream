import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, of } from 'rxjs';
import { catchError } from 'rxjs/operators';

/** One currently-active viewer, as /admin/api/viewers returns it. */
export interface Viewer {
  ip: string;
  loc: string;
  last_seen: number;
  who: string | null;
}

/** One line of the on-disk viewer log. Every field is best-effort — the log
 *  is JSONL written across app versions, so old rows can be missing keys. */
export interface ViewerHistoryRow {
  ts?: number;
  ip?: string;
  loc?: string;
  who?: string;
}

@Injectable({ providedIn: 'root' })
export class ViewersService {
  private readonly http = inject(HttpClient);

  /** The 5s poll. Errors collapse to the last-known-empty list rather than
   *  killing the stream — matching the old `catch {}`, which deliberately
   *  left the panel alone through a transient blip. */
  list(): Observable<Viewer[]> {
    return this.http
      .get<Viewer[]>('/admin/api/viewers')
      .pipe(catchError(() => of([])));
  }

  /** On-demand, and allowed to surface its failure: this one is behind a
   *  button press, so a silent empty panel would read as "no history". */
  history(): Observable<ViewerHistoryRow[]> {
    return this.http.get<ViewerHistoryRow[]>('/admin/api/viewers/history');
  }
}
