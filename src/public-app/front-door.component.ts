import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  signal,
} from '@angular/core';

import { DoorResult, InviteService } from './invite.service';

/** A (enter friend code) → B (accepted; watch or register) → C (register form).
 *  B is only reached when the redeemed code carries can_register; a viewer-tier
 *  code goes straight to "/" because the redeem response already cookied them. */
type View = 'code' | 'choice' | 'register';

/** Mirrors the server's rules so mistakes surface without a round trip; the
 *  server remains the authority. */
const USERNAME_RE = /^[a-z0-9_.-]{3,32}$/;

@Component({
  // Adopts the page's <div class="card du-card">, so home.css keeps matching
  // every id and class below unchanged.
  selector: '[jet-front-door]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h1>jetstream</h1>

    @switch (view()) {
      @case ('code') {
        <section id="state-code">
          <p class="sub">A private stream for friends. Enter the code you were sent to start watching.</p>
          <form id="code-form" (submit)="redeem($event)">
            <label for="code">Friend code</label>
            <input class="du-input" id="code" name="code" type="text" autocomplete="one-time-code"
                   autocapitalize="none" autocorrect="off" spellcheck="false" required
                   [value]="code()" (input)="code.set(val($event))">
            <p id="code-error" class="error" [class.show]="codeError()" role="alert">{{ codeError() }}</p>
            <button class="du-btn du-btn-primary" id="code-btn" type="submit" [disabled]="busy()">
              {{ busy() ? 'Checking…' : 'Watch' }}
            </button>
          </form>
          <p class="divider">Already have an account? <a href="/login">Sign in</a></p>
        </section>
      }
      @case ('choice') {
        <section id="state-choice">
          <p class="ok">Code accepted.</p>
          <p class="sub">You're in. Head straight to the stream, or make an account first — it's optional, and you can always do it later.</p>
          <a class="action du-btn du-btn-primary" id="go-watch" href="/">Watch the live stream →</a>
          <button class="quiet du-btn" id="go-register" type="button" (click)="show('register')">
            Create an account — unlocks the on-demand library
          </button>
        </section>
      }
      @case ('register') {
        <section id="state-register">
          <p class="sub">Create an account to browse the library and watch anything on demand, privately.</p>
          <form id="register-form" (submit)="register($event)">
            <label for="username">Username</label>
            <input id="username" name="username" type="text" autocomplete="username"
                   autocapitalize="none" autocorrect="off" spellcheck="false" required
                   [value]="username()" (input)="username.set(val($event))">
            <p class="hint">3–32 characters: lowercase letters, numbers, and <code>_ . -</code></p>
            <label for="password">Password</label>
            <input id="password" name="password" type="password" autocomplete="new-password" required
                   [value]="password()" (input)="password.set(val($event))">
            <p class="hint">At least 8 characters.</p>
            <label for="password2">Confirm password</label>
            <input id="password2" name="password2" type="password" autocomplete="new-password" required
                   [value]="password2()" (input)="password2.set(val($event))">
            <p id="register-error" class="error" [class.show]="registerError()" role="alert">{{ registerError() }}</p>
            <button id="register-btn" type="submit" [disabled]="busy()">
              {{ busy() ? 'Creating…' : 'Create account' }}
            </button>
          </form>
          <p class="back"><button id="register-back" type="button" (click)="show('choice')">← Back</button></p>
        </section>
      }
    }
  `,
})
export class FrontDoorComponent {
  private readonly api = inject(InviteService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  protected readonly view = signal<View>('code');
  protected readonly busy = signal(false);

  protected readonly code = signal('');
  protected readonly codeError = signal('');

  protected readonly username = signal('');
  protected readonly password = signal('');
  protected readonly password2 = signal('');
  protected readonly registerError = signal('');

  /** The redeemed code is replayed on register — the server re-validates it,
   *  so this is a convenience, not a trust boundary. */
  private acceptedCode = '';

  constructor() {
    // Convenience only: the real ?t= invite flow is handled server-side before
    // this page ever renders, so prefill and let the visitor press Watch.
    const deepCode = new URLSearchParams(location.search).get('t');
    if (deepCode) this.code.set(deepCode);

    // Land focus on each state's first control, once it exists in the DOM.
    effect(() => {
      this.view();
      queueMicrotask(() => {
        this.host.nativeElement.querySelector<HTMLElement>('input, a.action')?.focus();
      });
    });
  }

  protected val(e: Event): string {
    return (e.target as HTMLInputElement).value;
  }

  protected show(view: View): void {
    this.codeError.set('');
    this.registerError.set('');
    this.view.set(view);
  }

  protected async redeem(e: Event): Promise<void> {
    e.preventDefault();
    this.codeError.set('');
    const code = this.code().trim();
    if (!code) {
      this.codeError.set('Enter your friend code.');
      return;
    }
    this.busy.set(true);
    const r = await this.api.redeem(code);
    this.busy.set(false);
    if (r.kind === 'ok') {
      this.acceptedCode = code;
      if (r.body['can_register']) {
        this.show('choice');
      } else {
        location.href = '/';
      }
      return;
    }
    this.codeError.set(describe(r, {
      401: "That code isn't valid.",
      429: 'Too many tries — wait a minute.',
    }, 'Something went wrong — try again.'));
  }

  protected async register(e: Event): Promise<void> {
    e.preventDefault();
    this.registerError.set('');
    const username = this.username().trim().toLowerCase();
    const password = this.password();
    if (!USERNAME_RE.test(username)) {
      this.registerError.set('Username must be 3–32 characters, using letters, numbers, _ . or - only.');
      return;
    }
    if (password.length < 8) {
      this.registerError.set('Password must be at least 8 characters.');
      return;
    }
    if (password !== this.password2()) {
      this.registerError.set("Passwords don't match.");
      return;
    }
    this.busy.set(true);
    const r = await this.api.register(this.acceptedCode, username, password);
    this.busy.set(false);
    if (r.kind === 'ok') {
      location.href = '/home';
      return;
    }
    if (r.kind === 'status' && r.status === 400) {
      const err = r.body['error'];
      if (err === 'bad_username' || err === 'bad_password') {
        const msg = r.body['message'];
        this.registerError.set(typeof msg === 'string' && msg ? msg : "That won't work — try something else.");
        return;
      }
    }
    this.registerError.set(describe(r, {
      409: 'That username is taken.',
      403: "That code can't create accounts.",
      401: "That code isn't valid.",
      429: 'Too many tries — wait a minute.',
    }, "Couldn't create the account — try again."));
  }
}

/** Maps a failed door result to user-facing copy. */
function describe(r: DoorResult, byStatus: Record<number, string>, fallback: string): string {
  if (r.kind === 'network') return 'Network error — try again.';
  if (r.kind === 'status') return byStatus[r.status] ?? fallback;
  return fallback;
}
