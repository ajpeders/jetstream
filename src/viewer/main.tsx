import { mountIslands } from '@/lib';

/* Viewer islands. The player itself stays hand-written in viewer.html — hls.js,
 * the video element and fullscreen are commanded imperatively and gain nothing
 * from being driven declaratively. What lives here are the data panels around
 * it, registered as [selector, Component] pairs keyed on the [jet-*]
 * attributes viewer.html already carries. */
mountIslands([]);
