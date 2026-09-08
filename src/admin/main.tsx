import { mountIslands } from '@/lib';
import { AdminQueue } from './AdminQueue';
import { AdminRecent } from './AdminRecent';
import { ChatPanel } from './ChatPanel';
import { CustomReactionsPanel } from './CustomReactionsPanel';
import { MediaRequestsPanel } from './MediaRequestsPanel';
import { QueueRequestsPanel } from './QueueRequestsPanel';
import { ReportsPanel } from './ReportsPanel';
import { UsersPanel } from './UsersPanel';
import { ViewersPanel } from './ViewersPanel';
import { VodSessionsPanel } from './VodSessionsPanel';

/* Admin islands. Ported panels register here as [selector, Component] pairs
 * keyed on the tags admin.html already carries (e.g. 'jet-viewers-panel'). */
mountIslands([
  ['#admin-queue-app', AdminQueue],
  ['#admin-recent-app', AdminRecent],
  ['jet-queue-requests-panel', QueueRequestsPanel],
  ['jet-media-requests-panel', MediaRequestsPanel],
  ['jet-reports-panel', ReportsPanel],
  ['jet-custom-reactions-panel', CustomReactionsPanel],
  ['jet-viewers-panel', ViewersPanel],
  ['jet-users-panel', UsersPanel],
  ['jet-vod-sessions-panel', VodSessionsPanel],
  ['[jet-chat-panel]', ChatPanel],
]);
