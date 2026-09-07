import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { actions, fmtAgo, formatTime, polled, publishRefresh } from '@jet/ui';

import { VodSession, VodSessionFeed, VodSessionsService } from './vod-sessions.service';

@Component({
  selector: 'jet-vod-sessions-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="head">
      <h2>Active VOD streams</h2>
      <span class="badge">{{ countLabel() }}</span>
    </div>

    <div class="tbl-wrap">
      @if (sessions().length) {
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Title</th>
              <th>Position</th>
              <th>Started</th>
              <th>Last access</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            @for (s of sessions(); track s.session_id) {
              <tr>
                <td class="vwho">{{ s.username || '?' }}</td>
                <td [title]="s.title || ''">{{ s.title || '?' }}</td>
                <td class="vtime">{{ position(s) }}</td>
                <td class="vtime">{{ since(s.started_at) }}</td>
                <td class="vtime">{{ since(s.last_access) }}</td>
                <td>
                  <button
                    type="button"
                    class="danger"
                    [disabled]="act.busy('kill:' + s.session_id)"
                    (click)="kill(s)"
                  >
                    Kill
                  </button>
                </td>
              </tr>
            }
          </tbody>
        </table>
      } @else {
        <span class="empty-v">No active streams.</span>
      }
    </div>
  `,
})
export class VodSessionsPanelComponent {
  private readonly api = inject(VodSessionsService);
  protected readonly act = actions();

  // 10s, not the 5s default: active_vod dots and this table stay fresh without
  // hammering the API. Same cadence the hand-written poller used.
  private readonly poll = polled(() => this.api.list(), {
    initial: { sessions: [], cap: null } as VodSessionFeed,
    intervalMs: 10_000,
  });

  readonly sessions = computed(() => this.poll.value().sessions);

  constructor() {
    // Disabling or deleting a user revokes their sessions and kills any active
    // stream, so the users panel — still hand-written — nudges this table.
    publishRefresh('vodSessions', () => this.poll.refresh());
  }

  countLabel(): string {
    const { sessions, cap } = this.poll.value();
    return `${sessions.length} of ${cap ?? '?'} streams`;
  }

  position(s: VodSession): string {
    return formatTime(s.start_offset);
  }

  /** Relative age, recomputed each poll tick like the old table rebuild. */
  since(ts: number | null): string {
    const now = Date.now() / 1000;
    return fmtAgo(now - (ts ?? now));
  }

  async kill(s: VodSession): Promise<void> {
    if (!confirm("Kill this stream? The viewer's playback stops immediately.")) return;
    await this.act.run(`kill:${s.session_id}`, () => this.api.kill(s.session_id), {
      ok: 'Stream killed',
      fail: 'Kill failed',
    });
    this.poll.refresh();
    // The users panel shows an active_vod dot per user, so it goes stale the
    // moment a stream dies. Both are Angular now, but separate bootstraps mean
    // separate injectors, so the nudge still routes through window.
    window.jetstream?.['users']?.();
  }
}
