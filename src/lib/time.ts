/**
 * The two time formats this UI uses, in one place. Both were defined three
 * times over across viewer.html, admin.html and library.html; the thresholds
 * and padding here match those originals exactly so nothing reads differently
 * as pages move across.
 */

/** h:mm:ss. Empty string for null/NaN — callers render it into a slot that is
 *  allowed to be blank (a live item has no duration). */
export function formatTime(s: number | null | undefined): string {
  if (s == null || isNaN(s)) return '';
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

/** "45s ago" / "3m ago" / "2h ago" / "5d ago" from a seconds delta. */
export function fmtAgo(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
