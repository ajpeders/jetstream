import { mountIslands } from '@/lib';

import { ChatPanel } from './chat-panel';
import { LibraryPanel } from './library-panel';
import { QueuePanel } from './queue-panel';
import { RequestsPanel } from './requests-panel';

/* The player itself stays hand-written in viewer.html — hls.js, the video
 * element and fullscreen are commanded imperatively and gain nothing from
 * being driven declaratively. What lives here are the panels around it,
 * keyed on the [jet-*] attributes and ids viewer.html already carries.
 *
 * Each island is its own root, so panels cannot share state through context.
 * The refresh nudges between them (library → requests → queue) go through
 * window.jetstream; see src/lib/bridge.ts. */
mountIslands([
  ['[jet-queue-panel]', QueuePanel],
  ['[jet-requests-panel]', RequestsPanel],
  ['[jet-library-panel]', LibraryPanel],
  ['#chat-app', ChatPanel],
]);
