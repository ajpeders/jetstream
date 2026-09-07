import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { actions, clientSid, collapseState, polled, publishRefresh } from '@jet/ui';

import { MediaRequest, RequestFeed, RequestsService } from './requests.service';

@Component({
  // Adopts the page's existing <section id="reqlist-panel">, so every rule in
  // viewer.css and the theme keeps matching.
  selector: '[jet-requests-panel]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[class.collapsed]': 'collapsed()',
  },
  template: `
    <!-- A button, like #queue-head: the header is an interactive control, so a
         div leaves it unreachable by keyboard. viewer.css already carries the
         chrome opt-out (border/font/appearance) and the :focus-visible ring for
         this id, and the label is .rh-label there rather than h2 — an <h2> here
         matches neither that rule nor the theme's uppercase treatment. -->
    <button
      id="reqlist-head"
      type="button"
      [attr.aria-expanded]="!collapsed()"
      aria-controls="reqlist"
      (click)="collapsed.set(!collapsed())"
    >
      <span class="rh-label">requests</span>
      <span id="reqlist-count">{{ countLabel() }}</span>
      <span class="toggle" aria-hidden="true">{{ collapsed() ? '▸' : '▾' }}</span>
    </button>

    <ul id="reqlist">
      @for (r of requests(); track r.id) {
        <li>
          <button
            type="button"
            class="req-vote"
            [class.voted]="r.voted"
            [title]="r.voted ? 'Remove your vote' : 'Vote for this'"
            [disabled]="act.busy('vote:' + r.id)"
            (click)="vote(r)"
          >
            <span class="req-arrow">▲</span>
            <span class="req-n">{{ r.votes }}</span>
          </button>

          <div class="req-meta">
            <div class="req-title">{{ r.title || '(untitled)' }}</div>
            @if (r.requester) {
              <div class="req-by">by {{ r.requester }}</div>
            }
          </div>

          @if (canManage()) {
            <div class="req-actions">
              <button
                type="button"
                class="req-ok"
                title="Approve &amp; queue"
                [disabled]="act.busy('approve:' + r.id)"
                (click)="approve(r)"
              >
                Queue
              </button>
              <button
                type="button"
                class="req-no"
                title="Deny"
                [disabled]="act.busy('deny:' + r.id)"
                (click)="deny(r)"
              >
                ✕
              </button>
            </div>
          }
        </li>
      } @empty {
        <li class="empty">
          No requests yet — find something in the library below and tap Request.
        </li>
      }
    </ul>
  `,
})
export class RequestsPanelComponent {
  private readonly api = inject(RequestsService);
  protected readonly act = actions();

  private readonly poll = polled(() => this.api.list(clientSid()), {
    initial: { requests: [], can_manage: false } as RequestFeed,
  });

  readonly requests = computed(() => this.poll.value().requests);
  readonly canManage = computed(() => this.poll.value().can_manage);

  readonly collapsed = collapseState('jetstream_reqlist_collapsed');

  constructor() {
    // The library-search panel calls this after filing a request, so the new
    // entry (or its bumped vote count) shows up without waiting for the tick.
    publishRefresh('requests', () => this.poll.refresh());
  }

  countLabel(): string {
    const n = this.requests().length;
    return n ? `${n} ${n === 1 ? 'request' : 'requests'}` : '';
  }

  async vote(r: MediaRequest): Promise<void> {
    // Deliberately quiet: voting is high-frequency and its result is visible in
    // the count, so a toast per click would be noise.
    await this.act.run(`vote:${r.id}`, () => this.api.vote(r.id, clientSid()), {
      ok: '',
      fail: 'Vote failed',
    });
    this.poll.refresh();
  }

  async approve(r: MediaRequest): Promise<void> {
    await this.act.run(`approve:${r.id}`, () => this.api.approve(r.id), {
      ok: 'Queued',
      fail: 'Approve failed',
    });
    this.poll.refresh();
    // Approving moves the item into the queue, which is a separate component.
    window.jetstream?.['queue']?.();
  }

  async deny(r: MediaRequest): Promise<void> {
    await this.act.run(`deny:${r.id}`, () => this.api.deny(r.id), {
      ok: 'Request removed',
      fail: 'Deny failed',
    });
    this.poll.refresh();
  }
}
