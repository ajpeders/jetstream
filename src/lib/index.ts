/**
 * @/lib — shared pieces for the jetstream React islands.
 *
 * Consumed by src/admin, src/viewer and src/public via the "@/lib" alias.
 * Deliberately a source library: nothing outside this repo consumes it, and
 * importing the source lets each entry tree-shake what it doesn't use.
 *
 * Islands may import from here; nothing here may import from an island.
 * Everything here earned its place by already existing two or more times in
 * the hand-written pages this front end is replacing.
 */
export { useActions, toast, type Actions } from './actions';
export { publishRefresh, usePublishRefresh } from './bridge';
export { useCollapse } from './collapse';
export { mountIslands, type IslandProps } from './mount';
export { useHostClass } from './host-class';
export { usePolled, usePollTick, getJson, type Polled } from './polling';
export { clientSid } from './sid';
export { formatTime, fmtAgo } from './time';
export { PosterThumb } from './poster-thumb';
