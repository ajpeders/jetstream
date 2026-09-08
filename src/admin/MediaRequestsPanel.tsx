import { useMemo, useState } from 'react';

import { fmtAgo, useActions, usePolled } from '@/lib';
import {
  approveMediaRequest,
  listMediaRequests,
  rejectMediaRequest,
  type MediaRequest,
} from './services';

interface MetaBit {
  k: string;
  v: string;
}

export function MediaRequestsPanel() {
  const act = useActions();
  const poll = usePolled<MediaRequest[]>(listMediaRequests, { initial: [] });
  const [hideDecided, setHideDecided] = useState(false);
  const all = poll.data;
  const now = useMemo(() => Date.now() / 1000, [all]);
  const pendingCount = all.filter((r) => statusOf(r) === 'pending').length;
  const hasDecided = all.some((r) => statusOf(r) !== 'pending');
  const visible = hideDecided ? all.filter((r) => statusOf(r) === 'pending') : all;

  const approve = async (r: MediaRequest) => {
    await act.run(`approve:${r.id}`, () => approveMediaRequest(r.id), {
      ok: 'Media request approved',
      fail: 'Approve failed',
    });
    poll.refresh();
  };
  const reject = async (r: MediaRequest) => {
    const reason = prompt('Reject reason (optional)');
    if (reason === null) return;
    await act.run(`reject:${r.id}`, () => rejectMediaRequest(r.id, reason), {
      ok: 'Media request rejected',
      fail: 'Reject failed',
    });
    poll.refresh();
  };

  return (
    <>
      <div className="head">
        <h2>Media Requests</h2>
        <span className={`badge${pendingCount > 0 ? ' waiting' : ''}`}>{pendingCount}</span>
        {hasDecided && <button id="media-requests-clear" type="button" onClick={() => setHideDecided((v) => !v)} style={clearButtonStyle}>{hideDecided ? 'Show decided' : 'Hide decided'}</button>}
      </div>
      <ul>
        {visible.length ? visible.map((r) => {
          const status = statusOf(r);
          return (
            <li key={r.id} className={status !== 'pending' ? 'decided' : undefined}>
              <div className="mrtop">
                <span className={`mrstatus ${status}`}>{status}</span>
                <span className="mrtitle">{r.title || '(untitled)'}</span>
              </div>
              <div className="mrmeta">
                {meta(r, now).map((bit, i) => (
                  <span key={`${bit.k}:${i}`}>
                    <span className="k">{bit.k}</span> {bit.v}
                    {i < meta(r, now).length - 1 && <span className="sep">&nbsp;·&nbsp;</span>}
                  </span>
                ))}
              </div>
              <div className="mractions">
                {status === 'pending' && (
                  <>
                    <button className="approve" type="button" disabled={act.busy(`approve:${r.id}`)} onClick={() => approve(r)}>Approve</button>
                    <button type="button" disabled={act.busy(`reject:${r.id}`)} onClick={() => reject(r)}>Reject</button>
                  </>
                )}
              </div>
            </li>
          );
        }) : <li className="empty-r">{hideDecided ? 'No pending media requests.' : 'No media requests.'}</li>}
      </ul>
    </>
  );
}

function statusOf(r: MediaRequest): string {
  return r.status || 'pending';
}

function meta(r: MediaRequest, now: number): MetaBit[] {
  const bits: MetaBit[] = [{ k: 'by', v: r.username || 'unknown' }];
  if (r.kind) bits.push({ k: 'kind', v: r.kind });
  if (r.year) bits.push({ k: 'year', v: String(r.year) });
  if (r.tmdb_id != null) bits.push({ k: 'tmdb', v: String(r.tmdb_id) });
  if (r.tvdb_id != null) bits.push({ k: 'tvdb', v: String(r.tvdb_id) });
  if (Array.isArray(r.seasons) && r.seasons.length) bits.push({ k: 'seasons', v: r.seasons.join(', ') });
  if (r.reject_reason) bits.push({ k: 'reason', v: r.reject_reason });
  if (r.requested_at) bits.push({ k: 'requested', v: fmtAgo(now - r.requested_at) });
  return bits;
}

const clearButtonStyle = { marginLeft: 'auto', fontSize: '0.75rem', padding: '0.2rem 0.55rem' };
