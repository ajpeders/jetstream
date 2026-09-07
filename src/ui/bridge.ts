declare global {
  interface Window {
    /** Refresh hooks the Angular panels publish for the page's remaining
     *  inline script. Absent until a panel bootstraps, so callers use `?.`. */
    jetstream?: Record<string, () => void>;
  }
}

/**
 * Publish a "re-poll now" hook under `window.jetstream`.
 *
 * A page is part hand-written script and part Angular during the port, and the
 * two halves have moments where one must nudge the other: approving a request
 * should show up in the queue immediately, not a poll-tick later. The inline
 * script can't import from the bundle, so the bundle exposes a named hook and
 * the page calls `window.jetstream?.<name>?.()`.
 *
 * Every one of these is a seam that disappears when the calling code is ported
 * too — if a name outlives the inline caller, delete it.
 */
export function publishRefresh(name: string, fn: () => void): void {
  (window.jetstream ??= {})[name] = fn;
}
