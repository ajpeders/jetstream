import { useCallback, useMemo, useState } from 'react';

import { fmtAgo, usePolled } from '@/lib';
import { listViewers, viewerHistory, type Viewer, type ViewerHistoryRow } from './services';

export function ViewersPanel() {
  const { data: viewers } = usePolled<Viewer[]>(listViewers, { initial: [] });
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<ViewerHistoryRow[]>([]);
  const [historyState, setHistoryState] = useState<'loading' | 'error' | 'ready'>('ready');
  const [historyError, setHistoryError] = useState('');
  const now = useMemo(() => Date.now() / 1000, [viewers]);

  const toggleHistory = useCallback(async () => {
    const open = !historyOpen;
    setHistoryOpen(open);
    if (!open) return;
    setHistoryState('loading');
    try {
      setHistory([...await viewerHistory()].reverse());
      setHistoryState('ready');
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : String(e));
      setHistoryState('error');
    }
  }, [historyOpen]);

  return (
    <>
      <div className="head">
        <h2>Viewers</h2>
        <span className="badge">{viewers.length}</span>
        <button type="button" onClick={toggleHistory}>
          {historyOpen ? 'Hide history' : 'History'}
        </button>
      </div>
      <ul className="viewers">
        {viewers.length ? (
          viewers.map((v) => (
            <li key={v.ip}>
              <span className={`who${v.who ? '' : ' anon'}`}>{v.who || 'anonymous'}</span>
              <span className="ip">{v.ip}</span>
              <span className="loc">{v.loc || '?'}</span>
              <span className="ago">{fmtAgo(now - v.last_seen)}</span>
            </li>
          ))
        ) : (
          <li className="empty">no active viewers</li>
        )}
      </ul>
      {historyOpen && (
        <div className="history">
          {historyState === 'loading' ? (
            <div className="muted">loading...</div>
          ) : historyState === 'error' ? (
            <div className="err">Failed: {historyError}</div>
          ) : history.length ? (
            history.map((r, i) => (
              <div className="row" key={`${r.ts ?? ''}:${r.ip ?? ''}:${i}`}>
                <span className="ts">{new Date((r.ts || 0) * 1000).toLocaleString()}</span>
                <span className="ip">{r.ip || '?'}</span>
                <span className="loc">{r.loc || '?'}</span>
                <span className="who">{r.who || 'anonymous'}</span>
              </div>
            ))
          ) : (
            <div className="muted">no history yet</div>
          )}
        </div>
      )}
    </>
  );
}
