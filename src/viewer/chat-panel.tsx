import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';

import { clientSid, useCollapse, usePollTick } from '@/lib';

interface ChatMessage {
  id: number;
  sid: string;
  name: string | null;
  text: string;
}

interface ChatDelta {
  max_id?: number;
  messages?: ChatMessage[];
  deleted_ids?: number[];
}

const POLL_MS = 2500;
const FEEDBACK_MS = 3000;
const PLACEHOLDER = 'Type a message...';

function chatColor(value: string): string {
  let h = 0;
  for (let i = 0; i < value.length; i += 1) h = ((h << 5) - h + value.charCodeAt(i)) | 0;
  return `hsl(${((h % 360) + 360) % 360}, 65%, 60%)`;
}

/** Mounts into <div id="chat-app"> and renders the <section id="chat"> that
 *  viewer.css styles. */
export function ChatPanel() {
  const sid = clientSid();
  const [collapsed, setCollapsed] = useCollapse('jetstream_chat_collapsed', false);
  const [name, setName] = useState(() => readName());
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [placeholder, setPlaceholder] = useState(PLACEHOLDER);
  const [feedback, setFeedback] = useState<{ text: string; kind: 'error' | 'success' } | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const deleted = useRef(new Set<number>());
  const maxId = useRef(0);
  const pane = useRef<HTMLDivElement>(null);
  const feedbackTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Chat is best-effort: a failed tick is skipped and playback never cares.
  const fetcher = useCallback(async (): Promise<ChatDelta | undefined> => {
    const r = await fetch(`/chat/recent?since=${maxId.current}`);
    return r.ok ? ((await r.json()) as ChatDelta) : undefined;
  }, []);
  const onTick = useCallback((payload: ChatDelta) => {
    if (typeof payload.max_id === 'number') maxId.current = payload.max_id;
    for (const id of payload.deleted_ids ?? []) deleted.current.add(id);
    const incoming = payload.messages ?? [];
    if (incoming.length === 0 && deleted.current.size === 0) return;
    const el = pane.current;
    const wasNearBottom = el ? el.scrollHeight - el.scrollTop - el.clientHeight < 50 : true;
    setMessages((prev) => [...prev, ...incoming].filter((m) => !deleted.current.has(m.id)));
    if (incoming.length && wasNearBottom) {
      requestAnimationFrame(() => {
        if (pane.current) pane.current.scrollTop = pane.current.scrollHeight;
      });
    }
  }, []);
  const refresh = usePollTick(fetcher, onTick, { intervalMs: POLL_MS });

  useEffect(() => () => clearTimeout(feedbackTimer.current), []);

  const showFeedback = (msg: string, kind: 'error' | 'success' = 'error') => {
    setFeedback({ text: msg, kind });
    clearTimeout(feedbackTimer.current);
    feedbackTimer.current = setTimeout(() => setFeedback(null), FEEDBACK_MS);
  };
  const flashPlaceholder = (msg: string) => {
    setPlaceholder(msg);
    showFeedback(msg);
    setTimeout(() => setPlaceholder(PLACEHOLDER), 1600);
  };

  const persistName = () => {
    const trimmed = name.trim().slice(0, 24);
    setName(trimmed);
    try {
      localStorage.setItem('jetstream_chatname', trimmed);
    } catch {
      /* storage blocked — name lasts as long as the page */
    }
  };

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const message = text.trim();
    if (!message || sending) return;
    setSending(true);
    try {
      const r = await fetch('/chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, sid, name: name.trim() }),
      });
      if (r.ok) {
        setText('');
        showFeedback('Message sent.', 'success');
        refresh();
      } else {
        const payload = (await r.json().catch(() => ({}))) as { error?: string; remaining?: number };
        if (payload.error === 'muted') {
          const mins = Math.max(1, Math.ceil((payload.remaining || 0) / 60));
          flashPlaceholder(`You are muted for about ${mins}m.`);
        } else if (payload.error === 'rate limited') {
          flashPlaceholder('Slow down before sending another message.');
        } else {
          flashPlaceholder(payload.error || 'Message could not be sent.');
        }
      }
    } catch {
      flashPlaceholder('Message could not be sent.');
    } finally {
      setSending(false);
    }
  };

  const onHeaderKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setCollapsed(!collapsed);
    }
  };

  const n = messages.length;
  return (
    <section id="chat" className={collapsed ? 'collapsed' : undefined}>
      <div
        id="chat-header"
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        aria-controls="chat-messages chat-form"
        onClick={() => setCollapsed(!collapsed)}
        onKeyDown={onHeaderKey}
      >
        <div className="chat-title"><h2>Chat</h2></div>
        <div className="chat-meta">
          <span className="chat-count">{n} {n === 1 ? 'message' : 'messages'}</span>
          <span className="toggle" aria-hidden="true">{collapsed ? '+' : '−'}</span>
        </div>
      </div>

      <div id="chat-messages" ref={pane} aria-live="polite" aria-label="Chat messages">
        {n === 0 ? (
          <div className="empty">
            <strong>No messages yet</strong>
            <span>Start the conversation when you are ready.</span>
          </div>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={'chat-msg' + (m.sid === sid ? ' self' : '')} data-id={m.id}>
              <span className="chat-dot" style={{ background: chatColor(m.sid) }} />
              <span className="chat-name">{m.name || 'anonymous'}</span>
              <span className="chat-text">{m.text}</span>
            </div>
          ))
        )}
      </div>

      <form id="chat-form" autoComplete="off" onSubmit={(e) => void send(e)}>
        <input
          id="chat-name-input"
          type="text"
          maxLength={24}
          placeholder="name"
          aria-label="Display name (optional, anonymous if blank)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={persistName}
        />
        <input
          id="chat-input"
          type="text"
          maxLength={500}
          placeholder={placeholder}
          aria-label="Chat message"
          aria-describedby={feedback ? 'chat-feedback' : undefined}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={sending}
        />
        <button id="chat-send" type="submit" disabled={sending || !text.trim()} aria-busy={sending}>
          {sending ? 'Sending...' : 'Send'}
        </button>
      </form>

      {feedback && (
        <div
          id="chat-feedback"
          className={feedback.kind === 'success' ? 'success' : undefined}
          role={feedback.kind === 'success' ? 'status' : 'alert'}
        >
          {feedback.text}
        </div>
      )}
    </section>
  );
}

function readName(): string {
  try {
    return localStorage.getItem('jetstream_chatname') || '';
  } catch {
    return '';
  }
}
