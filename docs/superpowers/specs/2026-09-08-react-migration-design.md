# React migration — design

**Status:** approved 2026-09-08. Supersedes the Angular migration (branches
`angular-migration`, `angular-home`).

## Decision

Replace every Angular component and every Svelte island with React, drop both
toolchains, and keep the islands-in-Flask-shells page model. Parity first: no
user-visible change until the swap is complete, then the remaining inline pages
(library, admin controls, viewer player, login, hub) are converted in React.

Why React over the Angular port already under way: Angular assumes it owns the
page, and the islands pattern fights that (one injector per island, panels
talking through `window.jetstream`). React's `createRoot` per island is the
natural fit, it is what the rest of the streaming world ships, and the bundle
for a form-sized page is smaller. Why not Svelte, which was already here and is
the best fit for islands on paper: the owner chose React; ecosystem and
familiarity beat a few kB.

Styling is out of scope. React adopts the existing markup, ids and classes
exactly as the Angular components did, so `static/css/*.css` and the theme
match unchanged. The daisyUI restyle stays a separate job.

## Toolchain

- Vite (already present) + `@vitejs/plugin-react`, React 19, TypeScript.
- Removed: `@angular/*`, `svelte`, `@sveltejs/vite-plugin-svelte`,
  `angular.json`, `tsconfig.app.json`, `tsconfig.viewer.json`,
  `tsconfig.public.json`. `tsconfig.json` becomes a normal React/Vite config
  with `jsx: react-jsx`; the `@jet/ui` alias is replaced by `@/lib`.
- `npm run build` = `build:theme && vite build && vite build --config
  vite.public.config.js`.

## Bundles and gating

| Entry | Output | Served as | Gate |
|---|---|---|---|
| `src/admin/main.tsx` | `static/build/admin.js` (+ shared `chunks/`) | `/build/admin.js` | token / basicauth, as `/build/` is today |
| `src/viewer/main.tsx` | `static/build/viewer.js` (+ shared `chunks/`) | `/build/viewer.js` | same |
| `src/public/main.tsx` | `static/build-public/main.js`, fully inlined | `/build-public/main.js` | **none, by design** |

The public bundle is a separate Vite config because it must not depend on a
shared chunk under the gated `/build/` path. The rule from the Angular version
carries over verbatim: nothing that names an authenticated endpoint may be
imported under `src/public/`.

`static/build-admin/` and `static/build-viewer/` cease to exist; the Dockerfile
copies `build/` and `build-public/` only.

## Shared code: `src/lib/`

Replaces `src/ui/`, same responsibilities, as hooks and plain functions:

| Export | Replaces | Contract |
|---|---|---|
| `usePolled(fetcher, {intervalMs, pauseWhenHidden})` | `polled()` | returns `{data, refresh}`; a failed tick keeps the last good value |
| `useActions()` | `actions()` | `{busy(key), run(key, fn)}` for per-button in-flight state |
| `useCollapse(id)` | `collapseState()` | persisted collapsed flag per panel |
| `publishRefresh(name)` / `useRefreshSignal(name)` | `bridge.ts` | cross-island nudge via `window.jetstream` |
| `clientSid()`, `formatTime()`, `fmtAgo()`, `toast()` | same | unchanged behaviour |
| `PosterThumb` | `PosterThumbComponent` | same props |

Islands may only import from `src/lib/`. Nothing in `src/lib/` may import from
an island directory.

## Islands

One `.tsx` per panel under `src/admin/`, `src/viewer/`, `src/public/`;
services become plain fetch modules beside them. Each page entry does:

```ts
for (const [selector, Component] of islands)
  for (const el of document.querySelectorAll(selector))
    createRoot(el).render(<Component />);
```

keyed on the same custom-element tags and `[jet-*]` attributes the shells
already carry. A shell that lacks a mount point simply gets no island.

## Tests

`tests/test_admin_island.py`, `test_viewer_island.py`, `test_home_island.py`
keep their assertions; only bundle paths change (`/build/admin.js`,
`/build/viewer.js`, `/build-public/main.js`). The gate tests that prove
`/build/` stays gated and `/build-public/` does not are unchanged.

## Work split

Split by directory so two agents never edit the same file.

1. **Agent A (this session), first:** toolchain swap, `src/lib/`, public
   front door, gate, tests, pushed as branch `react-migration`.
2. **Agent B, after 1:** admin side — 8 Angular components + `AdminQueue` and
   `AdminRecent` Svelte islands → `src/admin/*.tsx`; `admin.html` script
   tags; `test_admin_island.py` green; browser-checked on the dev server.
   Does not touch `src/lib/`, viewer, public, configs, `app.py`, docs.
   Missing helper → report, don't add.
3. **Agent A, parallel with 2:** viewer side — 3 Angular panels +
   `ViewerChat.svelte` → `src/viewer/*.tsx`; `viewer.html`;
   `test_viewer_island.py`.
4. **Agent A, last:** delete Angular/Svelte sources and packages, Dockerfile,
   README/ARCHITECTURE/ROADMAP/HOWTO, merge to main.

Done when: `npm run build` is Tailwind + two Vite builds, no `@angular` or
`svelte` in `package.json`, all tests pass, every page renders its panels on
the dev server with a clean console.
