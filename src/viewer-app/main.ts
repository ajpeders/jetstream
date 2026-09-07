import { provideHttpClient, withFetch } from '@angular/common/http';
import { provideZonelessChangeDetection } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';

import { LibraryPanelComponent } from './library-panel.component';
import { QueuePanelComponent } from './queue-panel.component';
import { RequestsPanelComponent } from './requests-panel.component';

/* The player itself stays hand-written in viewer.html — hls.js, the video
 * element and fullscreen are commanded imperatively and gain nothing from being
 * driven declaratively. What moves here are the data panels around it — the
 * read-only queue, the request list with its votes, and the library browser
 * that files those requests.
 *
 * Guarded like the Svelte islands: this bundle is loaded by a page it does not
 * own, so it no-ops quietly when its mount point is absent. */
const providers = [provideZonelessChangeDetection(), provideHttpClient(withFetch())];

/* Each panel bootstraps independently rather than under one root, matching the
 * admin bundle: a panel can be ported (or reverted) without touching the
 * others, and the page keeps working if one is absent from the markup.
 *
 * The cost of that choice is real and worth stating: every island gets its own
 * root injector, so `providedIn: 'root'` is per-island, not per-page. Two panels
 * cannot share a service instance or a poll — which is why the library panel
 * nudges the request list through window.jetstream rather than through DI. */
for (const [selector, component] of [
  ['[jet-queue-panel]', QueuePanelComponent],
  ['[jet-requests-panel]', RequestsPanelComponent],
  ['[jet-library-panel]', LibraryPanelComponent],
] as const) {
  if (!document.querySelector(selector)) continue;
  bootstrapApplication(component, { providers }).catch((err: unknown) =>
    console.error(err),
  );
}
