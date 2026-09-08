import { PosterThumb, useActions, usePolled } from '@/lib';
import {
  approveQueueRequest,
  clearQueueRequests,
  denyQueueRequest,
  listQueueRequests,
  type QueueRequest,
} from './services';

declare global {
  interface Window {
    loadQueue?: () => void;
    jetstreamQueue?: { reload?: () => Promise<void> | void };
  }
}

export function QueueRequestsPanel() {
  const act = useActions();
  const poll = usePolled<QueueRequest[]>(listQueueRequests, { initial: [] });
  const items = poll.data;

  const approve = async (r: QueueRequest) => {
    const ok = await act.run(`approve:${r.id}`, () => approveQueueRequest(r.id), {
      ok: 'Added to queue',
      fail: 'Approve failed',
    });
    poll.refresh();
    if (ok) {
      window.loadQueue?.();
      await window.jetstreamQueue?.reload?.();
    }
  };
  const deny = async (r: QueueRequest) => {
    await act.run(`deny:${r.id}`, () => denyQueueRequest(r.id), {
      ok: 'Request denied',
      fail: 'Deny failed',
    });
    poll.refresh();
  };
  const clearAll = async () => {
    if (!confirm('Clear all pending requests?')) return;
    await act.run('clear', clearQueueRequests, {
      ok: (body) => {
        const n = (body as { cleared?: number }).cleared;
        return `Cleared ${n ?? ''} request${n === 1 ? '' : 's'}`.trim();
      },
      fail: 'Clear failed',
    });
    poll.refresh();
  };

  return (
    <>
      <div className="head">
        <h2>Requests</h2>
        <span className={`badge${items.length > 0 ? ' waiting' : ''}`}>{items.length}</span>
        {items.length > 0 && <button id="requests-clear" type="button" disabled={act.busy('clear')} onClick={clearAll} style={clearButtonStyle}>Clear all</button>}
      </div>
      <ul>
        {items.length ? items.map((r) => (
          <li key={r.id}>
            <span className="rthumb"><PosterThumb src={posterFor(r)} /></span>
            <span className="rwho">{r.requester || 'anonymous'}</span>
            <span className="rtitle">{label(r)}</span>
            <button className="approve" type="button" disabled={act.busy(`approve:${r.id}`)} onClick={() => approve(r)}>Add</button>
            <button type="button" disabled={act.busy(`deny:${r.id}`)} onClick={() => deny(r)}>Deny</button>
          </li>
        )) : <li className="empty-r">No pending requests.</li>}
      </ul>
    </>
  );
}

function label(r: QueueRequest): string {
  return r.title || (r.path ? r.path.split('/').pop() ?? '' : '');
}

function posterFor(r: QueueRequest): string | null {
  return r.path ? `/poster?path=${encodeURIComponent(r.path)}` : null;
}

const clearButtonStyle = { marginLeft: 'auto', fontSize: '0.75rem', padding: '0.2rem 0.55rem' };
