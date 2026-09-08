import { DragEvent, useCallback, useEffect, useState } from 'react';

import { PosterThumb } from '@/lib';
import { adminApiBase, type QueueItem } from './services';

export function AdminQueue() {
  const api = adminApiBase();
  const [items, setItems] = useState<QueueItem[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [dragSrcIndex, setDragSrcIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);

  const loadQueue = useCallback(async () => {
    try {
      const response = await fetch(`${api}/queue`);
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
    window.jetstreamQueue = { reload: loadQueue };
    window.loadQueue = loadQueue;
    void loadQueue();
    const timer = setInterval(() => void loadQueue(), 5000);
    return () => {
      clearInterval(timer);
      if (window.jetstreamQueue?.reload === loadQueue) delete window.jetstreamQueue;
      if (window.loadQueue === loadQueue) delete window.loadQueue;
    };
  }, [loadQueue]);

  const removeItem = async (index: number) => {
    try {
      await fetch(`${api}/queue/${index}`, { method: 'DELETE' });
      await loadQueue();
    } catch (err) {
      notify(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const moveItem = async (index: number, body: unknown) => {
    try {
      await fetch(`${api}/queue/${index}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      await loadQueue();
    } catch (err) {
      notify(`Move failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const clearQueue = async () => {
    if (!confirm('Clear the entire queue?')) return;
    try {
      await fetch(`${api}/queue/clear`, { method: 'POST' });
      notify('Queue cleared');
      await loadQueue();
    } catch (err) {
      notify(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const shuffleQueue = async () => {
    try {
      const response = await fetch(`${api}/queue/shuffle`, { method: 'POST' });
      const payload: { error?: string; queue_length?: number } = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const n = payload.queue_length ?? 0;
      notify(`Shuffled ${n} item${n === 1 ? '' : 's'}`);
      await loadQueue();
    } catch (err) {
      notify(`Shuffle failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const onDragStart = (event: DragEvent, index: number) => {
    if ((event.target as HTMLElement).closest('button')) {
      event.preventDefault();
      return;
    }
    setDragSrcIndex(index);
    event.dataTransfer.effectAllowed = 'move';
    try {
      event.dataTransfer.setData('text/plain', String(index));
    } catch {
      /* Firefox only needs some payload. */
    }
  };
  const onDrop = (event: DragEvent, index: number) => {
    event.preventDefault();
    const src = dragSrcIndex;
    setDragSrcIndex(null);
    setDragOverIndex(null);
    if (src == null || src === index) return;
    void moveItem(src, { to: index });
  };

  return (
    <div id="queue-panel">
      <div className="head">
        <div className="queue-heading">
          <h2>Up next</h2>
          <span className="queue-subtitle">{items.length ? `${items.length} queued` : 'Ready for media'}</span>
        </div>
        <span className="badge" id="queue-count" aria-label={`${items.length} queue items`}>{items.length}</span>
        <button id="queue-shuffle" type="button" onClick={shuffleQueue} disabled={!items.length} title="Shuffle the queue">Shuffle</button>
        <button id="queue-clear" type="button" onClick={clearQueue} disabled={!items.length} title="Clear every queued item">Clear</button>
      </div>
      <ul id="queue-list" aria-busy={loading}>
        {loading ? <QueueState icon="" title="Loading queue" text="Checking what is coming up next." /> :
          error ? <QueueState icon="!" title="Queue unavailable" text={`Failed to load queue: ${error}`} error retry={loadQueue} /> :
            !items.length ? <QueueState icon="+" title="Queue is empty" text="Add a file from the library or paste a URL above. New items will play after the current stream." /> :
              items.map((item, index) => {
                const chip = chipForItem(item);
                const hasSubs = item.subtitle_idx !== null && item.subtitle_idx !== undefined;
                const label = itemLabel(item);
                return (
                  <li
                    className={`queue-row${dragSrcIndex === index ? ' dragging' : ''}${dragOverIndex === index && dragSrcIndex !== index ? ' drag-over' : ''}`}
                    draggable
                    aria-label={`Queue item ${index + 1}: ${label}`}
                    key={`${item.ref ?? ''}:${index}`}
                    onDragStart={(event) => onDragStart(event, index)}
                    onDragEnd={() => { setDragSrcIndex(null); setDragOverIndex(null); }}
                    onDragEnter={() => setDragOverIndex(index)}
                    onDragOver={(event) => event.preventDefault()}
                    onDragLeave={() => { if (dragOverIndex === index) setDragOverIndex(null); }}
                    onDrop={(event) => onDrop(event, index)}
                  >
                    <span className="drag-handle" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
                    <span className="pos" aria-label={`Position ${index + 1}`}>{index + 1}</span>
                    <span className="qthumb">{item.type === 'file' && item.ref ? <PosterThumb src={`/poster?path=${encodeURIComponent(item.ref)}`} /> : <span aria-hidden="true">{chip.text.slice(0, 1)}</span>}</span>
                    <span className="qmain">
                      <span className="qtitle" title={item.ref}>{label}</span>
                      <span className="qmeta">
                        <span className={`qchip ${chip.cls}`} title={`Source: ${chip.text.toLowerCase()}`}>{chip.text}</span>
                        {hasSubs && <span className="qcc" title="Subtitles will be burned in">CC</span>}
                        {item.ref && item.title && item.ref !== item.title && <span className="qref" title={item.ref}>{item.ref}</span>}
                      </span>
                    </span>
                    {item.is_live ? <span className="qdur live">LIVE</span> : <span className="qdur">{fmtDur(item.duration)}</span>}
                    <span className="queue-actions" aria-label={`Actions for ${label}`}>
                      <button type="button" className="qbtn" disabled={index === 0} title="Move this item up" aria-label={`Move ${label} up`} onClick={() => moveItem(index, { direction: 'up' })}><UpIcon /></button>
                      <button type="button" className="qbtn" disabled={index === items.length - 1} title="Move this item down" aria-label={`Move ${label} down`} onClick={() => moveItem(index, { direction: 'down' })}><DownIcon /></button>
                      <button type="button" className="qbtn danger" title="Remove this item from the queue" aria-label={`Remove ${label} from the queue`} onClick={() => removeItem(index)}><XIcon /></button>
                    </span>
                  </li>
                );
              })}
      </ul>
    </div>
  );
}

function QueueState({ icon, title, text, error, retry }: { icon: string; title: string; text: string; error?: boolean; retry?: () => void }) {
  return (
    <li className={`empty-q queue-state${error ? ' error-state' : ''}`} aria-live="polite">
      <span className="state-icon" aria-hidden="true">{icon}</span>
      <span><strong>{title}</strong><small>{text}</small></span>
      {retry && <button type="button" onClick={retry} title="Retry loading the queue">Retry</button>}
    </li>
  );
}

function chipForItem(item: QueueItem): { cls: string; text: string } {
  if (item.type === 'file') return { cls: '', text: 'FILE' };
  const ref = item.ref || '';
  if (/^https?:\/\/(www\.|m\.)?(youtube\.com|youtu\.be)\b/i.test(ref)) return { cls: 'yt', text: 'YOUTUBE' };
  return { cls: 'url', text: 'URL' };
}

function itemLabel(item: QueueItem): string {
  return item.title || item.ref || 'Untitled item';
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

function UpIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 14l6-6 6 6" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

function DownIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 10l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

function XIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" /></svg>;
}
