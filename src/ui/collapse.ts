import { WritableSignal, effect, signal } from '@angular/core';

/**
 * A collapse flag that survives a reload, matching the `jetstream_*_collapsed`
 * convention the hand-written panels established: "1" collapsed, "0" expanded,
 * and the `defaultCollapsed` fallback when the key was never written.
 *
 * localStorage throws outright in some contexts (private windows, embedded
 * webviews, browsers set to block site data), so every access is guarded — a
 * panel that cannot persist its state must still open and close.
 *
 * Must be called from an injection context: the write-back is an effect.
 */
export function collapseState(
  key: string,
  defaultCollapsed = true,
): WritableSignal<boolean> {
  const state = signal(read(key, defaultCollapsed));
  effect(() => {
    const c = state();
    try {
      localStorage.setItem(key, c ? '1' : '0');
    } catch {
      /* storage unavailable — in-memory only for this session */
    }
  });
  return state;
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
