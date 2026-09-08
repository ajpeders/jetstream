import { useCallback, useState } from 'react';

/**
 * A collapse flag that survives a reload, matching the `jetstream_*_collapsed`
 * convention the hand-written panels established: "1" collapsed, "0" expanded,
 * and the `defaultCollapsed` fallback when the key was never written.
 *
 * localStorage throws outright in some contexts (private windows, embedded
 * webviews, browsers set to block site data), so every access is guarded — a
 * panel that cannot persist its state must still open and close.
 */
export function useCollapse(
  key: string,
  defaultCollapsed = true,
): [boolean, (next: boolean) => void] {
  const [collapsed, setState] = useState(() => read(key, defaultCollapsed));
  const set = useCallback(
    (next: boolean) => {
      setState(next);
      try {
        localStorage.setItem(key, next ? '1' : '0');
      } catch {
        /* storage unavailable — in-memory only for this session */
      }
    },
    [key],
  );
  return [collapsed, set];
}

function read(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return raw !== '0';
  } catch {
    return fallback;
  }
}
