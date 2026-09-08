import { LibraryItem } from './library-paths';

/** What /api/library/search adds over a browse: the server caps results, and
 *  says so, rather than pretending the list is complete. */
export interface SearchResult {
  results: LibraryItem[];
  truncated: boolean;
}

/*
 * The viewer's read-only view of the library, plus the one write it can make.
 *
 * Both reads throw on failure on purpose — the panel needs to tell "loading"
 * from "failed" apart to render the right placeholder, so the error reaches
 * the component rather than being swallowed into an empty list.
 *
 * NOTE: these two endpoints list raw filenames. A catalog-backed `GET
 * /api/library` (titles, descriptions, grouped by series) is being designed in
 * another session; when it lands, this module is the seam that changes and the
 * panel above it should not have to.
 */

/** Directory listing. Answers a bare array, roots when the path is empty. */
export async function browse(path: string): Promise<LibraryItem[]> {
  const r = await fetch(`/api/library/browse?path=${encodeURIComponent(path)}`);
  if (!r.ok) throw new Error(String(r.status));
  const items: unknown = await r.json();
  return Array.isArray(items) ? (items as LibraryItem[]) : [];
}

/** Recursive filename search across the viewer-visible roots. */
export async function search(q: string): Promise<SearchResult> {
  const r = await fetch(`/api/library/search?q=${encodeURIComponent(q)}`);
  if (!r.ok) throw new Error(String(r.status));
  const body = (await r.json()) as Partial<SearchResult>;
  return {
    results: Array.isArray(body.results) ? body.results : [],
    truncated: !!body.truncated,
  };
}

/**
 * Ask the host to queue a file. The reply distinguishes a genuine refusal
 * from a rate-limit, which the button surfaces differently, so the caller
 * gets the parsed body rather than a bare ok/not-ok.
 */
export async function request(
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
