# jetstream architecture

```
                                       ┌─ /hls/*  (auth_request → Flask)
   media file / URL ──▶ ffmpeg (-re) ──▶ tmpfs /hls (shared volume)
                          │                       │
   composer thread ◀──────┤              ┌────────▼─────────┐
                          │              │  nginx sidecar   │──▶ Traefik ──▶ Hls.js / Safari
   watcher thread ◀───────┘              │  (sendfile)      │              (iPhone, desktop)
                                         └────────┬─────────┘
                                                  │ proxy /, /api/*, /admin/*, /chat/*
                                                  ▼
                                         Flask (gunicorn 16-thread)
                                         /api/_authcheck  /api/status
                                         /admin/*  /chat/*
```

One ffmpeg per source writes per-run fmp4 segments + a per-rendition idx playlist into `/hls/run/<id>/`. A composer thread stitches a global `/hls/stream.m3u8` across all runs (so viewers see one continuous HLS stream across queue advances, seeks, pauses). nginx fronts everything: serves `/hls/*` via sendfile after a Flask auth subrequest, proxies the rest.

## Server

### ffmpeg pipeline (`_build_ffmpeg_cmd`)

Input is per-source — file path or yt-dlp-resolved URL (separate audio URL on YouTube DASH). Filter chain branches on three independent decisions:

1. **HW decode** (`USE_VAAPI_DECODE=1`, source codec in HEVC/H.264/VP8/VP9/MPEG-2, source is SDR). On this iGPU, broken; pinned off by default. NVENC/NVDEC is enabled through the GPU compose overlay on hosts that support it.
2. **HDR source** (color_transfer in `smpte2084` PQ or `arib-std-b67` HLG) → CPU decode + zscale tonemap → BT.709 SDR.
3. **Subtitle burn-in** (file source has English-tagged text sub track via `_pick_default_subtitle`) → libavfilter `subtitles=` filter inserted after tonemap (if any) and before the scale.

Concrete shapes (one-line summaries; see `app.py` for the actual command):

```
# SDR file, no subs:                  hwupload-only chain
-vf scale=-2:<H>,format=nv12,hwupload

# SDR file with English subs:
-vf subtitles=…:si=N:force_style=…,scale=-2:<H>,format=nv12,hwupload

# HDR source (any kind):
-vf zscale=t=linear…,tonemap=hable,zscale=t=bt709…[,subtitles=…],scale=-2:<H>,format=nv12,hwupload

# YouTube DASH:
-i <video-url> -re -i <audio-url>  -map 0:v:0 -map 1:a:0  …
```

Other invariants across branches:
- `-re` paces input at native frame rate.
- `-map 0:v:0 -map 0:a:N? -sn` (drop subs from default mapping — bitmap PGS auto-mapping caused 1015% CPU once).
- `-c:v h264_vaapi -low_power 1 -rc_mode CQP -qp 23 -profile:v main` for VAAPI; `-c:v libx264 -preset veryfast -tune zerolatency` otherwise.
- `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709` always (forces correct SPS tagging — VAAPI omits these by default and Firefox's decoder then guesses).
- `-bsf:v h264_metadata=aud=insert` (Firefox's MP4 demuxer needs explicit Access Unit Delimiters to find frame boundaries inside fmp4 segments).
- `-force_key_frames "expr:gte(t,n_forced*HLS_SEG_TIME)"` so segments start on IDR frames.
- `-f hls -hls_time HLS_SEG_TIME -hls_list_size HLS_LIST_SIZE -hls_segment_type fmp4`.
- Output: `idx_av.m3u8` + `seg_av_NNNNN.m4s` + `init_av.mp4` in `/hls/run/<run_id>/`.

Pause is **kill-and-resume**: SIGSTOP doesn't work cleanly with `-re` (wall-clock advances while suspended; SIGCONT then burst-encodes to "catch up"). On resume, a new ffmpeg starts with `-ss <paused_position>`.

### Run directory + composer

Each ffmpeg launch (new source / seek / resume) increments `next_run_id` and gets its own `/hls/run/<id>/` directory. The composer thread (`_composer_tick`, ticks every `COMPOSER_TICK_S = 0.5`) stitches:

- All active+recent runs' `idx_av.m3u8` files into one global `/hls/stream.m3u8` with `#EXT-X-DISCONTINUITY` between runs.
- Running media-sequence and discontinuity-sequence so the player never sees a discontinuity on its end of the playlist (just a tag).
- Per-run `EXT-X-MAP` for fmp4 init segments.
- Per-segment `EXT-X-PROGRAM-DATE-TIME` (preserved from ffmpeg's per-run output) for cross-device sync.

Old segments roll off the front of the global window once total exceeds `HLS_LIST_SIZE`. A run's directory gets cleaned up after every one of its segments has rolled out AND the run is no longer active.

This is what makes queue advances / seeks / resumes seamless on the viewer side: the player is fetching a single never-changing URL, and the composer just appends new runs onto the playlist with discontinuity markers.

### State machine (locks all in `app.py`)

| Lock | Protects |
|---|---|
| `state_lock` | `current_proc`, `current_source`, `current_start_offset`, `current_start_time`, `current_paused`, `paused_position`, `active_run_id`, `next_run_id`, `finished_run_ids` |
| `_composer_state_lock` | composer's per-(run, segment) seq tables |
| `playlist_lock` | `playlist[]` |
| `settings_lock` | `settings{}` |
| `tokens_lock` | `tokens[]` |
| `viewers_lock` | `viewers{}`, `viewer_labels{}` |
| `chat_lock` | `chat_messages[]`, `chat_next_id`, `chat_rate{}` |
| `_library_cache_lock` | `_scan_library`'s mtime-invalidated cache |
| `users_lock` | `users{}` + `/data/users.json` writes |
| `user_sessions_lock` | login sessions + `/data/sessions.json` writes |
| `login_rate_lock` | per-IP login-attempt window |
| `vod_lock` | VOD session table (procs, run generations, last-fetch timestamps) |

### Watcher thread

`_watcher()` ticks every 1s. Single state machine:
1. If ffmpeg is alive → continue (and persist position every ~10 ticks).
2. If `current_paused` → continue (don't auto-anything while paused).
3. Pop next item from `playlist`. If empty + `auto_fill` is on → `_pick_random_from_library()` walks `/media` (cached scan, see Library section).
4. If we have an item → `_start_stream(item)`. On failure, sleep 4s (back-off).
5. Else → `_stop_locked()` + `_cleanup_hls()` (fully idle).

This is what gives the "always playing 24/7" behavior. Setting `auto_fill=false` is the only true off switch.

### State persistence (`STATE_FILE = /data/state.json`)

Saved on every `_start_stream`, every pause, every ~10 ticks of the watcher. Cleared on explicit stop. On startup `_restore_state_on_startup()` runs *before* the watcher starts. If the saved state was paused, restore re-enters paused state (terminate ffmpeg, set `current_paused=True`) so playback comes back at exactly the same frame. Saved sources include `subtitle_idx` so burn-in survives restarts.

### Auth flow (post-nginx)

```
viewer ──▶ nginx /hls/* ──▶ auth_request /__authcheck ──▶ Flask /api/_authcheck
             │                                              │
             │                                              ▼
             │                              200/204 OR 401 (token cookie /
             ▼                                  viewer_public check)
   sendfile from /hls tmpfs
```

The Flask endpoint also calls `_track_viewer()` so the active-viewers list and history keep working — nginx serving `/hls/*` directly means Flask never sees those requests, and the auth subrequest is the only Python touchpoint per segment fetch.

For non-`/hls` paths nginx just proxies through to Flask, which runs the existing `_gate_viewer_routes()` before-request hook.

### Users & sessions

Admin-created accounts only — no self-registration. Username + password, scrypt-hashed via stdlib `hashlib.scrypt` (no external deps), stored in `/data/users.json` under `users_lock`. Login at `/login`, rate-limited 5 attempts / 60 s per IP (`login_rate_lock`).

Sessions are **server-side**: opaque id in the `js_user` cookie (30 d), record in `/data/sessions.json` under `user_sessions_lock`. Deliberately distinct from the `lt` invite cookie — a browser can hold both. Revocation is real, not cookie-expiry-based: password reset, disable, and delete all drop the user's session records, so the cookie dies server-side immediately.

The request gate treats a valid `js_user` session as viewer-equivalent for the live stream (logged-in users can watch live) and as the sole key for the user-only surface (`/library`, `/api/user/*`, `/api/vod/*`). Invite tokens never unlock the user surface; the VOD auth check (below) keys ownership to the session, not the tier.

Admin CRUD: `GET/POST /admin/api/users`, `DELETE /admin/api/users/<id>`, `POST …/password`, `POST …/disabled`.

### Viewer tracking + logs

`_track_viewer()` runs from `/api/_authcheck`, keyed by `X-Forwarded-For` IP (forwarded by nginx). On *new* IP, fires `[viewer] connect ...` to stderr with token label + ipinfo.io geo + UA. Geo lookups happen on a daemon thread to avoid blocking. `_viewer_count()` prunes IPs idle >30s and emits `[viewer] disconnect ...`. Both the active list (`/admin/api/viewers`) and the JSONL log (`/data/viewer_log.jsonl`, exposed via `/admin/api/viewers/history`) include the resolved token label.

### Chat

Anonymous, in-memory only. Polling-based:
- `POST /chat/send` `{message, sid, name?}` — appends to a 200-message ring buffer with monotonic id. Per-IP rate limit: 10 messages / 30 s. 500-char cap on `message`, 24-char cap on `name` (control chars + zero-widths stripped). `name` defaults to `"anonymous"` when blank.
- `GET /chat/recent?since=N` — returns every message with id > N (so late joiners get up to 200 of history; existing viewers just get the delta).
- `sid` is opaque, generated client-side, stored in localStorage. Server uses it as a tag, never validates. Client renders a per-sid color dot so threads-of-conversation are visually trackable.
- `name` is a free-text display name set in a small input next to the chat send button — also localStorage-persisted, also unvalidated. The color dot stays tied to `sid`, so changing your name doesn't change your color.

Server restart wipes the chat — intentional, no persistence.

### Library scan cache

`_scan_library()` walks `MEDIA_ROOT` once, caches the file list keyed by `MEDIA_ROOT.stat().st_mtime`. Top-level mtime bumps (adding/removing a series or movie folder) trigger a rescan. Currently 26 ms cold, 0.03 ms warm on ~350 files.

### Tokens / gating

Per-friend invite tokens live in `tokens.json`. Admin mints with a label, viewer hits `?t=<token>`, server sets `lt` cookie (90d). All non-admin paths gated by `_gate_viewer_routes()` unless `settings.viewer_public=true`. Admin paths gated externally by Traefik basicauth on prod (no auth on dev — localhost-only).

**Two ways an account is born** (v2.1): the host creates one from the admin Users panel, or a holder of a **`friend`-level** invite code self-registers from the home screen (`POST /api/auth/register`). There is no open registration — the code is re-validated server-side on every signup rather than trusted from the preceding `/api/invite/redeem` call, so a client can't skip straight to registration. Codes are deliberately reusable (one link per household); each user record keeps `invited_by`/`invite_token` so a leaked link's accounts can all be found and removed. Signup budget is charged on accounts *created*, not on attempts — otherwise two mistyped passwords would lock a whole NAT'd household out for the hour; bad-code attempts are charged to the invite-guessing bucket instead.

**The front door** is `static/home.html`, served by the gate for unauthenticated `/` and `/controls` (it replaced an inline dead-end page that offered neither a code field nor a login link). Signed-in users land on `static/hub.html` at `/home` and choose live stream vs on-demand.

## VOD engine

Private Netflix-style on-demand playback for logged-in users, layered next to (not into) the live pipeline. The live stream is untouched.

### Session lifecycle

`POST /api/vod/start {path}` allocates a session id, spawns a dedicated ffmpeg writing HLS into `/hls/vod/<session_id>/`, records it in the session table under `vod_lock`. Constraints:
- **One session per user** — a new start replaces the user's existing session.
- **Global cap** `VOD_MAX_SESSIONS` (default 2) → `409 vod_capacity`. Cap exists because each session is a full encode.
- `GET /api/vod/status` reports the caller's session; admin sees all via `GET /admin/api/vod/sessions` and can kill any via `DELETE /admin/api/vod/sessions/<sid>`.

VOD ffmpeg differs from live on purpose:
- **No `-re`** — input paced by `-readrate` (`VOD_READRATE=2.0`), so the encoder builds buffer ahead of the viewer at 2× instead of crawling at realtime. Seeks and startup feel snappy without racing the disk.
- **No `-tune zerolatency`** — there's no live edge to chase.
- **Full playlist, nothing deleted** (`-hls_list_size 0`, no `delete_segments`, no `omit_endlist`): with the paced encoder running ahead of the 1× viewer, *any* rolling window eventually slides past the playhead and deletes the segment the player needs next — a deterministic mid-film stall. Instead the whole transcoded range stays on disk (back-seek within it is free and client-side) and ffmpeg writes `#EXT-X-ENDLIST` at completion so the player gets a real `ended`. Affordable because `/hls/vod` is a **disk-backed volume** (`jetstream-vod` in compose, nested over the `/hls` tmpfs and mounted in both containers) — bounded by `VOD_MAX_SESSIONS` × one film, the startup orphan sweep, and the idle reaper.
- `VOD_FORCE_CPU=1` optionally pins VOD to libx264, reserving the GPU's limited NVENC sessions for live + preroll.

### Seek model

Seek = kill ffmpeg + restart with `-ss <t>`, same as the live pause/seek philosophy — but the client is *not* on a composed continuous playlist, so each (re)start writes a fresh playlist generation and the client reloads the manifest with a `?g=` cache-buster. No composer involvement; a VOD session is one player on one private playlist.

### Idle reaper

A reaper thread watches per-session last-segment-fetch timestamps (updated from the VOD auth check, so nginx-served segments still count). No fetches for `VOD_IDLE_TIMEOUT_S` (120 s) → ffmpeg killed, `/hls/vod/<sid>/` removed, session dropped. Closed tabs can't pin an encoder or tmpfs space. `_cleanup_hls` (live idle cleanup) explicitly skips `/hls/vod/` so going live-idle doesn't nuke active VOD sessions.

### VOD auth (nginx)

```
user ──▶ nginx /hls/vod/<sid>/* ──▶ auth_request /__authcheck_vod ──▶ Flask /api/_authcheck_vod
                                       (passes X-Original-URI)          │
                                                                        ▼
                                                    js_user session must OWN <sid> → 200 / 403
```

Flask parses the sid out of `X-Original-URI` and checks the requesting `js_user` session owns that VOD session. Two deliberate differences from the live `/hls/` block:
- **No LAN bypass.** `/hls/` skips auth for LAN clients (living-room devices); `/hls/vod/` never does — private VOD is private even on the LAN.
- Ownership, not tier: a valid login isn't enough; the session must own the sid. Guessing another user's session id gets a 403.

## Client (admin.html, viewer.html)

### React islands (migration in progress)

The pages are still Flask-served HTML shells; what has moved is the code *inside* them. Vite builds one entry per page, and each island is a React function component that `mountIslands` (`src/lib/mount.tsx`) renders into an element the shell already carries — a custom tag like `<jet-viewers-panel>` or an attribute like `<section id="queue-panel" jet-queue-panel>` — so existing markup, ids and page CSS keep matching unchanged. One `createRoot` per island, not per page: a panel can be ported or reverted alone, and a shell that lacks a mount point simply gets no island.

Because a panel renders *into* a page-owned `<section>` whose chrome keys off `.collapsed` on the section itself, every island receives its `host` element and `useHostClass` toggles that class imperatively. Islands are separate roots, so they share no React context; the refresh nudges between them (library → requests → queue) go through `window.jetstream` (`src/lib/bridge.ts`).

| Entry | Bundle | Mounted in | Gated? |
|---|---|---|---|
| `src/admin/main.tsx` | `/build/admin.js` + shared `chunks/` | `admin.html` | Traefik basicauth |
| `src/viewer/main.tsx` | `/build/viewer.js` + shared `chunks/` | `viewer.html` | token cookie, like everything under `/build/` |
| `src/public/main.tsx` | `/build-public/main.js`, fully inlined | `home.html` (and `login.html` once ported) | **no — by design** |

`/build-public/` is the one bundle `_gate_viewer_routes` lets through anonymously: `home.html` *is* the 401 body, so gating its script would leave the front door with a dead form. It is a separate Vite config (`vite.public.config.js`) with no code splitting, so it can never reference a chunk under the gated `/build/`. The rule that keeps it safe is stated in `src/public/main.tsx`: nothing that names an authenticated endpoint may be imported there. Cost stated plainly: React + ReactDOM put it at ~62 kB gzipped, against ~6 kB for the inline script it replaced.

`src/lib/` is the only thing islands may import from: `usePolled` / `usePollTick` (interval polling that keeps the last good value; the tick variant folds deltas, which chat needs), `useActions` (per-button in-flight state + toast), `useCollapse` (the `jetstream_*_collapsed` localStorage convention), `usePublishRefresh`, `clientSid`, `formatTime`, `fmtAgo`, `PosterThumb`. `npm run typecheck` is the only TypeScript gate — Vite does not type-check.

Still hand-written: the HLS player in `viewer.html`, the file browser + playback controls in `admin.html`, all of `library.html`, `login.html`, `hub.html`.

Both pages share the same playback core. Differences: admin has scrub bar + queue UI + play/pause controls + chat panel; viewer has the player + position counter + chat panel.

### Player setup / teardown

```
softTeardown()   destroy Hls.js, leave <video> with last frame on screen
teardownPlayer() destroy Hls.js + clear video.src — only used when fully idle
setupPlayer()    new Hls.js (cache-buster on URL) → loadSource → attachMedia
```

iOS Safari path uses `?_=<Date.now()>` cache-busters because reusing the same URL is treated as a cached 404. Hls.js path also wires `MANIFEST_PARSED` → `snapToLive()` as a fallback for cases where `loadedmetadata` fired before the live edge was computable.

Hls.js fatal-error handler:
- If `serverPaused` → expected (manifest cleared); just drop hls and wait for the resume.
- Otherwise → softTeardown + retry `setupPlayer` after 800 ms.

### Sync (PDT-based)

Server emits `EXT-X-PROGRAM-DATE-TIME` for every segment + `server_unix` in `/api/status`. Client computes:

```
serverClockOffsetMs = s.server_unix * 1000 - Date.now()      # refreshed each poll
playingDateMs       = hls.playingDate                         # Hls.js
                     | video.getStartDate() + currentTime    # Safari native
lagS = (Date.now() + serverClockOffsetMs - playingDateMs) / 1000 - TARGET_LAG_S
```

Every 2s, if `|lagS| > 5` → `snapToLive()` (sets `currentTime = duration`). Otherwise leave alone. `playbackRate` is held at 1.0 — earlier attempts at continuous rate steering (1.05/0.95) caused audible pitch wobble on iOS.

Realistic floor: **~1.7 s** with the default `HLS_SEG_TIME=1`. A 4-s override raises sync lag to roughly 3-4 s and should be paired with player retuning. Sub-second sync would need real LL-HLS via `EXT-X-PART` (ffmpeg 7.1 doesn't emit it).

### iPhone specifics

- Fullscreen uses `video.webkitEnterFullscreen()` (iOS Safari can't fullscreen a `<div>`).
- PiP uses `video.webkitSetPresentationMode("picture-in-picture")` (`requestPictureInPicture` is unsupported).
- `playsinline` is set so video stays inline by default.
- Native HLS (`canPlayType("application/vnd.apple.mpegurl")`) is preferred over Hls.js when available.
- Spacebar-pause-prevent on the document only fires when no input/textarea is focused (so chat can take spaces).

## HLS specifics

- Segments live on a tmpfs named volume (`jetstream-hls`, 1.5 GiB) shared between Flask and nginx.
- Default segment time = 1 s, list size = 24 → 24 s playlist tuned for the current player live-edge constants.
- `omit_endlist` keeps clients in live mode.
- `delete_segments` rolls old segments off disk per-run; the composer cleans up an entire run dir once all its segments have aged out of the global window.
- `+program_date_time` is the linchpin for cross-device sync.
- `+independent_segments` declares each segment self-decodable (force_key_frames enforces this).

## VAAPI on this iGPU

Intel UHD 630 (`8086:3E92`, Coffee Lake). `vainfo` shows decode (VLD) for H.264, HEVC Main + Main10, VP8, VP9, MPEG-2. H.264 encode (EncSliceLP) works. **No `VAEntrypointVideoProc`** — meaning `scale_vaapi` cannot run, so the HW-decode path has to download frames to CPU memory for the scale step.

That works mechanically, but with `-f hls` + a VAAPI-decoded source, segment timestamps come out wrong: the muxer produces segments with ~5 minutes of content despite the playlist claiming 4 s. Same chain to `-f null /dev/null` runs at 0.995× realtime — so the bug is in HLS-muxer/timestamp interaction, not in decode/scale/encode. Workaround: `USE_VAAPI_DECODE=0` everywhere. A hardware path with NVIDIA NVDEC/NVENC sidesteps this.

## Network

- `web` Docker network (external) — Traefik front-door for prod.
- `jetstream-internal` bridge network — nginx ⇆ Flask hop, never leaves the docker daemon.
- Traefik routes go to `nginx-jetstream:80`; nginx then proxies to Flask or serves /hls direct.
- Friends connect from public internet via invite tokens. Admin routes are additionally protected by Traefik basicauth.

## Things that look weird but are deliberate

- Pause kills ffmpeg and retires the run. Composer keeps the (now-finished) run's segments in `/hls/stream.m3u8` until they roll out, so viewers' players sit on the existing live edge instead of seeing a 404'd manifest. On resume, a new run with `EXT-X-DISCONTINUITY` between.
- Player pane is always visible (no `display:none`) even when idle, so seek/pause/auto-advance can't collapse the layout. `aspect-ratio: 16/9 + object-fit: contain` keeps the box at fixed dimensions.
- `auto_fill` defaults to true. Stop button is mostly a "skip current; watcher will pick something else" unless `auto_fill=false`. To truly stop: flip the setting then stop.
- Admin's `broadcast-seek` slider posts to `/admin/api/seek`; it does NOT scrub the local `<video>` element. Seek is server-side (kill + restart ffmpeg with new `-ss`).
- Subtitle burn-in is implicit-on for files with English text subs. Pass `subtitle_idx: null` at queue/play time to opt out. URLs don't get subs.
- `docker cp app.py` to prod survives until the next image rebuild — always rebuild for app.py changes that need to stick.
