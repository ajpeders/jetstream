import { MouseEvent, useCallback, useEffect, useRef, useState } from 'react';

import { IslandProps, PosterThumb, clientSid, useCollapse, useHostClass } from '@/lib';

import {
  LibraryItem,
  directoryIcon,
  directoryPoster,
  friendlyDirectoryName,
  libraryLabel,
  parentLibraryPath,
  posterUrl,
  requestParent,
} from './library-paths';
import { browse, request, search } from './library';

interface Row {
  item: LibraryItem;
  isDir: boolean;
  /** Dimmed second line under the name — a parent path, or a breadcrumb. */
  subtitle: string;
}

interface ViewState {
  rows: Row[];
  /** Shown as the single `.empty` row when there is nothing to list. */
  message: string;
  /** Shown after the rows when the list is real but incomplete. */
  note: string;
}

/** How far a Request has got. Drives the button's label and disabled state the
 *  way the hand-written panel drove `btn.textContent` directly. */
type ReqState = 'busy' | 'done' | 'rate' | 'fail';

const DEBOUNCE_MS = 250;
const RESET_MS = 1800;

const placeholder = (message: string): ViewState => ({ rows: [], message, note: '' });

/** Mounts into the page's <section id="request-panel" jet-library-panel>. */
export function LibraryPanel({ host }: IslandProps) {
  /** Expanded by default, unlike the queue and requests panels — this is the
   *  panel a viewer came to use. Same key the hand-written version wrote. */
  const [collapsed, setCollapsed] = useCollapse('jetstream_request_collapsed', false);
  useHostClass(host, 'collapsed', collapsed);

  /** Current directory. Empty string is the roots listing. */
  const [path, setPath] = useState('');
  const [label, setLabel] = useState(libraryLabel(''));
  const [query, setQuery] = useState('');
  const [view, setView] = useState<ViewState>(() => placeholder('Loading library…'));
  /** Directory posters that have decoded, so the placeholder word can go. */
  const [loaded, setLoaded] = useState<ReadonlySet<string>>(() => new Set());
  const [reqStates, setReqStates] = useState<Record<string, ReqState>>({});

  /**
   * Staleness in one place: every navigation bumps `seq`, and a result only
   * lands if its seq is still current. That covers both an in-flight request
   * and a still-pending search debounce — the RxJS version got the same
   * guarantee from `switchMap`, the hand-written one needed a counter *and* a
   * clearTimeout.
   */
  const seq = useRef(0);
  const debounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const run = useCallback((loading: string, load: () => Promise<ViewState>, failed: string) => {
    const mine = ++seq.current;
    setView(placeholder(loading));
    load()
      .catch(() => placeholder(failed))
      .then((next) => {
        if (mine === seq.current) setView(next);
      });
  }, []);

  const goTo = useCallback(
    (next: string) => {
      clearTimeout(debounce.current);
      setPath(next);
      setLabel(libraryLabel(next));
      setQuery('');
      run('Loading library…', async () => browseRows(await browse(next)), 'Library failed to load.');
    },
    [run],
  );

  useEffect(() => {
    goTo('');
    return () => clearTimeout(debounce.current);
  }, [goTo]);

  const goUp = () => {
    if (path) goTo(parentLibraryPath(path));
  };
  const open = (item: LibraryItem) => goTo(item.path || '');

  /** Clicks on the tile open it, except when they landed on its own button —
   *  which opens it too, and must not fire twice. */
  const onTileClick = (e: MouseEvent, item: LibraryItem) => {
    if ((e.target as HTMLElement | null)?.tagName === 'BUTTON') return;
    open(item);
  };

  const onSearch = (value: string) => {
    setQuery(value);
    const q = value.trim();
    clearTimeout(debounce.current);
    if (!q) {
      // Emptying the box returns to wherever the browse was.
      goTo(path);
      return;
    }
    setLabel('Search');
    // Bump now so a browse result still in flight can't land over the search.
    seq.current++;
    setView(placeholder('Searching…'));
    debounce.current = setTimeout(() => {
      run('Searching…', async () => searchRows(q, await search(q)), 'Search failed.');
    }, DEBOUNCE_MS);
  };

  const setReq = (p: string, s: ReqState | undefined) =>
    setReqStates((m) => {
      const next = { ...m };
      if (s) next[p] = s;
      else delete next[p];
      return next;
    });

  // --- the one write ----------------------------------------------------
  const onRequest = async (item: LibraryItem) => {
    setReq(item.path, 'busy');
    let name = '';
    try {
      name = localStorage.getItem('jetstream_chatname') ?? '';
    } catch {
      /* storage blocked — the host just sees an anonymous request */
    }
    const res = await request(item.path, name, clientSid());
    if (res.ok) {
      // Same confirmation whether it was new or folded into an existing
      // request — the viewer only needs to know it registered.
      setReq(item.path, 'done');
      // The requests panel is a separate island; see src/lib/bridge.ts.
      window.jetstream?.['requests']?.();
      return;
    }
    setReq(item.path, res.error === 'rate limited' ? 'rate' : 'fail');
    setTimeout(() => setReq(item.path, undefined), RESET_MS);
  };

  const buttonLabel = (p: string): string => {
    switch (reqStates[p]) {
      case 'done': return 'Requested ✓';
      case 'rate': return 'slow down';
      case 'fail': return 'failed';
      default: return 'Request';
    }
  };

  return (
    <>
      {/* A button, like #queue-head and #reqlist-head: the header toggles the
          panel, so it has to be reachable by keyboard. */}
      <button
        id="request-head"
        type="button"
        aria-expanded={!collapsed}
        aria-controls="request-results"
        onClick={() => setCollapsed(!collapsed)}
      >
        <span className="lh-label">library</span>
        <span className="toggle" aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
      </button>

      <input
        id="request-search"
        type="search"
        placeholder="Search movies and shows…"
        autoComplete="off"
        aria-label="Search the library"
        value={query}
        onChange={(e) => onSearch(e.target.value)}
      />

      <div id="library-nav">
        <button id="library-back" type="button" disabled={!path} onClick={goUp}>Back</button>
        <span id="library-path">{label}</span>
      </div>

      <ul id="request-results" className="library-grid">
        {view.rows.length === 0 && <li className="empty">{view.message}</li>}
        {view.rows.map((row) => {
          const p = row.item.path;
          if (row.isDir) {
            const isLoaded = loaded.has(p);
            const url = directoryPoster(row.item);
            return (
              <li key={p} onClick={(e) => onTileClick(e, row.item)}>
                <span className={'rthumb' + (isLoaded ? '' : ' folder')}>
                  {!isLoaded && directoryIcon(row.item)}
                  {url && (
                    // Present but transparent until decoded, then it replaces the
                    // placeholder word — the fade is .rthumb img.loaded in CSS.
                    <img
                      loading="lazy"
                      alt=""
                      src={url}
                      className={isLoaded ? 'loaded' : undefined}
                      onLoad={() => setLoaded((s) => new Set(s).add(p))}
                    />
                  )}
                </span>
                <span className="rtitle">
                  <span className="rt-name">{friendlyDirectoryName(row.item)}</span>
                  <span className="rt-parent">{row.subtitle}</span>
                </span>
                <button type="button" onClick={() => open(row.item)}>Open</button>
              </li>
            );
          }
          const st = reqStates[p];
          return (
            <li key={p}>
              <span className="rthumb"><PosterThumb src={p ? posterUrl(p) : null} /></span>
              <span className="rtitle">
                <span className="rt-name">{row.item.name || '(untitled)'}</span>
                <span className="rt-parent">{row.subtitle}</span>
              </span>
              <button
                type="button"
                className={st === 'done' ? 'done' : undefined}
                disabled={st === 'busy' || st === 'done'}
                onClick={() => void onRequest(row.item)}
              >
                {buttonLabel(p)}
              </button>
            </li>
          );
        })}
        {view.note && <li className="empty">{view.note}</li>}
      </ul>
    </>
  );
}

/** Directories first, then files — the server sorts within each group. */
function browseRows(items: LibraryItem[]): ViewState {
  const dirs = items.filter((i) => i.type === 'directory');
  const files = items.filter((i) => i.type === 'file');
  return {
    rows: [
      ...dirs.map((item) => ({ item, isDir: true, subtitle: item.path ? libraryLabel(item.path) : '' })),
      ...files.map((item) => ({ item, isDir: false, subtitle: requestParent(item.path) })),
    ],
    message: 'Nothing here.',
    note: '',
  };
}

function searchRows(q: string, r: { results: LibraryItem[]; truncated: boolean }): ViewState {
  return {
    rows: r.results.map((item) => ({
      item,
      isDir: false,
      subtitle: item.path?.includes('/') ? `↳ ${requestParent(item.path)}` : '',
    })),
    // Rendered as text, so the query needs no escaping here, where the
    // hand-written version had to call escapeHtml.
    message: `No matches for “${q}”`,
    // The server caps the result set and says so; the old panel dropped the
    // flag, which made a capped list look like the whole library.
    note: r.truncated ? 'Showing the first matches only — narrow your search.' : '',
  };
}
