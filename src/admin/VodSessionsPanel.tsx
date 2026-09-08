import { useEffect, useMemo } from 'react';

import { fmtAgo, formatTime, publishRefresh, useActions, usePolled } from '@/lib';
import { killVodSession, listVodSessions, type VodSession, type VodSessionFeed } from './services';

export function VodSessionsPanel() {
  const act = useActions();
  const poll = usePolled<VodSessionFeed>(listVodSessions, {
    initial: { sessions: [], cap: null },
    intervalMs: 10000,
  });
  const { sessions, cap } = poll.data;
  const now = useMemo(() => Date.now() / 1000, [poll.data]);

  useEffect(() => {
    publishRefresh('vodSessions', poll.refresh);
    return () => {
      if (window.jetstream?.vodSessions === poll.refresh) delete window.jetstream.vodSessions;
    };
  }, [poll.refresh]);

  const kill = async (s: VodSession) => {
    if (!confirm("Kill this stream? The viewer's playback stops immediately.")) return;
    await act.run(`kill:${s.session_id}`, () => killVodSession(s.session_id), {
      ok: 'Stream killed',
      fail: 'Kill failed',
    });
    poll.refresh();
    window.jetstream?.users?.();
  };

  return (
    <>
      <div className="head">
        <h2>Active VOD streams</h2>
        <span className="badge">{sessions.length} of {cap ?? '?'} streams</span>
      </div>
      <div className="tbl-wrap">
        {sessions.length ? (
          <table>
            <thead>
              <tr><th>User</th><th>Title</th><th>Position</th><th>Started</th><th>Last access</th><th></th></tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.session_id}>
                  <td className="vwho">{s.username || '?'}</td>
                  <td title={s.title || ''}>{s.title || '?'}</td>
                  <td className="vtime">{formatTime(s.start_offset)}</td>
                  <td className="vtime">{since(s.started_at, now)}</td>
                  <td className="vtime">{since(s.last_access, now)}</td>
                  <td><button type="button" className="danger" disabled={act.busy(`kill:${s.session_id}`)} onClick={() => kill(s)}>Kill</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="empty-v">No active streams.</span>}
      </div>
    </>
  );
}

function since(ts: number | null, now: number): string {
  return fmtAgo(now - (ts ?? now));
}
