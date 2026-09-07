import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

import { actions, polled } from '@jet/ui';

/** A viewer-uploaded reaction image awaiting host moderation. */
export interface CustomReaction {
  id: number;
  label: string | null;
  ip: string | null;
}

@Component({
  selector: 'jet-custom-reactions-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="head">
      <h2>Custom reactions</h2>
      <span class="badge">{{ items().length }}</span>
    </div>

    <div id="reactions-grid">
      @for (c of items(); track c.id) {
        <div class="rx" [title]="hover(c)">
          <img [src]="'/reactions/img/' + c.id" alt="" />
          <button
            type="button"
            title="Delete"
            [disabled]="act.busy('del:' + c.id)"
            (click)="remove(c)"
          >
            ✕
          </button>
        </div>
      } @empty {
        <span class="empty-r">No custom reactions uploaded.</span>
      }
    </div>
  `,
})
export class CustomReactionsPanelComponent {
  private readonly http = inject(HttpClient);
  protected readonly act = actions();

  // 10s — uploads are rare, and each tick pulls a grid of images.
  private readonly poll = polled(() => this.list(), {
    initial: [] as CustomReaction[],
    intervalMs: 10_000,
  });
  readonly items = this.poll.value;

  private list(): Observable<CustomReaction[]> {
    return this.http.get<CustomReaction[]>('/admin/api/reactions').pipe(
      map((r) => (Array.isArray(r) ? r : [])),
      catchError(() => NO_EMIT),
    );
  }

  /** Label plus the uploader's IP, as the old cell title did. */
  hover(c: CustomReaction): string {
    return `${c.label ?? ''}${c.ip ? ` · ${c.ip}` : ''}`;
  }

  async remove(c: CustomReaction): Promise<void> {
    await this.act.run(
      `del:${c.id}`,
      () => fetch(`/admin/api/reactions/${c.id}`, { method: 'DELETE' }),
      { ok: 'Reaction removed', fail: 'Remove failed' },
    );
    this.poll.refresh();
  }
}
