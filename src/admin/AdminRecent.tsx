import { useCallback, useEffect, useState } from 'react';

import { fmtAgo, PosterThumb } from '@/lib';
import { adminApiBase, type RecentItem } from './services';

export function AdminRecent() {
  const api = adminApiBase();
  const [items, setItems] = useState<RecentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<number | null>(null);

  const loadRecent = useCallback(async () => {
    try {
      const response = await fetch(`${api}/recent`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.json();
      setItems(Array.isArray(body) ? body : []);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void loadRecent();
    const timer = setInterval(() => void loadRecent(), 15000);
    return () => clearInterval(timer);
  }, [loadRecent]);

  const requeue = async (index: number) => {
    setPending(index);
    try {
      const response = await fetch(`${api}/recent/${index}/queue`, { method: 'POST' });
      const payload: { error?: string; first_title?: string } = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      notify(`Requeued: ${payload.first_title || titleOf(items[index] ?? {})}`);
      await window.jetstreamQueue?.reload?.();
    } catch (err) {
      notify(`Requeue failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setPending(null);
    }
  };

  return (
    <section id="recent-panel">
      <div className="head">
        <div>
          <h2>Recently played</h2>
          <span className="recent-subtitle">{items.length ? `${items.length} saved` : 'Nothing yet'}</span>
        </div>
        <button type="button" onClick={loadRecent} title="Refresh recently played">Refresh</button>
      </div>
      <ul id="recent-list" aria-busy={loading}>
        {loading ? <li className="recent-state">Loading recently played...</li> :
          error ? <li className="recent-state error-state"><span>Failed to load: {error}</span><button type="button" onClick={loadRecent}>Retry</button></li> :
            !items.length ? <li className="recent-state"><strong>No recently played items</strong><span>Played files and URLs will show up here for quick requeue.</span></li> :
              items.slice(0, 8).map((item, index) => {
                const source = item.source || {};
                return (
                  <li className="recent-row" key={`${source.ref ?? ''}:${item.ts ?? ''}:${index}`}>
                    <span className="recent-thumb">{source.type === 'file' && source.ref ? <PosterThumb src={`/poster?path=${encodeURIComponent(source.ref)}`} /> : <span aria-hidden="true">U</span>}</span>
                    <span className="recent-main">
                      <span className="recent-title" title={source.ref}>{titleOf(item)}</span>
                      <span className="recent-meta">
                        <span>{sourceOf(item)}</span>
                        {source.duration ? <span>{fmtDur(source.duration)}</span> : null}
                        <span>{item.ts ? fmtAgo(Math.max(0, Math.floor(Date.now() / 1000 - item.ts))) : ''}</span>
                      </span>
                    </span>
                    <button type="button" onClick={() => requeue(index)} disabled={pending === index} title={`Requeue ${titleOf(item)}`}>{pending === index ? 'Adding...' : 'Requeue'}</button>
                  </li>
                );
              })}
      </ul>
    </section>
  );
}

function titleOf(item: RecentItem): string {
  const source = item.source || {};
  return source.title || source.ref || 'Untitled';
}

function sourceOf(item: RecentItem): string {
  const source = item.source || {};
  if (source.type === 'file') return 'File';
  if (source.type === 'url') return source.is_live ? 'Live URL' : 'URL';
  return 'Source';
}

function fmtDur(sec: number | null | undefined): string {
  if (!sec || !Number.isFinite(sec) || sec < 0) return '';
  const t = Math.floor(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function notify(message: string): void {
  (window.jetstreamShowToast || window.showToast)?.(message);
}
