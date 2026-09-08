import { useCallback } from 'react';

import {
  IslandProps,
  clientSid,
  useActions,
  useCollapse,
  useHostClass,
  usePolled,
  usePublishRefresh,
} from '@/lib';

import { MediaRequest, RequestFeed, approve, deny, listRequests, vote } from './requests';

const EMPTY: RequestFeed = { requests: [], can_manage: false };

/** Mounts into the page's <section id="reqlist-panel" jet-requests-panel>. */
export function RequestsPanel({ host }: IslandProps) {
  const act = useActions();
  const fetcher = useCallback(() => listRequests(clientSid()), []);
  const { data: feed, refresh } = usePolled(fetcher, { initial: EMPTY });
  const [collapsed, setCollapsed] = useCollapse('jetstream_reqlist_collapsed');
  useHostClass(host, 'collapsed', collapsed);

  // The library panel calls this after filing a request, so the new entry (or
  // its bumped vote count) shows up without waiting for the tick.
  usePublishRefresh('requests', useCallback(() => void refresh(), [refresh]));

  const onVote = async (r: MediaRequest) => {
    // Deliberately quiet: voting is high-frequency and its result is visible in
    // the count, so a toast per click would be noise.
    await act.run(`vote:${r.id}`, () => vote(r.id, clientSid()), { ok: '', fail: 'Vote failed' });
    void refresh();
  };
  const onApprove = async (r: MediaRequest) => {
    await act.run(`approve:${r.id}`, () => approve(r.id), { ok: 'Queued', fail: 'Approve failed' });
    void refresh();
    // Approving moves the item into the queue, which is a separate island.
    window.jetstream?.['queue']?.();
  };
  const onDeny = async (r: MediaRequest) => {
    await act.run(`deny:${r.id}`, () => deny(r.id), { ok: 'Request removed', fail: 'Deny failed' });
    void refresh();
  };

  const n = feed.requests.length;
  return (
    <>
      {/* A button, like #queue-head: the header is an interactive control, so a
          div leaves it unreachable by keyboard. viewer.css carries the chrome
          opt-out and the :focus-visible ring for this id. */}
      <button
        id="reqlist-head"
        type="button"
        aria-expanded={!collapsed}
        aria-controls="reqlist"
        onClick={() => setCollapsed(!collapsed)}
      >
        <span className="rh-label">requests</span>
        <span id="reqlist-count">{n ? `${n} ${n === 1 ? 'request' : 'requests'}` : ''}</span>
        <span className="toggle" aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
      </button>

      <ul id="reqlist">
        {n === 0 && (
          <li className="empty">No requests yet — find something in the library below and tap Request.</li>
        )}
        {feed.requests.map((r) => (
          <li key={r.id}>
            <button
              type="button"
              className={'req-vote' + (r.voted ? ' voted' : '')}
              title={r.voted ? 'Remove your vote' : 'Vote for this'}
              disabled={act.busy(`vote:${r.id}`)}
              onClick={() => void onVote(r)}
            >
              <span className="req-arrow">▲</span>
              <span className="req-n">{r.votes}</span>
            </button>

            <div className="req-meta">
              <div className="req-title">{r.title || '(untitled)'}</div>
              {r.requester && <div className="req-by">by {r.requester}</div>}
            </div>

            {feed.can_manage && (
              <div className="req-actions">
                <button type="button" className="req-ok" title="Approve & queue"
                        disabled={act.busy(`approve:${r.id}`)} onClick={() => void onApprove(r)}>
                  Queue
                </button>
                <button type="button" className="req-no" title="Deny"
                        disabled={act.busy(`deny:${r.id}`)} onClick={() => void onDeny(r)}>
                  ✕
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}
