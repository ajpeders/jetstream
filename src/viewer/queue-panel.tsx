import { useCallback } from 'react';

import {
  IslandProps,
  PosterThumb,
  formatTime,
  useCollapse,
  useHostClass,
  usePolled,
  usePublishRefresh,
} from '@/lib';

import { QueueItem, listQueue } from './queue';

/** Mounts into the page's <section id="queue-panel" jet-queue-panel>, so every
 *  rule in viewer.css keeps matching and .collapsed stays the styling hook it
 *  always was. */
export function QueuePanel({ host }: IslandProps) {
  const { data: items, refresh } = usePolled(listQueue, { initial: [] as QueueItem[] });
  /** Starts collapsed unless a previous visit expanded it — the same key the
   *  hand-written panel used, so a returning viewer sees no change. */
  const [collapsed, setCollapsed] = useCollapse('jetstream_queue_collapsed');
  useHostClass(host, 'collapsed', collapsed);

  // Approving a request should surface it in the queue at once rather than up
  // to a tick later — the request-list island calls this.
  usePublishRefresh('queue', useCallback(() => void refresh(), [refresh]));

  const n = items.length;
  return (
    <>
      <button
        id="queue-head"
        type="button"
        aria-expanded={!collapsed}
        aria-controls="queue-list"
        onClick={() => setCollapsed(!collapsed)}
      >
        <span className="qh-label">queue</span>
        <span id="queue-count">{`${n} ${n === 1 ? 'item' : 'items'}`}</span>
        <span className="toggle" aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
      </button>

      <ol id="queue-list">
        {items.length === 0 && <li className="empty">Nothing queued yet.</li>}
        {items.map((it, i) => (
          <li key={i}>
            <span className="qpos">{i + 1}</span>
            <span className="qthumb"><PosterThumb src={posterFor(it)} /></span>
            <span className="qtitle">{it.title || '(untitled)'}</span>
            <span className="qdur">{it.is_live ? 'LIVE' : formatTime(it.duration)}</span>
          </li>
        ))}
      </ol>
    </>
  );
}

function posterFor(it: QueueItem): string | null {
  return it.type === 'file' && it.ref ? `/poster?path=${encodeURIComponent(it.ref)}` : null;
}
