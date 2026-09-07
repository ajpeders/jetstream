/**
 * @jet/ui — shared Angular pieces for the jetstream front end.
 *
 * Consumed by both apps in this workspace (src/admin-app, src/viewer-app) via
 * the "@jet/ui" path alias in tsconfig.json. Deliberately a source library, not
 * an ng-packagr build: nothing outside this repo consumes it, so a separate
 * build step and its dist/ would buy nothing, and importing the source lets
 * each app's bundler tree-shake what it doesn't use.
 *
 * Everything here earned its place by already existing two or more times in
 * the hand-written pages this front end is replacing.
 */
export { actions, toast, type Actions } from './actions';
export { publishRefresh } from './bridge';
export { collapseState } from './collapse';
export { polled, pollingStream, type Polled, type PollingStream } from './polling';
export { clientSid } from './sid';
export { formatTime, fmtAgo } from './time';
export { PosterThumbComponent } from './poster-thumb.component';
