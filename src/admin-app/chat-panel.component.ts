import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';

import { actions, clientSid, collapseState, pollingStream, toast } from '@jet/ui';

export interface ChatMessage {
  id: number;
  sid: string;
  name?: string | null;
  text: string;
}

interface ChatPage {
  messages?: ChatMessage[];
  deleted_ids?: number[];
  max_id?: number;
}

/** Stable per-sid hue, so a given anon keeps one colour across the session. */
function chatColor(sid: string): string {
  let h = 0;
  for (let i = 0; i < sid.length; i++) h = ((h << 5) - h + sid.charCodeAt(i)) | 0;
  return `hsl(${((h % 360) + 360) % 360}, 65%, 60%)`;
}

const NAME_KEY = 'jetstream_chatname';
/** Treat the pane as "following" if the reader is within this many px of the
 *  bottom; matches the hand-written panel's threshold. */
const STICK_PX = 50;

@Component({
  // Attribute selector: the component adopts the page's existing
  // <section id="chat-panel"> so #chat-panel rules keep matching and the
  // .collapsed host binding lands on the element the CSS expects.
  selector: '[jet-chat-panel]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[class.collapsed]': 'collapsed()' },
  template: `
    <div id="chat-header" (click)="collapsed.set(!collapsed())">
      <h2>Chat</h2>
      <span class="toggle">{{ collapsed() ? '▸' : '▾' }}</span>
    </div>

    <div id="chat-messages" #pane>
      @for (m of messages(); track m.id) {
        <div class="chat-msg" [class.self]="m.sid === sid">
          <span class="chat-dot" [style.background]="colorFor(m.sid)"></span>
          <span class="chat-name">{{ (m.name || 'anonymous') + ':' }}</span>
          <span class="chat-text">{{ m.text }}</span>
          @if (isAdmin) {
            <span class="mod-actions">
              <button
                type="button"
                title="Delete message"
                [disabled]="act.busy('del:' + m.id)"
                (click)="remove(m)"
              >
                ✕
              </button>
              <button
                type="button"
                title="Mute this sid"
                [disabled]="act.busy('mute:' + m.id)"
                (click)="mute(m)"
              >
                mute
              </button>
            </span>
          }
        </div>
      } @empty {
        <div class="empty">No messages yet.</div>
      }
    </div>

    <form id="chat-form" autocomplete="off" (submit)="send($event)">
      <input
        id="chat-name-input"
        type="text"
        maxlength="24"
        placeholder="name"
        aria-label="Display name (optional, anonymous if blank)"
        [value]="name()"
        (input)="name.set(value($event))"
        (change)="persistName()"
      />
      <input
        id="chat-input"
        type="text"
        maxlength="500"
        [placeholder]="placeholder()"
        [disabled]="sending()"
        [value]="draft()"
        (input)="draft.set(value($event))"
      />
      <button id="chat-send" type="submit" [disabled]="sending()">send</button>
    </form>
  `,
})
export class ChatPanelComponent {
  private readonly http = inject(HttpClient);
  protected readonly act = actions();

  /** Moderation is host-only: /controls serves the same page to friends, who
   *  don't get to delete each other's messages. */
  protected readonly isAdmin = !location.pathname.startsWith('/controls');
  protected readonly sid = clientSid();

  private readonly pane = viewChild<ElementRef<HTMLElement>>('pane');

  readonly messages = signal<ChatMessage[]>([]);
  readonly collapsed = collapseState('jetstream_chat_collapsed', false);

  readonly name = signal(readName());
  readonly draft = signal('');
  readonly sending = signal(false);
  readonly placeholder = signal('Type a message…');

  /** Highest id seen, so each tick asks only for what is new. */
  private maxId = 0;
  /** Whether the reader was pinned to the bottom when the last batch arrived. */
  private stick = true;

  private readonly poll = pollingStream(() => this.fetchPage(), { intervalMs: 2500 });

  constructor() {
    this.poll.stream$.pipe(takeUntilDestroyed()).subscribe((page) => this.apply(page));

    // Restore the scroll position after the rows render. Measuring has to
    // happen before the DOM changes (in apply), and scrolling after it — a
    // reader who has scrolled up to read history must not be yanked down.
    afterRenderEffect(() => {
      this.messages();
      if (!this.stick) return;
      const el = this.pane()?.nativeElement;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  private fetchPage(): Observable<ChatPage> {
    return this.http
      .get<ChatPage>(`/chat/recent?since=${this.maxId}`)
      .pipe(catchError(() => NO_EMIT));
  }

  /** Fold a tick into what is on screen: append new rows, drop deleted ones. */
  private apply(page: ChatPage): void {
    const el = this.pane()?.nativeElement;
    this.stick =
      !el || el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;

    if (typeof page.max_id === 'number') this.maxId = page.max_id;

    const incoming = Array.isArray(page.messages) ? page.messages : [];
    const deleted = Array.isArray(page.deleted_ids) ? new Set(page.deleted_ids) : null;
    if (!incoming.length && !deleted?.size) return;

    this.messages.update((cur) => {
      let next = incoming.length ? [...cur, ...incoming] : cur;
      if (deleted?.size) next = next.filter((m) => !deleted.has(m.id));
      return next;
    });
  }

  colorFor(sid: string): string {
    return chatColor(sid);
  }

  value(e: Event): string {
    return (e.target as HTMLInputElement).value;
  }

  persistName(): void {
    try {
      localStorage.setItem(NAME_KEY, this.name().trim().slice(0, 24));
    } catch {
      /* storage blocked — the name just won't survive a reload */
    }
  }

  async send(e: Event): Promise<void> {
    e.preventDefault();
    const text = this.draft().trim();
    if (!text) return;
    this.sending.set(true);
    try {
      const r = await fetch('/chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, sid: this.sid, name: this.name().trim() }),
      });
      if (r.ok) {
        this.draft.set('');
        this.poll.refresh();
      } else {
        // Rejections report in the placeholder rather than a toast — the reason
        // belongs next to the input the viewer is about to retype into.
        const j: { error?: string; remaining?: number } = await r
          .json()
          .catch(() => ({}));
        this.flashPlaceholder(this.rejectionText(j));
      }
    } catch {
      /* network blip — leave the draft intact so it can be retried */
    } finally {
      this.sending.set(false);
    }
  }

  private rejectionText(j: { error?: string; remaining?: number }): string {
    if (j.error === 'muted') {
      const mins = Math.max(1, Math.ceil((j.remaining || 0) / 60));
      return `muted (${mins}m left)`;
    }
    if (j.error === 'rate limited') return 'slow down — too many messages';
    return j.error || 'send failed';
  }

  private flashPlaceholder(msg: string): void {
    const prev = this.placeholder();
    this.placeholder.set(msg);
    setTimeout(() => this.placeholder.set(prev), 1500);
  }

  async remove(m: ChatMessage): Promise<void> {
    const ok = await this.act.run(
      `del:${m.id}`,
      () => fetch(`/admin/api/chat/${m.id}`, { method: 'DELETE' }),
      { ok: '', fail: 'Delete failed' },
    );
    // Drop it locally at once; the server's deleted_ids will confirm on the
    // next tick for everyone else's pane.
    if (ok) this.messages.update((cur) => cur.filter((x) => x.id !== m.id));
  }

  async mute(m: ChatMessage): Promise<void> {
    const raw = prompt('Mute this sid for how many minutes? (0 to unmute)', '5');
    if (raw === null) return;
    const minutes = parseInt(raw, 10);
    if (!Number.isFinite(minutes) || minutes < 0) {
      toast('Invalid duration');
      return;
    }
    await this.act.run(
      `mute:${m.id}`,
      () =>
        fetch('/admin/api/chat/mute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sid: m.sid, seconds: minutes * 60 }),
        }),
      { ok: minutes ? `Muted ${minutes}m` : 'Unmuted', fail: 'Mute failed' },
    );
  }
}

function readName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? '';
  } catch {
    return '';
  }
}
