import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { actions, fmtAgo, formatTime, polled } from '@jet/ui';

import { Report, ReportTriage, ReportsService } from './reports.service';

/** One `key value` pair on a report's meta line. */
interface MetaBit {
  k: string;
  v: string;
  mono?: boolean;
}

@Component({
  selector: 'jet-reports-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="head">
      <h2>Reports</h2>
      <span class="badge" [class.waiting]="unhandled() > 0">{{ unhandled() }}</span>
      @if (reports().length) {
        <button
          id="reports-clear"
          type="button"
          [disabled]="act.busy('clear')"
          (click)="clearAll()"
        >
          Clear all
        </button>
      }
    </div>

    <ul>
      @for (r of reports(); track r.id) {
        <li [class.handled]="isHandled(r)">
          <div class="rtop">
            <span class="rstatus" [class.handled]="isHandled(r)" [class.new]="!isHandled(r)">
              {{ isHandled(r) ? 'handled' : 'new' }}
            </span>
            <span class="rwho">{{ r.viewer_label || 'anonymous' }}</span>
            <span class="rtime">{{ ago(r.ts) }}</span>
          </div>

          <div class="rmsg" [class.none]="!r.message">{{ r.message || '(no message)' }}</div>

          <div class="rmeta">
            @for (group of metaGroups(r); track $index) {
              <div>
                @for (bit of group; track $index) {
                  <span class="k">{{ bit.k }}</span>
                  @if (bit.mono) {
                    <code>{{ bit.v }}</code>
                  } @else {
                    {{ bit.v }}
                  }
                  @if (!$last) {
                    <span class="sep">&nbsp;·&nbsp;</span>
                  }
                }
              </div>
            }
          </div>

          @if (r.triage != null) {
            <div class="rtriage">
              @if (triageText(r.triage); as text) {
                <span class="rtkey">triage</span> {{ text }}
              } @else {
                @if (triageOf(r.triage); as t) {
                  @if (t.cause) {
                    <span class="rtkey">cause</span> <strong>{{ t.cause }}</strong>
                  }
                  @if (t.confidence) {
                    <span class="rtkey">confidence</span> {{ t.confidence }}
                  }
                  @if (t.reasoning) {
                    <div>{{ t.reasoning }}</div>
                  }
                  @if (t.suggested_action) {
                    <div><span class="rtkey">action</span> {{ t.suggested_action }}</div>
                  }
                  @if (t.empty) {
                    <code>{{ t.raw }}</code>
                  }
                }
              }
            </div>
          }

          <div class="ractions">
            <button
              type="button"
              class="handle"
              [disabled]="act.busy('handle:' + r.id)"
              (click)="toggleHandled(r)"
            >
              {{ isHandled(r) ? 'Reopen' : 'Mark handled' }}
            </button>
            <button type="button" [disabled]="act.busy('delete:' + r.id)" (click)="remove(r)">
              Delete
            </button>
          </div>
        </li>
      } @empty {
        <li class="empty-r">No issue reports.</li>
      }
    </ul>
  `,
  styles: `
    /* The panel's chrome still comes from #reports-panel in css/admin.css —
       this component only owns what the old markup carried as inline style on
       the Clear all button, which had no stylesheet rule to fall back on.
       (Its red danger colouring comes from the shared theme and is unaffected.) */
    #reports-clear {
      margin-left: auto;
      font-size: 0.75rem;
      padding: 0.2rem 0.55rem;
    }
  `,
})
export class ReportsPanelComponent {
  private readonly api = inject(ReportsService);
  protected readonly act = actions();

  private readonly poll = polled(() => this.api.list(), { initial: [] as Report[] });
  readonly reports = this.poll.value;

  readonly unhandled = computed(
    () => this.reports().filter((r) => r.status === 'new').length,
  );

  isHandled(r: Report): boolean {
    return r.status === 'handled';
  }

  ago(ts: number | undefined): string {
    return ts ? fmtAgo(Date.now() / 1000 - ts) : '';
  }

  /** The meta line, as data rather than a string of HTML. Three groups: what
   *  the client saw, what the encode was doing, and who reported it. */
  metaGroups(r: Report): MetaBit[][] {
    const c = r.client ?? {};
    const h = r.stream_health ?? {};

    const client: MetaBit[] = [];
    const title = c.title || h.title;
    if (title) client.push({ k: 'title', v: title });
    if (c.path) client.push({ k: 'path', v: c.path, mono: true });
    if (c.position_seconds != null) {
      client.push({ k: 'at', v: formatTime(c.position_seconds) });
    }
    if (c.playback_mode) client.push({ k: 'mode', v: c.playback_mode });
    if (c.muted != null) client.push({ k: 'muted', v: c.muted ? 'yes' : 'no' });
    if (c.fullscreen != null) client.push({ k: 'fs', v: c.fullscreen ? 'yes' : 'no' });

    const health: MetaBit[] = [];
    if (h.encoder) health.push({ k: 'enc', v: h.encoder });
    health.push({ k: 'ffmpeg', v: h.ffmpeg_alive ? 'alive' : 'down' });
    if (h.viewers != null) health.push({ k: 'viewers', v: String(h.viewers) });
    if (h.server_position_seconds != null) {
      health.push({ k: 'srv@', v: formatTime(h.server_position_seconds) });
    }
    if (h.segments_on_disk != null) {
      health.push({ k: 'segs', v: String(h.segments_on_disk) });
    }
    if (h.paused) health.push({ k: 'paused', v: '' });

    const who: MetaBit[] = [];
    if (r.ip) who.push({ k: 'ip', v: r.ip, mono: true });
    if (r.user_agent) who.push({ k: 'ua', v: r.user_agent });

    return [client, health, who].filter((g) => g.length > 0);
  }

  /** Non-null only for the legacy string form of a triage verdict. */
  triageText(t: ReportTriage | null | undefined): string | null {
    if (typeof t === 'string') return t;
    if (t != null && typeof t !== 'object') return String(t);
    return null;
  }

  triageOf(t: ReportTriage | null | undefined) {
    if (t == null || typeof t !== 'object') return null;
    const cause = t.cause == null ? '' : String(t.cause);
    const confidence = t.confidence == null ? '' : String(t.confidence);
    const reasoning = t.reasoning == null ? '' : String(t.reasoning);
    const suggested_action = t.suggested_action == null ? '' : String(t.suggested_action);
    const empty = !cause && !confidence && !reasoning && !suggested_action;
    return {
      cause,
      confidence,
      reasoning,
      suggested_action,
      empty,
      raw: empty ? JSON.stringify(t) : '',
    };
  }

  async toggleHandled(r: Report): Promise<void> {
    const unhandle = this.isHandled(r);
    await this.act.run(`handle:${r.id}`, () => this.api.setHandled(r.id, unhandle), {
      ok: unhandle ? 'Report reopened' : 'Marked handled',
      fail: 'Update failed',
    });
    this.poll.refresh();
  }

  async remove(r: Report): Promise<void> {
    await this.act.run(`delete:${r.id}`, () => this.api.remove(r.id), {
      ok: 'Report deleted',
      fail: 'Delete failed',
    });
    this.poll.refresh();
  }

  async clearAll(): Promise<void> {
    if (!confirm("Delete ALL issue reports? This can't be undone.")) return;
    await this.act.run('clear', () => this.api.clearAll(), {
      ok: (body) => {
        const n = (body as { cleared?: number }).cleared;
        return `Cleared ${n ?? ''} report${n === 1 ? '' : 's'}`.trim();
      },
      fail: 'Clear failed',
    });
    this.poll.refresh();
  }
}
