import { getJson } from '@/lib';

/** One "up next" entry. /api/queue is deliberately read-only for viewers —
 *  adding and reordering stay on the admin/friend control surface. `ref` is
 *  present only for FILE items; URL and yt-dlp items have no useful cover art. */
export interface QueueItem {
  title: string | null;
  type: string | null;
  duration: number | null;
  is_live: boolean;
  ref: string | null;
}

/** Resolves `undefined` on failure so the poll keeps the last-rendered queue
 *  on screen rather than blanking it on a transient blip. */
export async function listQueue(): Promise<QueueItem[] | undefined> {
  const r = await getJson<{ queue?: QueueItem[] }>('/api/queue');
  return r ? (r.queue ?? []) : undefined;
}
