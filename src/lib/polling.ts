import { useCallback, useEffect, useRef, useState } from 'react';

export interface Polled<T> {
  /** Latest value. Starts at `initial` and updates on every successful tick. */
  readonly data: T;
  /** Re-poll immediately instead of waiting out the interval. */
  refresh(): void;
}

/**
 * The timer + manual-kick shape every polling panel repeats.
 *
 * The first tick fires immediately, so there is no separate priming call — the
 * hand-written pollers all had a stray `poll()` after their setInterval. A
 * stale response is dropped when a newer tick has since started, so a stalled
 * request can never deliver old data over fresh. A tick that resolves to
 * `undefined` contributes nothing and the previous value stays on screen —
 * that is how a fetcher says "transient failure, keep what you have" instead
 * of blanking the panel on a blip.
 *
 * Pass `fetcher` through `useCallback` (or define it outside the component);
 * a new identity restarts the poll.
 */
export function usePolled<T>(
  fetcher: () => Promise<T | undefined>,
  opts: { initial: T; intervalMs?: number },
): Polled<T> {
  const [data, setData] = useState<T>(opts.initial);
  const seq = useRef(0);
  const intervalMs = opts.intervalMs ?? 5000;

  const tick = useCallback(async () => {
    const mine = ++seq.current;
    let result: T | undefined;
    try {
      result = await fetcher();
    } catch {
      return;
    }
    if (mine !== seq.current || result === undefined) return;
    setData(result);
  }, [fetcher]);

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => {
      clearInterval(id);
      seq.current++; // invalidate any in-flight tick from this run
    };
  }, [tick, intervalMs]);

  return { data, refresh: tick };
}

/**
 * Fetch JSON, resolving to `undefined` on any failure so `usePolled` keeps its
 * last good value. The common fetcher shape for read-only panels.
 */
export async function getJson<T>(url: string): Promise<T | undefined> {
  try {
    const r = await fetch(url);
    if (!r.ok) return undefined;
    return (await r.json()) as T;
  } catch {
    return undefined;
  }
}

/**
 * The polling shape for panels that fold each tick into existing state rather
 * than replacing it — the chat panel appends deltas and syncs deletions, which
 * "the latest response" cannot express. `onTick` receives every successful
 * result; a tick that resolves to `undefined` or throws is skipped. Returns
 * `refresh` to fetch now instead of waiting out the interval.
 *
 * Pass both callbacks through `useCallback`; a new identity restarts the poll.
 */
export function usePollTick<T>(
  fetcher: () => Promise<T | undefined>,
  onTick: (result: T) => void,
  opts: { intervalMs?: number } = {},
): () => void {
  const seq = useRef(0);
  const intervalMs = opts.intervalMs ?? 5000;

  const tick = useCallback(async () => {
    const mine = ++seq.current;
    let result: T | undefined;
    try {
      result = await fetcher();
    } catch {
      return;
    }
    if (mine !== seq.current || result === undefined) return;
    onTick(result);
  }, [fetcher, onTick]);

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => {
      clearInterval(id);
      seq.current++;
    };
  }, [tick, intervalMs]);

  return tick;
}
