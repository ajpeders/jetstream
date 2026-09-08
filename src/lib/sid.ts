/**
 * The per-browser client id. The server folds it into `_voter_key`, so it is
 * what makes a request vote survive a reload, and it is how a client
 * recognises the echo of its own reaction. Deliberately not a security
 * boundary — the server treats a missing sid as "fall back to the caller's IP".
 *
 * Read-or-create against the key the hand-written pages established, and
 * idempotent by design: viewer.html's inline script and the islands mint the
 * same value into the same key, so whichever runs first wins and the others
 * read it back. Module scripts are deferred, so the bundle boots after the
 * inline script and must agree with it.
 */
const KEY = 'jetstream_chatsid';

/** Memoised so a storage failure still yields one stable id per page load. */
let cached: string | null = null;

export function clientSid(): string {
  if (cached) return cached;
  try {
    const existing = localStorage.getItem(KEY);
    if (existing) return (cached = existing);
    const sid = mint();
    localStorage.setItem(KEY, sid);
    return (cached = sid);
  } catch {
    // Storage blocked (private window, embedded webview). Votes then last only
    // as long as the page does, which beats not being able to vote at all.
    return (cached = mint());
  }
}

/** 12 hex chars, matching the format the inline script has always generated. */
function mint(): string {
  return [...crypto.getRandomValues(new Uint8Array(6))]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
