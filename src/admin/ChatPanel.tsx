import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { clientSid, useActions } from '@/lib';
import { deleteChatMessage, muteChatSid, recentChat, sendChat, type ChatMessage, type ChatPage } from './services';

const NAME_KEY = 'jetstream_chatname';
const COLLAPSE_KEY = 'jetstream_chat_collapsed';
const STICK_PX = 50;

export function ChatPanel() {
  const act = useActions();
  const isAdmin = !location.pathname.startsWith('/controls');
  const sid = useRef(clientSid()).current;
  const pane = useRef<HTMLDivElement>(null);
  const maxId = useRef(0);
  const stick = useRef(true);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [collapsed, setCollapsed] = useState(readBool(COLLAPSE_KEY, false));
  const [name, setName] = useState(readName);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [placeholder, setPlaceholder] = useState('Type a message...');

  const apply = useCallback((page: ChatPage) => {
    const el = pane.current;
    stick.current = !el || el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    if (typeof page.max_id === 'number') maxId.current = page.max_id;
    const incoming = Array.isArray(page.messages) ? page.messages : [];
    const deleted = Array.isArray(page.deleted_ids) ? new Set(page.deleted_ids) : null;
    if (!incoming.length && !deleted?.size) return;
    setMessages((cur) => {
      let next = incoming.length ? [...cur, ...incoming] : cur;
      if (deleted?.size) next = next.filter((m) => !deleted.has(m.id));
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    const page = await recentChat(maxId.current);
    if (page) apply(page);
  }, [apply]);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 2500);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0');
    } catch {
      /* storage blocked */
    }
    document.getElementById('chat-panel')?.classList.toggle('collapsed', collapsed);
  }, [collapsed]);

  useLayoutEffect(() => {
    if (stick.current && pane.current) pane.current.scrollTop = pane.current.scrollHeight;
  }, [messages]);

  const persistName = () => {
    try {
      localStorage.setItem(NAME_KEY, name.trim().slice(0, 24));
    } catch {
      /* storage blocked */
    }
  };
  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setSending(true);
    try {
      const r = await sendChat(text, sid, name.trim());
      if (r.ok) {
        setDraft('');
        await refresh();
      } else {
        const j: { error?: string; remaining?: number } = await r.json().catch(() => ({}));
        flashPlaceholder(rejectionText(j), setPlaceholder);
      }
    } finally {
      setSending(false);
    }
  };
  const remove = async (m: ChatMessage) => {
    const ok = await act.run(`del:${m.id}`, () => deleteChatMessage(m.id), { ok: '', fail: 'Delete failed' });
    if (ok) setMessages((cur) => cur.filter((x) => x.id !== m.id));
  };
  const mute = async (m: ChatMessage) => {
    const raw = prompt('Mute this sid for how many minutes? (0 to unmute)', '5');
    if (raw === null) return;
    const minutes = parseInt(raw, 10);
    if (!Number.isFinite(minutes) || minutes < 0) {
      (window.jetstreamShowToast || window.showToast)?.('Invalid duration');
      return;
    }
    await act.run(`mute:${m.id}`, () => muteChatSid(m.sid, minutes * 60), {
      ok: minutes ? `Muted ${minutes}m` : 'Unmuted',
      fail: 'Mute failed',
    });
  };

  return (
    <>
      <div id="chat-header" onClick={() => setCollapsed((v) => !v)}>
        <h2>Chat</h2>
        <span className="toggle">{collapsed ? '▸' : '▾'}</span>
      </div>
      <div id="chat-messages" ref={pane}>
        {messages.length ? messages.map((m) => (
          <div className={`chat-msg${m.sid === sid ? ' self' : ''}`} key={m.id}>
            <span className="chat-dot" style={{ background: chatColor(m.sid) }}></span>
            <span className="chat-name">{(m.name || 'anonymous') + ':'}</span>
            <span className="chat-text">{m.text}</span>
            {isAdmin && (
              <span className="mod-actions">
                <button type="button" title="Delete message" disabled={act.busy(`del:${m.id}`)} onClick={() => remove(m)}>✕</button>
                <button type="button" title="Mute this sid" disabled={act.busy(`mute:${m.id}`)} onClick={() => mute(m)}>mute</button>
              </span>
            )}
          </div>
        )) : <div className="empty">No messages yet.</div>}
      </div>
      <form id="chat-form" autoComplete="off" onSubmit={send}>
        <input id="chat-name-input" type="text" maxLength={24} placeholder="name" aria-label="Display name (optional, anonymous if blank)" value={name} onChange={(e) => setName(e.currentTarget.value)} onBlur={persistName} />
        <input id="chat-input" type="text" maxLength={500} placeholder={placeholder} disabled={sending} value={draft} onChange={(e) => setDraft(e.currentTarget.value)} />
        <button id="chat-send" type="submit" disabled={sending}>send</button>
      </form>
    </>
  );
}

function chatColor(sid: string): string {
  let h = 0;
  for (let i = 0; i < sid.length; i++) h = ((h << 5) - h + sid.charCodeAt(i)) | 0;
  return `hsl(${((h % 360) + 360) % 360}, 65%, 60%)`;
}

function readName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? '';
  } catch {
    return '';
  }
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : raw === '1' || raw === 'true';
  } catch {
    return fallback;
  }
}

function rejectionText(j: { error?: string; remaining?: number }): string {
  if (j.error === 'muted') return `muted (${Math.max(1, Math.ceil((j.remaining || 0) / 60))}m left)`;
  if (j.error === 'rate limited') return 'slow down — too many messages';
  return j.error || 'send failed';
}

function flashPlaceholder(message: string, setPlaceholder: (msg: string) => void): void {
  setPlaceholder(message);
  setTimeout(() => setPlaceholder('Type a message...'), 1500);
}
