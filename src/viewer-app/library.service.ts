import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { map } from 'rxjs/operators';

import { LibraryItem } from './library-paths';

/** What /api/library/search adds over a browse: the server caps results, and
 *  says so, rather than pretending the list is complete. */
export interface SearchResult {
  results: LibraryItem[];
  truncated: boolean;
}

/**
 * The viewer's read-only view of the library, plus the one write it can make.
 *
 * Both reads are deliberately unguarded against failure here — the panel needs
 * to tell "loading" from "failed" apart to render the right placeholder, so the
 * error reaches the component rather than being swallowed into an empty list.
 *
 * NOTE: these two endpoints list raw filenames. A catalog-backed `GET
 * /api/library` (titles, descriptions, grouped by series) is being designed in
 * another session; when it lands, this service is the seam that changes and the
 * panel above it should not have to.
 */
@Injectable({ providedIn: 'root' })
export class LibraryService {
  private readonly http = inject(HttpClient);

  /** Directory listing. Answers a bare array, roots when the path is empty. */
  browse(path: string): Observable<LibraryItem[]> {
    return this.http
      .get<LibraryItem[]>('/api/library/browse', { params: { path } })
      .pipe(map((items) => (Array.isArray(items) ? items : [])));
  }

  /** Recursive filename search across the viewer-visible roots. */
  search(q: string): Observable<SearchResult> {
    return this.http.get<Partial<SearchResult>>('/api/library/search', { params: { q } }).pipe(
      map((r) => ({
        results: Array.isArray(r.results) ? r.results : [],
        truncated: !!r.truncated,
      })),
    );
  }

  /**
   * Ask the host to queue a file. The reply distinguishes a genuine refusal
   * from a rate-limit, which the button surfaces differently, so the caller
   * gets the parsed body rather than a bare ok/not-ok.
   */
  async request(
    path: string,
    name: string,
    sid: string,
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      const res = await fetch('/api/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, name, sid }),
      });
      if (res.ok) return { ok: true };
      const body: { error?: string } = await res.json().catch(() => ({}));
      return { ok: false, error: body.error };
    } catch {
      return { ok: false };
    }
  }
}
