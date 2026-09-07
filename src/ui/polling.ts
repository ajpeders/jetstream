import { Signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Observable, Subject, merge, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';

export interface PollingStream<T> {
  /** Emits each tick's result. Cold until subscribed. */
  readonly stream$: Observable<T>;
  /** Fetch now instead of waiting out the interval. */
  refresh(): void;
}

/**
 * The timer + manual-kick shape every polling panel repeats.
 *
 * `timer(0, ms)` fires immediately, so there is no separate priming call — the
 * hand-written pollers all had a stray `poll()` after their setInterval.
 * `switchMap` means a slow tick is abandoned when the next one starts, so a
 * stalled request can never deliver stale data over fresh.
 *
 * Most panels want `polled()` below. Reach for this directly only when the
 * result has to be folded into existing state rather than replacing it — the
 * chat panel appends deltas and syncs deletions, which a signal of "the latest
 * response" cannot express.
 */
export function pollingStream<T>(
  source: () => Observable<T>,
  opts: { intervalMs?: number } = {},
): PollingStream<T> {
  const kick = new Subject<void>();
  return {
    stream$: merge(timer(0, opts.intervalMs ?? 5000), kick).pipe(switchMap(source)),
    refresh: () => kick.next(),
  };
}

export interface Polled<T> {
  /** Latest value. Starts at `initial` and updates on every tick. */
  readonly value: Signal<T>;
  /** Re-poll immediately instead of waiting out the interval. */
  refresh(): void;
}

/**
 * A polling stream surfaced as a signal — the common case, where each tick's
 * response simply replaces what is on screen.
 *
 * Must be called from an injection context (a field initializer or a
 * constructor), because `toSignal` registers a cleanup on the injector.
 */
export function polled<T>(
  source: () => Observable<T>,
  opts: { initial: T; intervalMs?: number },
): Polled<T> {
  const poll = pollingStream(source, opts);
  return {
    value: toSignal(poll.stream$, { initialValue: opts.initial }),
    refresh: poll.refresh,
  };
}
