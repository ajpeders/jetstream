import { ChangeDetectionStrategy, Component, input } from '@angular/core';

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
@Component({
  selector: 'jet-poster-thumb',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (src(); as url) {
      <img loading="lazy" alt="" [src]="url" (load)="onLoad($event)" />
    } @else {
      <img loading="lazy" alt="" />
    }
  `,
  styles: `
    :host { display: contents; }
  `,
})
export class PosterThumbComponent {
  readonly src = input<string | null>(null);

  onLoad(e: Event): void {
    (e.target as HTMLImageElement).classList.add('loaded');
  }
}
