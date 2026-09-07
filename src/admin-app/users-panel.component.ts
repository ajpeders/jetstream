import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';

import { actions, fmtAgo, polled, publishRefresh, toast } from '@jet/ui';

import { UserAccount, UsersService } from './users.service';

@Component({
  selector: 'jet-users-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h2>User accounts</h2>

    <form autocomplete="off" (submit)="create($event)">
      <input
        type="text"
        placeholder="username (a-z 0-9 _ . -)"
        maxlength="32"
        autocomplete="off"
        required
        [value]="username()"
        (input)="username.set(asValue($event))"
      />
      <input
        type="password"
        placeholder="password (min 8)"
        minlength="8"
        autocomplete="new-password"
        required
        [value]="password()"
        (input)="password.set(asValue($event))"
      />
      <button type="submit" [disabled]="act.busy('create')">Create user</button>
    </form>

    <div class="form-err" [style.display]="formError() ? 'block' : 'none'">
      {{ formError() }}
    </div>

    <ul>
      @for (u of users(); track u.id) {
        <li [class.disabled]="u.disabled">
          <span class="uname">{{ u.username }}</span>
          @if (u.active_vod) {
            <span class="udot" title="Streaming now"></span>
          }
          @if (u.disabled) {
            <span class="uflag">DISABLED</span>
          }
          @if (u.invited_by) {
            <span class="uvia" title="Self-registered with this invite code">
              via {{ u.invited_by }}
            </span>
          }
          <span class="umeta">{{ meta(u) }}</span>
          <button [disabled]="act.busy('pw:' + u.id)" (click)="resetPassword(u)">
            Password
          </button>
          <button [disabled]="act.busy('dis:' + u.id)" (click)="toggleDisabled(u)">
            {{ u.disabled ? 'Enable' : 'Disable' }}
          </button>
          <button class="danger" [disabled]="act.busy('del:' + u.id)" (click)="remove(u)">
            Delete
          </button>
        </li>
      } @empty {
        <li><span class="umeta">No user accounts yet.</span></li>
      }
    </ul>
  `,
  styles: `
    /* The DISABLED chip was inline style on the old markup with no stylesheet
       rule behind it, so it has to live here or the chip loses its shape. */
    .uflag {
      flex-shrink: 0;
      font-size: 0.65rem;
      letter-spacing: 0.04em;
      font-weight: 600;
      padding: 0.08rem 0.4rem;
      border-radius: 3px;
      background: #3a1818;
      color: #ff8a8a;
      border: 1px solid #5a2424;
    }
  `,
})
export class UsersPanelComponent {
  private readonly api = inject(UsersService);
  protected readonly act = actions();

  private readonly poll = polled(() => this.api.list(), {
    initial: [] as UserAccount[],
    intervalMs: 10_000,
  });
  readonly users = this.poll.value;

  readonly username = signal('');
  readonly password = signal('');
  readonly formError = signal('');

  constructor() {
    // The VOD table nudges this panel when a stream is killed — the dot next to
    // a streaming user goes stale otherwise. Both are Angular now, but they are
    // separate bootstraps and so separate injectors, which is why this still
    // goes through window rather than a shared service.
    publishRefresh('users', () => this.poll.refresh());
  }

  asValue(e: Event): string {
    return (e.target as HTMLInputElement).value;
  }

  meta(u: UserAccount): string {
    return `created ${this.when(u.created)} · last login ${this.when(u.last_login)}`;
  }

  private when(ts: number | null | undefined): string {
    return ts ? fmtAgo(Date.now() / 1000 - ts) : 'never';
  }

  /** Create is hand-rolled rather than run through actions(): the form has to
   *  distinguish a duplicate username from a server fault, which means reading
   *  the error body, and it reports inline rather than as a toast. */
  async create(e: Event): Promise<void> {
    e.preventDefault();
    this.formError.set('');
    const username = this.username().trim();
    const password = this.password();
    if (!username || !password) return;

    try {
      const res = await this.api.create(username, password);
      const body: { error?: string; message?: string } = await res
        .json()
        .catch(() => ({}));
      if (!res.ok) {
        this.formError.set(
          body.error === 'duplicate'
            ? 'That username is already taken.'
            : body.message || body.error || `HTTP ${res.status}`,
        );
        return;
      }
      this.username.set('');
      this.password.set('');
      toast(`User ${username} created`);
      this.poll.refresh();
    } catch (err: unknown) {
      toast(`Failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async resetPassword(u: UserAccount): Promise<void> {
    const pw = prompt(
      `New password for ${u.username} (min 8 chars). Their sessions get logged out.`,
    );
    if (pw == null) return;
    await this.act.run(`pw:${u.id}`, () => this.api.setPassword(u.id, pw), {
      ok: `Password reset for ${u.username}`,
      fail: 'Password reset failed',
    });
  }

  async toggleDisabled(u: UserAccount): Promise<void> {
    const disable = !u.disabled;
    await this.act.run(`dis:${u.id}`, () => this.api.setDisabled(u.id, disable), {
      ok: disable ? `${u.username} disabled — sessions revoked` : `${u.username} enabled`,
      fail: 'Update failed',
    });
    this.poll.refresh();
    // Disabling revokes sessions and kills any active stream.
    window.jetstream?.['vodSessions']?.();
  }

  async remove(u: UserAccount): Promise<void> {
    if (
      !confirm(
        `Delete user ${u.username}? Their sessions are revoked and any active stream is killed.`,
      )
    ) {
      return;
    }
    await this.act.run(`del:${u.id}`, () => this.api.remove(u.id), {
      ok: `Deleted ${u.username}`,
      fail: 'Delete failed',
    });
    this.poll.refresh();
    window.jetstream?.['vodSessions']?.();
  }
}
