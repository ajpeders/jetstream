import type { SyntheticEvent } from 'react';

/**
 * Cover art that fades in once decoded. The pages all do this by hand — set
 * `src`, listen for load, add a `loaded` class that flips opacity — because a
 * poster that pops in at full opacity while a list is still settling reads as
 * a flicker. `src` may be null (URL and yt-dlp queue items have no artwork),
 * in which case the empty box is the intended state, not a broken image.
 *
 * The fade itself belongs to the consuming page's CSS, which already styles
 * `img` and `img.loaded` inside its own thumb container.
 */
export function PosterThumb({ src }: { src: string | null | undefined }) {
  const onLoad = (e: SyntheticEvent<HTMLImageElement>) => e.currentTarget.classList.add('loaded');
  return src ? <img loading="lazy" alt="" src={src} onLoad={onLoad} /> : <img loading="lazy" alt="" />;
}
