import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { PosterThumbComponent, collapseState, formatTime, polled, publishRefresh } from '@jet/ui';

import { QueueItem, QueueService } from './queue.service';

@Component({
  // Attribute selector so the component adopts the page's existing
  // <section id="queue-panel"> rather than nesting inside it — every rule
  // in viewer.css keeps matching, and the port needs no CSS changes at all.
  selector: '[jet-queue-panel]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PosterThumbComponent],
  // The panel keeps its original id so #queue-panel's chrome in viewer.css
  // still applies, and .collapsed stays the same styling hook it always was.
  host: {
    '[class.collapsed]': 'collapsed()',
  },
  template: `
    <button
      id="queue-head"
      type="button"
      [attr.aria-expanded]="!collapsed()"
      aria-controls="queue-list"
      (click)="collapsed.set(!collapsed())"
    >
      <span class="qh-label">queue</span>
      <span id="queue-count">{{ countLabel() }}</span>
      <span class="toggle" aria-hidden="true">{{ collapsed() ? '▸' : '▾' }}</span>
    </button>

    <ol id="queue-list">
      @for (it of items(); track $index) {
        <li>
          <span class="qpos">{{ $index + 1 }}</span>
          <span class="qthumb"><jet-poster-thumb [src]="posterFor(it)" /></span>
          <span class="qtitle">{{ it.title || '(untitled)' }}</span>
          <span class="qdur">{{ it.is_live ? 'LIVE' : formatDuration(it.duration) }}</span>
        </li>
      } @empty {
        <li class="empty">Nothing queued yet.</li>
      }
    </ol>
  `,
})
export class QueuePanelComponent {
  private readonly api = inject(QueueService);

  private readonly queue = polled(() => this.api.list(), { initial: [] as QueueItem[] });
  readonly items = this.queue.value;

  /** Starts collapsed unless a previous visit expanded it — the same key the
   *  hand-written panel used, so a returning viewer sees no change. */
  readonly collapsed = collapseState('jetstream_queue_collapsed');

  constructor() {
    // Approving a request should surface it in the queue at once rather than up
    // to a tick later — called by the request-list panel and, until it is
    // ported, by the library-search code still inline in viewer.html.
    publishRefresh('queue', () => this.queue.refresh());
  }

  countLabel(): string {
    const n = this.items().length;
    return `${n} ${n === 1 ? 'item' : 'items'}`;
  }

  formatDuration(d: number | null): string {
    return formatTime(d);
  }

  posterFor(it: QueueItem): string | null {
    return it.type === 'file' && it.ref
      ? `/poster?path=${encodeURIComponent(it.ref)}`
      : null;
  }
}
