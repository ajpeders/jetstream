import { provideZonelessChangeDetection } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';

import { FrontDoorComponent } from './front-door.component';

/* The logged-out surfaces: the front door (home.html, served as the 401 body
 * at "/") and, once ported, /login. Unlike the viewer and admin bundles this
 * one is deliberately NOT token-gated — its page is what an anonymous visitor
 * sees, so a gated bundle would leave the form dead. Keep it to what those
 * pages already showed the world: nothing in here may name an authenticated
 * endpoint. No HttpClient provider on purpose — the pages use two POSTs and
 * the bundle should stay as small as the inline script it replaced. */
const providers = [provideZonelessChangeDetection()];

for (const [selector, component] of [
  ['[jet-front-door]', FrontDoorComponent],
] as const) {
  if (!document.querySelector(selector)) continue;
  bootstrapApplication(component, { providers }).catch((err: unknown) =>
    console.error(err),
  );
}
