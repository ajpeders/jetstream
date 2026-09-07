import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Observable, Subject, concat, of, timer } from 'rxjs';
import { catchError, map, switchMap } from 'rxjs/operators';

import { PosterThumbComponent, clientSid, collapseState } from '@jet/ui';

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
import { LibraryService } from './library.service';

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

/** Browse a directory, or run a search — the two things the list can be doing. */
type Nav = { kind: 'browse'; path: string } | { kind: 'search'; q: string };

/** How far a Request has got. Drives the button's label and disabled state the
 *  way the hand-written panel drove `btn.textContent` directly. */
type ReqState = 'busy' | 'done' | 'rate' | 'fail';

const DEBOUNCE_MS = 250;
const RESET_MS = 1800;

@Component({
  // Attribute selector so the component adopts the page's existing
  // <section id="request-panel"> rather than nesting inside it. Worth keeping:
  // it renders children directly into the section, so both descendant and
  // child selectors in viewer.css keep matching.
  selector: '[jet-library-panel]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PosterThumbComponent],
  host: {
    '[class.collapsed]': 'collapsed()',
  },
  template: `
    <!-- A button, like #queue-head and #reqlist-head: the header toggles the
         panel, so it has to be reachable by keyboard. viewer.css carries the
         chrome opt-out for this id. -->
    <button
      id="request-head"
      type="button"
      [attr.aria-expanded]="!collapsed()"
      aria-controls="request-results"
      (click)="collapsed.set(!collapsed())"
    >
      <span class="lh-label">library</span>
      <span class="toggle" aria-hidden="true">{{ collapsed() ? '▸' : '▾' }}</span>
    </button>

    <input
      id="request-search"
      type="search"
      placeholder="Search movies and shows…"
      autocomplete="off"
      aria-label="Search the library"
      [value]="query()"
      (input)="onSearch($any($event.target).value)"
    />

    <div id="library-nav">
      <button id="library-back" type="button" [disabled]="!path()" (click)="goUp()">
        Back
      </button>
      <span id="library-path">{{ label() }}</span>
    </div>

    <ul id="request-results" class="library-grid">
      @for (row of view().rows; track row.item.path) {
        @if (row.isDir) {
          <!-- The whole tile opens the folder; the button is a visible
               affordance for the same action, so clicks on it are left to its
               own handler rather than counted twice. -->
          <li (click)="onTileClick($event, row.item)">
            <span class="rthumb" [class.folder]="!loaded().has(row.item.path)">
              @if (!loaded().has(row.item.path)) {
                {{ icon(row.item) }}
              }
              @if (poster(row.item); as url) {
                <!-- Present but transparent until decoded, then it replaces the
                     placeholder word — the fade is .rthumb img.loaded in CSS. -->
                <img
                  loading="lazy"
                  alt=""
                  [src]="url"
                  [class.loaded]="loaded().has(row.item.path)"
                  (load)="markLoaded(row.item.path)"
                />
              }
            </span>
            <span class="rtitle">
              <span class="rt-name">{{ dirName(row.item) }}</span>
              <span class="rt-parent">{{ row.subtitle }}</span>
            </span>
            <button type="button" (click)="open(row.item)">Open</button>
          </li>
        } @else {
          <li>
            <span class="rthumb">
              <jet-poster-thumb [src]="filePoster(row.item)" />
            </span>
            <span class="rtitle">
              <span class="rt-name">{{ row.item.name || '(untitled)' }}</span>
              <span class="rt-parent">{{ row.subtitle }}</span>
            </span>
            <button
              type="button"
              [class.done]="state(row.item.path) === 'done'"
              [disabled]="state(row.item.path) === 'busy' || state(row.item.path) === 'done'"
              (click)="request(row.item)"
            >
              {{ buttonLabel(row.item.path) }}
            </button>
          </li>
        }
      } @empty {
        <li class="empty">{{ view().message }}</li>
      }

      @if (view().note) {
        <li class="empty">{{ view().note }}</li>
      }
    </ul>
  `,
})
export class LibraryPanelComponent {
  private readonly api = inject(LibraryService);

  /** Expanded by default, unlike the queue and requests panels — this is the
   *  panel a viewer came to use. Same key the hand-written version wrote. */
  readonly collapsed = collapseState('jetstream_request_collapsed', false);

  /** Current directory. Empty string is the roots listing. */
  readonly path = signal('');
  readonly label = signal(libraryLabel(''));
  readonly query = signal('');

  /** Directory posters that have decoded, so the placeholder word can go. */
  readonly loaded = signal<ReadonlySet<string>>(new Set());

  private readonly reqStates = signal<Record<string, ReqState>>({});

  private readonly nav = new Subject<Nav>();

  /**
   * One stream for both modes, so `switchMap` is the whole staleness story: a
   * newer navigation cancels an in-flight request AND a still-pending search
   * debounce. The hand-written version needed a `requestSeq` counter and a
   * `clearTimeout` to get the same guarantee.
   */
  readonly view = toSignal(
    this.nav.pipe(
      switchMap((n) =>
        n.kind === 'browse'
          ? this.loadBrowse(n.path)
          : timer(DEBOUNCE_MS).pipe(switchMap(() => this.loadSearch(n.q))),
      ),
    ),
    { initialValue: { rows: [], message: 'Loading library…', note: '' } as ViewState },
  );

  constructor() {
    this.goTo('');
  }

  // --- navigation -------------------------------------------------------

  goTo(path: string): void {
    this.path.set(path);
    this.label.set(libraryLabel(path));
    this.query.set('');
    this.nav.next({ kind: 'browse', path });
  }

  goUp(): void {
    if (!this.path()) return;
    this.goTo(parentLibraryPath(this.path()));
  }

  open(item: LibraryItem): void {
    this.goTo(item.path || '');
  }

  /** Clicks on the tile open it, except when they landed on its own button —
   *  which opens it too, and must not fire twice. */
  onTileClick(e: Event, item: LibraryItem): void {
    if ((e.target as HTMLElement | null)?.tagName === 'BUTTON') return;
    this.open(item);
  }

  onSearch(value: string): void {
    this.query.set(value);
    const q = value.trim();
    if (!q) {
      // Emptying the box returns to wherever the browse was.
      this.goTo(this.path());
      return;
    }
    this.label.set('Search');
    this.nav.next({ kind: 'search', q });
  }

  // --- rendering helpers ------------------------------------------------

  icon = directoryIcon;
  dirName = friendlyDirectoryName;
  poster = directoryPoster;

  filePoster(item: LibraryItem): string | null {
    return item.path ? posterUrl(item.path) : null;
  }

  markLoaded(path: string): void {
    this.loaded.update((s) => new Set(s).add(path));
  }

  state(path: string): ReqState | undefined {
    return this.reqStates()[path];
  }

  buttonLabel(path: string): string {
    switch (this.state(path)) {
      case 'done':
        return 'Requested ✓';
      case 'rate':
        return 'slow down';
      case 'fail':
        return 'failed';
      default:
        return 'Request';
    }
  }

  // --- the one write ----------------------------------------------------

  async request(item: LibraryItem): Promise<void> {
    this.setState(item.path, 'busy');
    let name = '';
    try {
      name = localStorage.getItem('jetstream_chatname') ?? '';
    } catch {
      /* storage blocked — the host just sees an anonymous request */
    }

    const res = await this.api.request(item.path, name, clientSid());
    if (res.ok) {
      // Same confirmation whether it was new or folded into an existing
      // request — the viewer only needs to know it registered.
      this.setState(item.path, 'done');
      // The requests panel is a separate island with its own injector, so it
      // cannot be reached through DI. See src/ui/bridge.ts.
      window.jetstream?.['requests']?.();
      return;
    }
    this.setState(item.path, res.error === 'rate limited' ? 'rate' : 'fail');
    setTimeout(() => this.clearState(item.path), RESET_MS);
  }

  private setState(path: string, s: ReqState): void {
    this.reqStates.update((m) => ({ ...m, [path]: s }));
  }

  private clearState(path: string): void {
    this.reqStates.update((m) => {
      const next = { ...m };
      delete next[path];
      return next;
    });
  }

  // --- loading ----------------------------------------------------------

  /** Placeholder first, then the result — `concat` is what makes the "Loading"
   *  state part of the same cancellable stream as the request itself. */
  private loadBrowse(path: string): Observable<ViewState> {
    return concat(
      of<ViewState>({ rows: [], message: 'Loading library…', note: '' }),
      this.api.browse(path).pipe(
        map((items) => this.browseRows(items)),
        catchError(() =>
          of<ViewState>({ rows: [], message: 'Library failed to load.', note: '' }),
        ),
      ),
    );
  }

  private loadSearch(q: string): Observable<ViewState> {
    return concat(
      of<ViewState>({ rows: [], message: 'Searching…', note: '' }),
      this.api.search(q).pipe(
        map((r) => ({
          rows: r.results.map((item) => ({
            item,
            isDir: false,
            subtitle: item.path?.includes('/') ? `↳ ${requestParent(item.path)}` : '',
          })),
          // Interpolated, not innerHTML — the query needs no escaping here,
          // where the hand-written version had to call escapeHtml.
          message: `No matches for “${q}”`,
          // The server caps the result set and says so; the old panel dropped
          // the flag, which made a capped list look like the whole library.
          note: r.truncated ? 'Showing the first matches only — narrow your search.' : '',
        })),
        catchError(() => of<ViewState>({ rows: [], message: 'Search failed.', note: '' })),
      ),
    );
  }

  /** Directories first, then files — the server sorts within each group. */
  private browseRows(items: LibraryItem[]): ViewState {
    const dirs = items.filter((i) => i.type === 'directory');
    const files = items.filter((i) => i.type === 'file');
    return {
      rows: [
        ...dirs.map((item) => ({
          item,
          isDir: true,
          subtitle: item.path ? libraryLabel(item.path) : '',
        })),
        ...files.map((item) => ({
          item,
          isDir: false,
          subtitle: requestParent(item.path),
        })),
      ],
      message: 'Nothing here.',
      note: '',
    };
  }
}
