import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

import { actions, fmtAgo, polled } from '@jet/ui';

/** An account-backed ask for media we do NOT have yet. Phase 1 records the
 *  host's decision only; later phases turn approval into Radarr/Sonarr work. */
export interface MediaRequest {
  id: string;
  title: string | null;
  username: string | null;
  status?: 'pending' | 'approved' | 'rejected' | string;
  kind?: string | null;
  year?: number | string | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  seasons?: (number | string)[] | null;
  reject_reason?: string | null;
  requested_at?: number | null;
}

/** One `key value` pair on a row's meta line. */
interface MetaBit {
  k: string;
  v: string;
}

@Component({
  selector: 'jet-media-requests-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="head">
      <h2>Media Requests</h2>
      <span class="badge" [class.waiting]="pendingCount() > 0">{{ pendingCount() }}</span>
      @if (hasDecided()) {
        <button id="media-requests-clear" type="button" (click)="hideDecided.set(!hideDecided())">
          {{ hideDecided() ? 'Show decided' : 'Hide decided' }}
        </button>
      }
    </div>

    <ul>
      @for (r of visible(); track r.id) {
        <li [class.decided]="statusOf(r) !== 'pending'">
          <div class="mrtop">
            <span class="mrstatus" [class]="'mrstatus ' + statusOf(r)">{{ statusOf(r) }}</span>
            <span class="mrtitle">{{ r.title || '(untitled)' }}</span>
          </div>

          <div class="mrmeta">
            @for (bit of meta(r); track $index) {
              <span class="k">{{ bit.k }}</span> {{ bit.v }}
              @if (!$last) {
                <span class="sep">&nbsp;·&nbsp;</span>
              }
            }
          </div>

          <div class="mractions">
            @if (statusOf(r) === 'pending') {
              <button
                class="approve"
                type="button"
                [disabled]="act.busy('approve:' + r.id)"
                (click)="approve(r)"
              >
                Approve
              </button>
              <button
                type="button"
                [disabled]="act.busy('reject:' + r.id)"
                (click)="reject(r)"
              >
                Reject
              </button>
            }
          </div>
        </li>
      } @empty {
        <li class="empty-r">
          {{ hideDecided() ? 'No pending media requests.' : 'No media requests.' }}
        </li>
      }
    </ul>
  `,
  styles: `
    /* Was an inline style attribute with no stylesheet rule behind it. */
    #media-requests-clear {
      margin-left: auto;
      font-size: 0.75rem;
      padding: 0.2rem 0.55rem;
    }
  `,
})
export class MediaRequestsPanelComponent {
  private readonly http = inject(HttpClient);
  protected readonly act = actions();

  private readonly poll = polled(() => this.list(), { initial: [] as MediaRequest[] });
  readonly all = this.poll.value;

  /** View-only filter — no request, so it stays local rather than in storage,
   *  matching the old `let mediaRequestsHideDecided = false` per page load. */
  readonly hideDecided = signal(false);

  readonly pendingCount = computed(
    () => this.all().filter((r) => this.statusOf(r) === 'pending').length,
  );
  readonly hasDecided = computed(
    () => this.all().some((r) => this.statusOf(r) !== 'pending'),
  );
  readonly visible = computed(() =>
    this.hideDecided() ? this.all().filter((r) => this.statusOf(r) === 'pending') : this.all(),
  );

  private list(): Observable<MediaRequest[]> {
    return this.http.get<MediaRequest[]>('/admin/api/media/requests').pipe(
      map((r) => (Array.isArray(r) ? r : [])),
      catchError(() => NO_EMIT),
    );
  }

  statusOf(r: MediaRequest): string {
    return r.status || 'pending';
  }

  meta(r: MediaRequest): MetaBit[] {
    const bits: MetaBit[] = [{ k: 'by', v: r.username || 'unknown' }];
    if (r.kind) bits.push({ k: 'kind', v: r.kind });
    if (r.year) bits.push({ k: 'year', v: String(r.year) });
    if (r.tmdb_id != null) bits.push({ k: 'tmdb', v: String(r.tmdb_id) });
    if (r.tvdb_id != null) bits.push({ k: 'tvdb', v: String(r.tvdb_id) });
    if (Array.isArray(r.seasons) && r.seasons.length) {
      bits.push({ k: 'seasons', v: r.seasons.join(', ') });
    }
    if (r.reject_reason) bits.push({ k: 'reason', v: r.reject_reason });
    if (r.requested_at) {
      bits.push({ k: 'requested', v: fmtAgo(Date.now() / 1000 - r.requested_at) });
    }
    return bits;
  }

  async approve(r: MediaRequest): Promise<void> {
    await this.act.run(
      `approve:${r.id}`,
      () =>
        fetch(`/admin/api/media/requests/${encodeURIComponent(r.id)}/approve`, {
          method: 'POST',
        }),
      { ok: 'Media request approved', fail: 'Approve failed' },
    );
    this.poll.refresh();
  }

  async reject(r: MediaRequest): Promise<void> {
    // prompt() returns null on cancel; the old code coerced that to "" and
    // rejected anyway, so cancelling still rejected. Treated as a bug here —
    // cancel now means cancel, and an empty reason is still allowed via OK.
    const reason = prompt('Reject reason (optional)');
    if (reason === null) return;
    await this.act.run(
      `reject:${r.id}`,
      () =>
        fetch(`/admin/api/media/requests/${encodeURIComponent(r.id)}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason }),
        }),
      { ok: 'Media request rejected', fail: 'Reject failed' },
    );
    this.poll.refresh();
  }
}
