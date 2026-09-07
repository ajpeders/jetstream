import { provideHttpClient, withFetch } from '@angular/common/http';
import { provideZonelessChangeDetection } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';

import { ChatPanelComponent } from './chat-panel.component';
import { CustomReactionsPanelComponent } from './custom-reactions-panel.component';
import { MediaRequestsPanelComponent } from './media-requests-panel.component';
import { QueueRequestsPanelComponent } from './queue-requests-panel.component';
import { UsersPanelComponent } from './users-panel.component';
import { ReportsPanelComponent } from './reports-panel.component';
import { VodSessionsPanelComponent } from './vod-sessions-panel.component';
import { ViewersPanelComponent } from './viewers-panel.component';

/* The viewer list is a host-only concern. /controls serves the same admin.html
 * to friends with the host-only panels hidden, so bootstrapping here would
 * start a 5s poll against an endpoint a friend is not allowed to call. The old
 * inline script gated the same way with `if (IS_ADMIN)`.
 *
 * The element check mirrors the Svelte islands' `if (target)` guard — the
 * bundle is loaded by a page it does not own, so it must no-op quietly when
 * its mount point is absent. */
const isAdmin = !location.pathname.startsWith('/controls');

const providers = [provideZonelessChangeDetection(), provideHttpClient(withFetch())];

/* Each panel is bootstrapped independently rather than under one root, so a
 * panel can be ported (or reverted) without touching the others, and so the
 * page keeps working if one of them is absent from the markup. */
if (isAdmin) {
  for (const [selector, component] of [
    ['jet-viewers-panel', ViewersPanelComponent],
    ['jet-reports-panel', ReportsPanelComponent],
    ['jet-vod-sessions-panel', VodSessionsPanelComponent],
    ['jet-custom-reactions-panel', CustomReactionsPanelComponent],
    ['jet-queue-requests-panel', QueueRequestsPanelComponent],
    ['jet-media-requests-panel', MediaRequestsPanelComponent],
    ['jet-users-panel', UsersPanelComponent],
    ['[jet-chat-panel]', ChatPanelComponent],
  ] as const) {
    if (!document.querySelector(selector)) continue;
    bootstrapApplication(component, { providers }).catch((err: unknown) =>
      console.error(err),
    );
  }
}
