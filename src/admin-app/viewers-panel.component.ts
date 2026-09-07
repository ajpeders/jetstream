import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';

import { fmtAgo, polled } from '@jet/ui';

import { Viewer, ViewersService, ViewerHistoryRow } from './viewers.service';

@Component({
  selector: 'jet-viewers-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe],
  template: `
    <div class="head">
      <h2>Viewers</h2>
      <span class="badge">{{ viewers().length }}</span>
      <button type="button" (click)="toggleHistory()">
        {{ historyOpen() ? 'Hide history' : 'History' }}
      </button>
    </div>

    <ul class="viewers">
      @for (v of viewers(); track v.ip) {
        <li>
          <span class="who" [class.anon]="!v.who">{{ v.who || 'anonymous' }}</span>
          <span class="ip">{{ v.ip }}</span>
          <span class="loc">{{ v.loc || '?' }}</span>
          <span class="ago">{{ agoFor(v.last_seen) }}</span>
        </li>
      } @empty {
        <li class="empty">no active viewers</li>
      }
    </ul>

    @if (historyOpen()) {
      <div class="history">
        @switch (historyState()) {
          @case ('loading') {
            <div class="muted">loading…</div>
          }
          @case ('error') {
            <div class="err">Failed: {{ historyError() }}</div>
          }
          @default {
            @for (r of history(); track $index) {
              <div class="row">
                <span class="ts">{{ (r.ts || 0) * 1000 | date: 'medium' }}</span>
                <span class="ip">{{ r.ip || '?' }}</span>
                <span class="loc">{{ r.loc || '?' }}</span>
                <span class="who">{{ r.who || 'anonymous' }}</span>
              </div>
            } @empty {
              <div class="muted">no history yet</div>
            }
          }
        }
      </div>
    }
  `,
  styles: `
    :host { display: block; }

    .head { display: flex; align-items: center; gap: 0.5rem; }
    h2 { margin: 0; font-size: 0.9rem; font-weight: 500; color: #ccc; letter-spacing: 0.02em; }

    ul.viewers { list-style: none; padding: 0; margin: 0.5rem 0 0; font-size: 0.85rem; }
    ul.viewers li { display: flex; gap: 0.5rem; align-items: baseline; padding: 0.3rem 0; border-bottom: 1px solid #1a1a1a; }
    ul.viewers li.empty { color: #666; padding: 0.4rem 0; border-bottom: none; }

    .who { flex: 0 0 7rem; color: #cda; }
    .who.anon { color: #666; font-style: italic; }
    .ip { flex: 0 0 9rem; color: #9c9; font-family: ui-monospace, monospace; }
    .loc { flex: 1; color: #aaa; }
    .ago { color: #666; font-size: 0.75rem; }

    .history {
      margin-top: 0.6rem; max-height: 18rem; overflow: auto;
      font-size: 0.8rem; background: #111; padding: 0.5rem; border-radius: var(--js-radius, 4px);
    }
    .history .row {
      padding: 0.18rem 0; font-family: ui-monospace, monospace;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .history .ts { color: #888; }
    .history .ip { color: #9c9; margin-left: 0.6rem; flex: none; }
    .history .loc { color: #aaa; margin-left: 0.6rem; flex: none; }
    .history .who { color: #ccc; margin-left: 0.6rem; flex: none; }
    .muted { color: #666; }
    .err { color: #e88; }

    /* Mirrors the max-width:760px rule the old markup inherited from admin.css. */
    @media (max-width: 760px) {
      ul.viewers li > span { font-size: 0.78rem; }
    }
  `,
})
export class ViewersPanelComponent {
  private readonly api = inject(ViewersService);

  /** 5s poll, identical cadence to the setInterval it replaces. */
  readonly viewers = polled(() => this.api.list(), { initial: [] as Viewer[] }).value;

  readonly historyOpen = signal(false);
  readonly history = signal<ViewerHistoryRow[]>([]);
  readonly historyError = signal('');
  readonly historyState = signal<'loading' | 'error' | 'ready'>('ready');

  /** Recomputed only when the poll delivers, so the relative times refresh on
   *  the same 5s tick the old innerHTML rebuild did. */
  private readonly now = computed(() => {
    this.viewers();
    return Date.now() / 1000;
  });

  agoFor(lastSeen: number): string {
    return fmtAgo(this.now() - lastSeen);
  }

  toggleHistory(): void {
    const open = !this.historyOpen();
    this.historyOpen.set(open);
    if (!open) return;

    this.historyState.set('loading');
    this.api.history().subscribe({
      next: (rows) => {
        // Newest first — the endpoint tails the log, so it arrives oldest-first.
        this.history.set(Array.isArray(rows) ? [...rows].reverse() : []);
        this.historyState.set('ready');
      },
      error: (e: unknown) => {
        this.historyError.set(e instanceof Error ? e.message : String(e));
        this.historyState.set('error');
      },
    });
  }
}
