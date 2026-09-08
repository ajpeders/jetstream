/**
 * Path→label logic for the viewer's library browser, ported verbatim from the
 * hand-written panel in viewer.html.
 *
 * The server hands back raw relative paths, and the shape of the library is a
 * convention rather than a schema: a top-level "tv" or "movies" directory, and
 * the synthetic "__season__/<n>" segment the viewer API injects to group a
 * show's episodes. Everything here is the client-side reading of that
 * convention, kept pure so it can be reasoned about without a DOM.
 */

/** Trailing path segment, tolerating a null/empty path. */
export function basename(p: string | null | undefined): string {
  return (p || '').split('/').pop() ?? '';
}

/** The synthetic segment the viewer API uses to group episodes by season. */
const SEASON_MARKER = '__season__';

/**
 * Breadcrumb label for a browse path: "TV Shows / The Show / Season 2".
 *
 * The season marker consumes the segment after it (the number), which is why
 * this is an index loop rather than a map — "__season__/2" is one label, not
 * two, and "__season__/0" is Specials.
 */
export function libraryLabel(path: string): string {
  if (!path) return 'Library';
  const parts = path.split('/').filter(Boolean);
  const labels: string[] = [];
  for (let idx = 0; idx < parts.length; idx++) {
    const part = parts[idx]!;
    const low = part.toLowerCase();
    if (low === SEASON_MARKER) {
      const raw = parts[idx + 1];
      const season = Number(raw);
      labels.push(
        season === 0 ? 'Specials' : `Season ${Number.isFinite(season) ? season : raw}`,
      );
      idx++;
      continue;
    }
    if (idx === 0 && low === 'tv') labels.push('TV Shows');
    else if (idx === 0 && (low === 'movie' || low === 'movies')) labels.push('Movies');
    else labels.push(part);
  }
  return labels.join(' / ');
}

/**
 * One level up. A season is two segments ("__season__/2"), so leaving one pops
 * both — otherwise Back would land on a marker path that means nothing.
 */
export function parentLibraryPath(path: string): string {
  const parts = (path || '').split('/').filter(Boolean);
  if (parts.length >= 2 && parts[parts.length - 2]?.toLowerCase() === SEASON_MARKER) {
    parts.splice(parts.length - 2, 2);
  } else {
    parts.pop();
  }
  return parts.join('/');
}

/** Parent path of a file, for the dimmed subtitle under its name. */
export function requestParent(path: string): string {
  return path && path.includes('/') ? path.substring(0, path.lastIndexOf('/')) : '';
}

export interface LibraryItem {
  name: string;
  path: string;
  type: string;
  size?: number | null;
  /** Set by the server on a configured viewer root, whose label is authored in
   *  VIEWER_LIBRARY_ROOTS and so must win over any name we'd infer. */
  viewer_root?: boolean;
}

function pathParts(item: LibraryItem): string[] {
  return (item.path || item.name || '').toLowerCase().split('/').filter(Boolean);
}

/** A movie's own folder — "movies/<title>", not the "movies" root itself. */
export function isMovieDirectory(item: LibraryItem): boolean {
  const parts = pathParts(item);
  return (parts[0] === 'movies' || parts[0] === 'movie') && parts.length >= 2;
}

/** A show's folder — "tv/<show>", the level that has cover art worth showing. */
export function isTvShowDirectory(item: LibraryItem): boolean {
  const parts = pathParts(item);
  return parts[0] === 'tv' && parts.length === 2;
}

/** Display name for a directory row: the authored root label where there is
 *  one, else the friendly spelling of the two well-known top-level folders. */
export function friendlyDirectoryName(item: LibraryItem): string {
  if (item.viewer_root) return item.name || basename(item.path);
  const low = (item.path || item.name || '').toLowerCase();
  if (low === 'tv' || low.endsWith('/tv')) return 'TV Shows';
  if (
    low === 'movie' ||
    low === 'movies' ||
    low.endsWith('/movie') ||
    low.endsWith('/movies')
  ) {
    return 'Movies';
  }
  return item.name || basename(item.path);
}

/** The word shown in a folder tile before (or instead of) cover art. */
export function directoryIcon(item: LibraryItem): string {
  const parts = pathParts(item);
  const path = parts.join('/');
  if (path === 'movies' || path === 'movie') return 'Movies';
  if (path === 'tv') return 'TV';
  if (isMovieDirectory(item)) return 'Movie';
  if (parts[0] === 'tv' && parts.length === 2) return 'Show';
  if (parts[0] === 'tv' && parts.length >= 3) return 'Season';
  return 'Folder';
}

/** Cover art for a directory tile, or null where a folder has none worth
 *  fetching (the roots, and season folders). */
export function directoryPoster(item: LibraryItem): string | null {
  return (isMovieDirectory(item) || isTvShowDirectory(item)) && item.path
    ? posterUrl(item.path)
    : null;
}

export function posterUrl(path: string): string {
  return `/poster?path=${encodeURIComponent(path)}`;
}
