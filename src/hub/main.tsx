import { mountIslands } from '@/lib';

import { HubHeader } from './header';
import { HubMain } from './main-panel';

mountIslands([
  ['header[jet-hub-header]', HubHeader],
  ['main[jet-hub-main]', HubMain],
]);
