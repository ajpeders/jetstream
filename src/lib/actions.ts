import { useCallback, useMemo, useState } from 'react';

declare global {
  interface Window {
    /** admin.html's toast helper. Still plain inline script, so it is reached
     *  through the global rather than imported. */
    showToast?: (msg: string) => void;
  }
}

/** Surface a message in the page's toast strip, if the host page has one.
 *  An empty message is how a caller says "this one is silent" — a vote toggle
 *  should disable its button while in flight without announcing itself. */
export function toast(message: string): void {
  if (!message) return;
  window.showToast?.(message);
}

export interface Actions {
  /** True while the action under `key` is in flight — bind it to `disabled`. */
  busy(key: string): boolean;
  /** True while any action is in flight. */
  anyBusy(): boolean;
  /**
   * Run a mutation with the bookkeeping every panel would otherwise repeat:
   * mark the key busy so its button disables, toast success or failure, always
   * clear the key, and hand back whether it worked so the caller can refetch.
   *
   * `key` scopes the busy flag — use a per-row id ("delete:12") so one row's
   * pending delete doesn't disable every other row's buttons.
   */
  run(
    key: string,
    work: () => Promise<Response>,
    messages: { ok: string | ((body: unknown) => string); fail: string },
  ): Promise<boolean>;
}

export function useActions(): Actions {
  const [inFlight, setInFlight] = useState<ReadonlySet<string>>(() => new Set());

  const mark = useCallback((key: string, on: boolean) => {
    setInFlight((s) => {
      const next = new Set(s);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  const run = useCallback<Actions['run']>(
    async (key, work, messages) => {
      mark(key, true);
      try {
        const res = await work();
        if (!res.ok) {
          toast(messages.fail);
          return false;
        }
        if (typeof messages.ok === 'function') {
          // Some endpoints report what they did (how many rows were cleared),
          // and a body that isn't JSON must not turn a success into a failure.
          const body: unknown = await res.json().catch(() => ({}));
          toast(messages.ok(body));
        } else {
          toast(messages.ok);
        }
        return true;
      } catch (e: unknown) {
        toast(`Failed: ${e instanceof Error ? e.message : String(e)}`);
        return false;
      } finally {
        mark(key, false);
      }
    },
    [mark],
  );

  return useMemo(
    () => ({
      busy: (key: string) => inFlight.has(key),
      anyBusy: () => inFlight.size > 0,
      run,
    }),
    [inFlight, run],
  );
}
