import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

import { PosterThumbComponent, actions, polled } from '@jet/ui';

declare global {
  interface Window {
    /** admin.html's queue loader — a hoisted function declaration, so it is
     *  genuinely on window. Goes away when the queue panel is ported. */
    loadQueue?: () => void;
  }
}

/** A watch-only viewer asking the host to queue a file we already have.
 *  Distinct from a media request, which asks for something we don't have. */
export interface QueueRequest {
  id: number;
  path: string | null;
  title: string | null;
  requester: string | null;
}

@Component({
  selector: 'jet-queue-requests-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PosterThumbComponent],
  template: `
    <div class="head">
      <h2>Requests</h2>
      <span class="badge" [class.waiting]="items().length > 0">{{ items().length }}</span>
      @if (items().length) {
        <button
          id="requests-clear"
          type="button"
          [disabled]="act.busy('clear')"
          (click)="clearAll()"
        >
          Clear all
        </button>
      }
    </div>

    <ul>
      @for (r of items(); track r.id) {
        <li>
          <span class="rthumb"><jet-poster-thumb [src]="posterFor(r)" /></span>
          <span class="rwho">{{ r.requester || 'anonymous' }}</span>
          <span class="rtitle">{{ label(r) }}</span>
          <button
            class="approve"
            type="button"
            [disabled]="act.busy('approve:' + r.id)"
            (click)="approve(r)"
          >
            Add
          </button>
          <button type="button" [disabled]="act.busy('deny:' + r.id)" (click)="deny(r)">
            Deny
          </button>
        </li>
      } @empty {
        <li class="empty-r">No pending requests.</li>
      }
    </ul>
  `,
  styles: `
    /* Carried over from the old markup's inline style — no stylesheet rule
       backed it, so dropping the attribute would have lost the layout. */
    #requests-clear {
      margin-left: auto;
      font-size: 0.75rem;
      padding: 0.2rem 0.55rem;
    }
  `,
})
export class QueueRequestsPanelComponent {
  private readonly http = inject(HttpClient);
  protected readonly act = actions();

  private readonly poll = polled(() => this.list(), { initial: [] as QueueRequest[] });
  readonly items = this.poll.value;

  private list(): Observable<QueueRequest[]> {
    return this.http.get<QueueRequest[]>('/admin/api/requests').pipe(
      map((r) => (Array.isArray(r) ? r : [])),
      catchError(() => NO_EMIT),
    );
  }

  /** Title if the request carried one, else the filename. */
  label(r: QueueRequest): string {
    return r.title || (r.path ? r.path.split('/').pop() ?? '' : '');
  }

  posterFor(r: QueueRequest): string | null {
    return r.path ? `/poster?path=${encodeURIComponent(r.path)}` : null;
  }

  async approve(r: QueueRequest): Promise<void> {
    const ok = await this.act.run(
      `approve:${r.id}`,
      () => fetch(`/admin/api/requests/${r.id}/approve`, { method: 'POST' }),
      { ok: 'Added to queue', fail: 'Approve failed' },
    );
    this.poll.refresh();
    // Approving appends to the real queue, whose panel is still hand-written.
    if (ok) window.loadQueue?.();
  }

  async deny(r: QueueRequest): Promise<void> {
    await this.act.run(
      `deny:${r.id}`,
      () => fetch(`/admin/api/requests/${r.id}`, { method: 'DELETE' }),
      { ok: 'Request denied', fail: 'Deny failed' },
    );
    this.poll.refresh();
  }

  async clearAll(): Promise<void> {
    // Cheap confirm — the per-row Deny buttons stay one-tap.
    if (!confirm('Clear all pending requests?')) return;
    await this.act.run(
      'clear',
      () => fetch('/admin/api/requests', { method: 'DELETE' }),
      {
        ok: (body) => {
          const n = (body as { cleared?: number }).cleared;
          return `Cleared ${n ?? ''} request${n === 1 ? '' : 's'}`.trim();
        },
        fail: 'Clear failed',
      },
    );
    this.poll.refresh();
  }
}
