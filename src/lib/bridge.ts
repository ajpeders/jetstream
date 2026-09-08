import { useEffect } from 'react';

declare global {
  interface Window {
    /** Refresh hooks the islands publish for the page's remaining inline
     *  script. Absent until an island mounts, so callers use `?.`. */
    jetstream?: Record<string, () => void>;
  }
}

/**
 * Publish a "re-poll now" hook under `window.jetstream`.
 *
 * A page is part hand-written script and part React during the port, and the
 * two halves have moments where one must nudge the other: approving a request
 * should show up in the queue immediately, not a poll-tick later. The inline
 * script can't import from the bundle, so the bundle exposes a named hook and
 * the page calls `window.jetstream?.<name>?.()`.
 *
 * Islands use the same channel to nudge each other — each island is its own
 * React root, so there is no shared context to go through.
 *
 * Every one of these is a seam that disappears when the calling code is ported
 * too — if a name outlives its last caller, delete it.
 */
export function publishRefresh(name: string, fn: () => void): void {
  (window.jetstream ??= {})[name] = fn;
}

/** Publish `fn` under `name` for the lifetime of the component, and drop it
 *  on unmount so a stale closure can't be called after the island is gone. */
export function usePublishRefresh(name: string, fn: () => void): void {
  useEffect(() => {
    publishRefresh(name, fn);
    return () => {
      if (window.jetstream?.[name] === fn) delete window.jetstream[name];
    };
  }, [name, fn]);
}
