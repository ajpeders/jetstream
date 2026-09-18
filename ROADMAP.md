# jetstream roadmap

v1 + a viewer-interaction pass shipped. All tracked cleanup items closed. **v2 (accounts + private VOD) shipped to main and prod** (merge `25a05df`), followed by v2.1 (home screen + invite-gated signup, `a71c892`).

## v2 — shipped

v1's invite-token live stream is unchanged — v2 layers on top.

| What | Status | Notes |
|---|---|---|
| User accounts | shipped | Created by the host **or** by invite-gated self-registration (see v2.1). Username + password, scrypt-hashed (stdlib), `/data/users.json`; server-side sessions in `/data/sessions.json`, `js_user` cookie (30 d, distinct from `lt`), revoked on password reset / disable / delete. `/login` rate-limited 5/60 s per IP. Admin API: `GET/POST /admin/api/users`, `DELETE /admin/api/users/<id>`, `POST …/password`, `POST …/disabled`. Admin UI: Users panel (host-only, hidden on `/controls`). Logged-in users can also watch live. |
| Private VOD (Netflix-style) | shipped | `/library` grid + search (`/api/user/library/{browse,search}`) → private HLS under `/hls/vod/<sid>/`, ownership-checked via nginx `auth_request` → `/api/_authcheck_vod` (`X-Original-URI`; no LAN bypass, unlike `/hls/`). One session per user, global cap `VOD_MAX_SESSIONS` (2, `409 vod_capacity`). Seek = kill + restart with `-ss` (fresh playlist gen, `?g=` cache-buster). Idle reaper: `VOD_IDLE_TIMEOUT_S` (120 s) no-fetch → ffmpeg killed, dir removed. ffmpeg: `-readrate VOD_READRATE=2.0` (no `-re`), no zerolatency, full playlist on the disk-backed `jetstream-vod` volume (`-hls_list_size 0`, ENDLIST on completion), `VOD_FORCE_CPU=1` to spare NVENC. Routes: `POST /api/vod/{start,seek,stop}`, `GET /api/vod/status`; admin `GET/DELETE /admin/api/vod/sessions[/<sid>]` + Active VOD panel. |

New env: `USERS_FILE`, `USER_SESSIONS_FILE`, `VOD_MAX_SESSIONS`, `VOD_IDLE_TIMEOUT_S`, `VOD_READRATE`, `VOD_FORCE_CPU`. New locks: `users_lock`, `user_sessions_lock`, `login_rate_lock`, `vod_lock`. New pages: `static/login.html`, `static/library.html`. `_cleanup_hls` now skips `/hls/vod/`; nginx grows `location /hls/vod/` + `/__authcheck_vod`.

**Deploy requirement:** VOD segment dirs live on the disk-backed `jetstream-vod` volume mounted at `/hls/vod` in *both* containers — a bare restart won't create it. Deploy with `docker compose up -d --build`; without the volume, VOD segments land on the 1.5 GiB `/hls` tmpfs and a feature-length film fills it.

**Not exercised on GPU hardware yet:** NVENC session count under live + preroll + 2 concurrent VOD sessions (`VOD_FORCE_CPU=1` is the escape hatch), and an HDR source through the VOD path.

## v2.1 — shipped

| What | Status | Notes |
|---|---|---|
| Home screen | shipped | `static/home.html`, served by the gate for unauthenticated `/` and `/controls`. Two doors: type a friend code (`POST /api/invite/redeem` — the type-in twin of `?t=`, sets the same `lt` cookie) or sign in. Replaced an inline dead-end page that offered neither a code field nor a login link, so an account holder with no `lt` cookie had no reachable entry point at all. |
| Invite-gated signup | shipped | `POST /api/auth/register {code, username, password}` — requires a **`friend`-level** code (viewer codes stay watch-only, `403`). No open registration; the code is re-validated server-side rather than trusted from the redeem step. Codes are **reusable** (one link covers a household); each user records `invited_by`/`invite_token` so a leaked link's accounts can be found and removed. Auto-logs-in and keeps `lt` so live keeps working. A code alone still just watches — the account is an optional upgrade, so invite links already in the wild are unaffected. Rate limits: redeem 10/60 s per trusted IP; signup 5/hour charged on accounts **created**, not attempts (`_ip_rate_check(record=False)` peek) so mistyped passwords can't lock out a NAT'd household. |
| Post-login hub | shipped | `static/hub.html` at `/home` — pick live stream or my library; live card shows the now-playing title. Post-login default moved `/library` → `/home`. Viewer header gained "🎬 My library" / "Sign in" driven by the new `user` field in `/api/status`. |

Rate limiting is keyed on `_trusted_client_ip()` (nginx-set `X-Real-IP`), never the client-forgeable leftmost `X-Forwarded-For`.

## v2.2 — shipped

| What | Status | Notes |
|---|---|---|
| Continue watching | shipped | Per-user VOD resume. `{user_id: {path: {position, duration, updated, title}}}` in `/data/progress.json`, disk-flushed at most every `PROGRESS_SAVE_INTERVAL` (30 s) because progress arrives on every client's 5 s poll. The client reports the **absolute** playhead (`POST /api/vod/progress {session_id, position}`) — the server only knows `start_offset`, not where the player sits inside the transcoded range. `/api/vod/start` with **no** `start` key auto-resumes and returns `resumed_from`; an explicit `start: 0` forces from-the-top ("Start over" in the resume toast) — testing for key presence, not truthiness, is what keeps those two cases distinct. Row + dismiss via `GET/DELETE /api/user/continue`; `library.html` gets a horizontally-scrolling card strip with progress bars and remaining-time labels. Finishing clears the entry; deleting a user drops their history; entries capped at `PROGRESS_MAX_PER_USER` (100) and hidden when the file is no longer library-visible. |

**Threshold gotcha (found in test):** the "finished" tail and "too early to resume" floor are clamped to a fraction of runtime (10% / 5%) rather than flat 90 s / 30 s. With flat values, every position in a sub-90 s clip counted as finished and nothing short was ever resumable. Feature-length content keeps the flat numbers.

## v2.3 — shipped

| What | Status | Notes |
|---|---|---|
| Subtitle controls (VOD + live) | shipped | **VOD is genuinely per-viewer** — `GET /api/user/library/subtitles` lists burnable text tracks, the `CC` button in the `/library` player sends `subtitle_idx` to `/api/vod/start`, and switching restarts that user's own session at the current position. **Live is necessarily broadcast-wide** — `GET/POST {/admin,/api/control}/subtitles` switches the track for the current source by restarting ffmpeg in place (~2 s blip, `EXT-X-DISCONTINUITY`); the UI says so rather than implying a private toggle. |
| Self-service password change | shipped | `POST /api/auth/password {current_password, new_password}` on `/home`. Revokes every **other** session and re-issues the caller's, so it doubles as "sign out my other devices" without logging you out of the device in your hand. Wrong current-password charges the login rate bucket — the endpoint is an online guessing oracle otherwise. |
| `invited_by` in admin Users panel | shipped | Rendered as a chip per row. This is the field you need to clean up after a leaked friend code: find every account a token minted, delete the token, then delete those users. |

**Off-switch gotcha (found in test):** `_start_stream` deliberately re-picks a default English track whenever `subtitle_idx is None`, which made "off" instantly undo itself and left it *unreachable*. Needed an explicit `subtitles_off` sentinel on the source dict — scoped to the current source, so the next queue item auto-picks again.

**First-play delay is by design, not a bug:** the first play of any file+track runs *without* subtitles while a background job extracts the track to a small cached `.srt` (see #32); the next play burns them in. Both UIs now say this out loud instead of looking broken.

## v2.4 — UI verification pass (shipped)

Everything from v2 through v2.3 was built and tested headlessly, and the theme refresh (`62bd6c7`) landed underneath it — so no v2 page had ever been *looked at* in a browser. This pass drove every page through a real browser (local Flask + real ffmpeg + a generated fixture carrying English/French subtitle tracks) at 1280 / 390 / 360 px.

**Verified working:** home screen, `/login`, `/home` hub, `/library` browse + drill-down, the VOD player (duration probed, CC menu listing both tracks with the burn-in caveat), `/controls`, `/admin` (invite-provenance chip, Active VOD panel), and the viewer page — including the live subtitle round-trip against real ffmpeg: **off sticks** (the `subtitles_off` sentinel survives the restart), track switching applies, and each restart resumes at the live position rather than from zero.

Four defects found, all CSS — no backend change. Three fixed here; the fourth was fixed in parallel by the mobile-polish commit `576c8bc`:

| Bug | Cause |
|---|---|
| `<main>` painted pure black on all four v2 pages | `jetstream-theme.css` had an unscoped `main` in `#player, #player-wrap, main { background:#000 !important }` — written when jetstream was one page. v2 made it multi-page, so it reached `/home`, `/library`, `/login` and the home screen. Scoped to `body.viewer-page main`, matching the convention the sheet already uses further down. (The viewer was always winning `#020408` from that later rule, so this fix is viewer-neutral.) |
| Live subtitle menu opened off the top of the screen — "Off" and the first track unreachable | `#subs-menu` was anchored `bottom: calc(100% + …)`, copied from the VOD player's CC menu where the trigger sits in a *bottom* control bar. On `/controls` the trigger is in the top seek row. Now anchors `top:`. |
| Player control cluster pushed ~16 px past the right edge at 360 px (horizontal scrollbar on phones) | The mobile rule centred `#vol-controls` with `transform: translateX(-50%)`, but the theme's chrome-fade rules set `transform: translateY(6px)` at higher specificity. `transform` is one property, not a set — the centring was replaced, never combined, so the cluster hung off `left:50%` and was never actually centred. **Fixed independently by `576c8bc`**, which pins it `right: 0.7rem; transform: none !important` under `@media (max-width: 760px)` — right-anchoring can't overflow, and `transform:none` removes the collision at its source. Recorded here because the diagnosis is the durable part: *don't use `transform` for layout on an element whose `transform` is also animated.* |
| Chat empty-state ghosted through the sticky chat header | `#chat-header` is `position: sticky` but its gradient was 0.94–0.96 alpha, so anything scrolling under it showed through. Same gradient, now opaque. |

The last two are pre-existing viewer chrome rather than v2 work, found incidentally.

**Testing note:** bash `grep` silently returns nothing on `static/viewer.html` — it briefly looked like the 34 `body.viewer-page` theme rules were dead code, when line 1038 does carry `<body class="viewer-page">`. Use python/`rg` when searching that file.

**Still not verified — needs the GPU/deploy host:** NVENC session count under live + preroll + 2 concurrent VOD (`VOD_FORCE_CPU=1` is the escape hatch), and confirming the deploy actually created the `jetstream-vod` volume (`docker volume ls | grep vod`).

**HDR through the VOD path** is no longer completely untested: `bin/dev-setup.sh` generates a genuinely BT.2020/PQ-tagged fixture, and an end-to-end CPU run produced 22 segments with an empty ffmpeg log and the full zscale tonemap chain in both `live` and `vod` modes. What remains unverified is HDR *on the GPU* — NVDEC/NVENC tonemapping is a different branch of `_build_ffmpeg_cmd` than the CPU one this exercises.

## Local dev environment (shipped)

`bin/dev-setup.sh` + `bin/dev-server.py` — run the app outside Docker against `.devenv/`, with generated fixtures. Added because the harness for the v2.4 UI pass had to be hand-rebuilt from scratch, and three of its bugs were pure setup errors rather than app bugs. Two are encoded in the scripts so they can't recur:

- Token records key on **`id`**, not `token`. Seeding the wrong key makes `_valid_token` raise `KeyError` *inside the `before_request` gate*, so every route 500s — it reads like the app is broken rather than like bad fixture data.
- Fixture generation must never clobber existing files. An earlier harness rewrote its placeholders empty on each boot, silently replacing real video with 0 bytes; every playback test then failed with `EBML header parsing failed`.

A third is worth knowing generally: **lavfi sources emit frames with unspecified colour properties**, so `-color_trc`/`-color_primaries` output options are silently dropped and the file probes as SDR. HDR fixtures must be tagged with the `setparams` filter, and the script now verifies `color_transfer=smpte2084` after encoding rather than assuming it.

## v2.5 — VOD playback on iOS (shipped)

**Symptom:** on-demand playback did nothing on an iPhone — player chrome drawn, duration correct, permanently stuck on "tap to play". Desktop was fine.

**Cause:** `/api/vod/start` returns as soon as ffmpeg is *spawned*, not once it has written anything, so `idx.m3u8` 404s for a while after the URL is handed out — measured at ~1.4–1.6 s on a 640×360 fixture, and far longer for a 2160p HDR source that must decode + tonemap before the first segment. The client attached a player to that URL immediately. Hls.js has a fatal-error → destroy/re-attach retry and rode straight through it, which is why desktop never showed the bug. iOS Safari takes the **native** `canPlayType("application/vnd.apple.mpegurl")` branch, which had **no error handling whatsoever** — one 404 and playback was dead until a page reload.

**Fix (client-side, `library.html`):**
- `waitForPlaylist()` HEAD-polls the playlist (200 ms, ×1.5 backoff, capped 2 s, 120 s ceiling) before either engine attaches. Engine-agnostic, so it fixes the root cause rather than the iOS symptom.
- A "preparing…" overlay replaces the dead black box, so a slow 4K HDR start reads as working rather than broken.
- A `video` `error` listener finally gives the native path a bounded reattach (5 tries, linear backoff) — the counter resets on `playing`, **not** on attach, or a permanently-failing segment would loop forever.

**Not done server-side on purpose:** having `/api/vod/start` block until the playlist exists would fix every client at once, but it holds a gunicorn thread for as long as the first segment takes — fine at 1.5 s, not fine for a 4K HDR file. The waiting belongs on the client.

**Caveat — this fixes "nothing ever plays", not "plays smoothly".** If the VOD encode runs slower than realtime (4K HDR tonemap on CPU is ~0.5×; check whether `VOD_FORCE_CPU=1` is set in the compose `.env`), playback will start and then stall as the player catches up to the encoder. The new retry recovers from those stalls instead of dying, but the real remedy is NVENC for VOD or a lower `TARGET_HEIGHT`.

**Verified:** the wait logic under unit test against the shipped function text (appears-after-404s, transient network errors, 401→login, superseded-session abort), and end-to-end against a live server — HEAD 200 after 5 polls / 1.64 s. **Not verified on a real iPhone** — no iOS device or browser automation available here.

## v2.6 — auto_fill variety + LLM content gate (shipped, filter default-OFF)

**Variety.** `auto_fill` picked uniformly over *files*, which is uniform over the wrong thing: a 200-episode show was 200× likelier than a movie, so the stream drowned in whichever series was longest. It also never consulted `recent_items`, so it could replay what just finished. Now it picks a **title** uniformly (`_title_key` collapses `tv/Show/Season N/…` to `tv/Show`), then an episode within it, skipping the last `AUTOFILL_RECENT_TITLES` (12) titles played. Measured over 1200 picks across 4 titles: ~300 each, with the 2-file show at 319 rather than the ~480 that file-weighting gave.

**Content gate.** A host-written policy (plain English, editable in settings) is judged per title by an Ollama-compatible endpoint; `auto_fill` only picks titles with an "allowed" verdict. **Fail-closed** — unjudged means ineligible.

Three constraints shaped the design:

1. **The model can never be called inline.** `_pick_random_from_library()` runs inside the watcher's source-transition path. A measured ~6 s `gemma3:27b` call there would be ~6 s of dead air on the live stream at *every* transition. So a background thread judges titles and writes a disk cache; the picker only ever reads it.
2. **The policy text is part of the cache key** (`_policy_hash`). Without that, editing the rule silently keeps every verdict made under the old one. Verified: changing the policy invalidated 4/4 verdicts and the picker correctly went silent.
3. **Verdicts must load before the watcher starts.** They didn't at first — the watcher's first tick saw an empty cache and skipped an auto_fill pick the on-disk verdicts would have allowed.

Manual overrides (`POST /admin/api/content/<key>` `{blocked}` / `{clear}`) are marked `manual` and **survive policy edits** — a human decision outranks the model's and shouldn't be silently re-judged away. `GET /admin/api/content` lists every title with state + reason + judge progress; without it, a title vanishing from auto_fill would be unexplainable.

Titles are judged on arr metadata (genres / certification / overview) when available — the arr poll now indexes it alongside the cover map at no extra request — falling back to the prettified title. New env: `OLLAMA_URL` (default `host.docker.internal:11434`), `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_S`, `CONTENT_JUDGE_INTERVAL_S`, `CONTENT_VERDICTS_FILE`, `AUTOFILL_RECENT_TITLES`.

**Default OFF on purpose:** enabling it with an empty cache means auto_fill has nothing to play until the judge sweeps the library (~6 s × title count).

**Provider:** the model call goes through the [`companion`](https://git.thelunadog.com/alex/companion) package's provider seam, not a hand-rolled HTTP call. `OllamaProvider.complete_json` uses Ollama's **native structured outputs** (`body["format"] = schema`), so the verdict arrives schema-validated rather than regexed out of a ```json fence, and `build_provider()` makes the backend swappable by env alone (`CONTENT_PROVIDER_KIND`: ollama | openai | openai-compatible | anthropic).

  companion's API is async while jetstream is sync Flask + threads, so `_judge_title()` bridges with `asyncio.run()` — safe only because it is called exclusively from the dedicated judge thread, one title at a time, never from a request handler or the watcher.

  **The import is guarded, and that is load-bearing.** The Dockerfile installs companion from Forgejo over HTTPS (last layer, so a Forgejo outage can't invalidate the pinned layers above), but if it is ever missing `app.py` catches the ImportError and the gate degrades to "never judges" instead of the container dying on boot. Verified both ways: with companion broken the app imports fine and `judge_available` reports false; and cold-start fail-closed still holds (empty cache + no judge => auto_fill correctly returns nothing).

  Note fail-closed applies to *unjudged* titles, not to a judge outage: already-cleared verdicts stay valid, so if ollama goes down the stream keeps playing the titles it had already cleared.

## v2.7 — idle auto-pause (shipped)

`auto_fill` transcoded a random library title forever with no audience: prod had been chewing on one film for 1h30m with the last real viewer connection days earlier. The watcher now reads `_viewer_count()` each tick and, with `LIVE_IDLE_TIMEOUT_S` (default 120 s) elapsed and zero viewers, calls `_auto_pause_locked()` — the same kill-and-resume freeze as `api_pause` (ffmpeg terminated, run retired, `current_paused=True`, `paused_position` frozen), tagged `auto_paused=True` to distinguish it from a user pause. While idle it also gates the queue pop, `auto_fill`, and pre-roll promotion, so nothing new starts for an empty room; a stream that ends naturally with no viewers falls through to the fully-idle cleanup.

When a viewer's `/hls` request reappears (`_viewer_count() > 0`), the watcher auto-resumes the *same* title from `paused_position`. Only `auto_paused` pauses resume — a manual Pause stays put. `is_live` sources are never auto-paused (no resumable position). Cost: a ~1-3 s cold start when a viewer connects instead of sitting on a warmed live edge. `0` disables and restores the old always-playing behavior.

New env: `LIVE_IDLE_TIMEOUT_S`. New global: `auto_paused` (under `state_lock`), reset wherever `current_paused` goes false (`_start_stream`, `_stop_locked`, `_skip_locked`, `_promote_preroll_locked`) and on manual `api_pause`.

## Open

- **React migration** — replacing the hand-written page scripts panel by panel as React islands (see ARCHITECTURE → React islands; design in `docs/superpowers/specs/2026-09-08-react-migration-design.md`). The earlier Angular port and the Svelte islands were replaced wholesale on 2026-09-08: Angular assumed it owned the page and fought the islands pattern. Merged to main and deployed 2026-09-09. Done: every admin data panel (incl. queue, recent, chat), viewer queue/requests/library/chat, the home front door, login, and the hub. Remaining, largest first: `library.html` (entirely inline, ~1250 lines — browse, search, VOD player, subtitles, continue-watching; also the file the `ui/daisyui-conversion` branch rewrote, so port from one or the other, not both), `admin.html` file browser + playback/seek/subtitle controls + invites (~1080 lines), `viewer.html` player + reactions (~1090 lines). Each is its own small spec.

- **Viewer: say *why* playback can't start (added 2026-09-09)** — `setupPlayer` in `viewer.html` falls through to "HLS not supported in this browser" whenever `Hls.isSupported()` is false, and the page otherwise looks healthy (status, chat and reactions keep polling), so the viewer sees a black box and reports "black screen". The 2026-09-09 case was LibreWolf on Arch: ffmpeg 9 moved the system to libavcodec 63, which Firefox 153 builds can't load, so `MediaSource.isTypeSupported('video/mp4; codecs="avc1…"')` was false while the server was fine. Two small pieces: (1) run the `isTypeSupported` probe up front and render a specific message ("this browser has no H.264 decoder — on Linux Firefox/LibreWolf install the distro's ffmpeg compat package"); (2) include the probe result in the `[viewer] connect` log line and in report-queue submissions so the server side can tell a decoder-less client from a stream fault in one grep. Triage notes for the human path are in `HOWTO.md` once the item below lands.

- **HOWTO: black-screen triage section (added 2026-09-09)** — write down the split that worked: `/api/now-playing` position advancing + a frame grabbed from `/hls/run/N/v0/idx.m3u8` proves the server; `docker logs livestream | grep m4s` per user agent proves the player (polls-but-never-fetches-playlist = unsupported branch; fetches 2-3 segments then stops = fatal hls.js media error); only then look at the encoder. Two traps worth recording: recreating the containers discards the nginx log history, and the Playwright Chromium bundled with the dev tooling has no H.264 decoder, so it reports decode errors on good segments.

- **Non-H.264 rendition for decoder-less browsers (option, not committed)** — a VP9 or AV1 variant would let Firefox-family browsers play through their bundled ffvpx decoder without touching the system ffmpeg. Constraint: the stream host's RTX 3050 (Ampere) has NVENC for H.264/HEVC only — AV1 encode needs Ada, VP9 encode isn't in NVENC at all — so this would be a CPU software encode on top of the three GPU ladders. Only worth it if the decoder problem shows up on viewers we don't control; for the host's own machine the compat package is the fix.

- **Jetstream watcher agent (automation)** — the report queue (#34) already persists agent-readable JSON at `/data/reports.json`, exposes `GET /admin/api/reports`, and now accepts triage write-back at `POST /admin/api/reports/<id>/triage` (sets the reserved `triage` field under `reports_lock` — a direct file edit would be clobbered by Flask's in-memory rewrite). The admin reports panel renders the verdict when present. Still to build: the watcher loop itself — polls the queue, gathers nearby app/ffmpeg/browser context, triages likely causes, writes back, and either adds a roadmap note or drafts a fix for admin review. Design + failure-taxonomy playbook captured in `apps/watcher/DESIGN.md`; only the agent runner is outstanding.

- **Media requests — replace Overseerr** — *phase 1 shipped; phases 2-5 open. Full design in [`DESIGN-media-requests.md`](DESIGN-media-requests.md).* Decided scope: full replacement; requesting requires a **user account**, not an invite token.

  The load-bearing distinction: jetstream's existing "request" (#27) means *"play this file we already have"*; this one means *"acquire this thing we don't have"*. Same word, opposite direction — separate store, separate panel, separate route prefix, or both become ambiguous and the admin panel grows two Approve buttons that do wildly different things (one plays a file, one starts a 40 GB download).

  Most of the plumbing already exists: arr API-key reading and `X-Api-Key` calls, the 30-min inventory poll, accounts, the request-queue idiom, the poster proxy, rate limiting. Genuinely missing: discovery, **write** access to arr (this would be the first non-read-only integration), availability tracking, and quotas.

  Useful finding: **search needs no TMDB key** — `/api/v3/{movie,series}/lookup` on Radarr/Sonarr already proxy TMDB/TVDB and return objects that can be POSTed straight back to add. Only *browse/trending* needs a key, so that's phased last and degrades to a hidden tab. (This reverses the closed "TMDB not worth the friction" call, deliberately: that was about enriching the existing library, which is a different requirement.)

  Shipped phase 1: separate `/data/media_requests.json`, account-backed create/list/cancel endpoints, admin list/approve/reject status controls, and a host-only admin panel with no arr writes. Shipped phase 2: `/api/media/search` uses read-only Radarr/Sonarr lookup, annotates already-in-library/already-requested state, and `/library` now has a mobile-friendly request panel. Next phases: arr writes → availability tracking → TMDB discovery.

- **Stream rooms (a "mod" tier that can run its own live room)** — *next up; needs a design session before any code.* Wanted: a trusted user can spin up their own live room — own queue, own viewers, own chat — instead of everyone sharing the single broadcast.

  Be clear-eyed about the cost: **this is a rearchitecture, not a feature.** Every live-playback global in `app.py` assumes exactly one stream — `current_proc` / `current_source` / `current_paused` / `active_run_id` / `next_run_id` / `finished_run_ids` under `state_lock`, the pre-roll globals, `skip_votes`, `_composer_state` (a single media-sequence + discontinuity counter space), `playlist`, `recent_items`, the IP-keyed `viewers` / `viewer_labels` / `viewer_session_start`, the single global chat + reaction rings, and `settings.viewer_public`. On disk it's one `/hls` tree with one `stream.m3u8`, and the watcher + composer threads are singletons driving it. Making rooms real means keying all of that by room id and running a watcher/composer per room — or accepting a hard cap and running N independent instances.

  Cheaper adjacent option worth weighing first: the private-VOD engine is *already* per-user and multi-session. "Watch together in a room" could be built as a shared VOD session (one encode, several authorized viewers, synced position) rather than N live pipelines — much closer to what already works.

  Also needs: a `mod` tier (a third invite/account level between friend and admin), per-room authorization, room lifecycle/reaping, and a concurrency cap — each room is another ffmpeg competing for the same NVENC slots the live stream and VOD already share.

- **Per-viewer live subtitles (WebVTT sidecar)** — the live encode burns subtitles into the shared video, so the shipped v2.3 toggle is necessarily broadcast-wide (everyone sees the change). A true per-viewer CC toggle needs subs extracted to WebVTT, served alongside, referenced via `EXT-X-MEDIA`, and the composer taught to carry a subtitle rendition across run boundaries. Previously marked won't-do; re-listed because the question keeps coming up.

- **`app.py` split (6,162 lines, 237 functions, 109 routes, 6 threads)** — *analysed, deliberately not yet done.* The measurements, so the next attempt doesn't have to redo them:

  **The boundary is cleaner than it looks.** An `auth` module (users/sessions/passwords/register/login/admin-user routes) is ~22 functions needing 13 helpers from the rest; a `vod` module is ~20 functions needing 9. Crucially the rest of `app.py` needs exactly **one** symbol back from either — `_session_user` — so there's a single circular edge, not a web.

  **The usual refactor landmine is mostly absent.** Cross-module breakage comes from *rebinding* module-level globals, not mutating them. The vod functions use `global` **zero** times; the auth ones use it twice (`_load_users`, `_load_user_sessions`), and both rebind globals that would move into the same module. Dict/list mutation (`vod_sessions`, `watch_progress`) is shared correctly through an imported reference.

  **Two real blockers, both must be handled first:**
  1. **The Dockerfile copies exactly one Python file** (`COPY app.py /app/app.py`; `static/` is copied separately, no other `.py` is). Any new module must be added there or the container crashes on import at boot — prod down, for a change with no user-facing benefit. Verify by actually building the image, not by reading the diff.
  2. **No safety net on the live pipeline.** The test suites cover auth and VOD thoroughly, but the composer, watcher, pre-roll, chat, reactions, requests and reports have no characterisation tests — and those are exactly what a bad move would break. `_build_ffmpeg_cmd` is the exception: the 192-variant byte-identity harness used for the VOD-mode change covers it.

  **Suggested order:** characterisation tests for the live pipeline → Dockerfile + an image-boots smoke test → extract `vod` (zero globals, best-covered) → extract `auth` → only then consider splitting the live pipeline itself. Do it in its own session, not appended to a feature batch.

## Closed (works in practice / won't-do)

- **Firefox playback** — works in practice on current main. Defensive fixes parked locally on the `firefox-playback` branch (NVENC AUD BSF + FF-tuned Hls.js config + wider PDT lag) if a regression surfaces; tested-in-Firefox-OK but unshipped while there's nothing to fix. Branch can be rebased + deployed later if needed.

- **Now-playing info card** + **metadata-aware library search (actor / director)** — both required a TMDB API key + a per-file metadata cache. Not worth the friction for a personal co-watching setup. Filename search (`/admin/api/search`, `/api/control/search`) covers the realistic case; viewers still see the title + duration in the up-next panel.
- **HW VAAPI decode + colorspace handling** — superseded by the NVENC pipeline. The original motivation was unlocking HW decode on the Intel UHD 630 iGPU to drop 4K HEVC from ~135% CPU to ~30%. The host got an RTX 3050 and the NVDEC+NVENC path delivers a stronger win (4K HEVC HDR at ~17% CPU on prod, full GPU decode + encode). VAAPI code stays in the codebase for portability to Intel-iGPU hosts but the 100×-oversized-segment bug isn't worth chasing.
- **CC sidecar / WebVTT** — burn-in via #11 covers the friends-and-family case. Toggle/multi-language don't justify the composer rework.
- **Live voice chat / WebRTC** — was the v2 reason-to-exist; v2 scrapped, voice product not being built. v1's HLS at ~1.7 s sync is fine for co-watching.

## Done (this branch)

| # | What | Notes |
|---|------|-------|
|  1 | Verify viewers never need a manual refresh | Hls.js seamless source swap, watchdog, visibilitychange handler. |
|  2 | Seamless media-to-media transitions | Composer thread + per-run dirs + `EXT-X-DISCONTINUITY`. |
|  4 | English-audio preference | `_probe_english_audio` picks first `eng`-tagged stream. |
|  5 | Active-viewer admin endpoint | `/admin/api/viewers` with token labels. |
|  6 | Queue UI polish | Drag-drop reorder, source-type chips (FILE/YOUTUBE/URL), duration badges, `[CC]` indicator. |
|  8 | gunicorn replacing Werkzeug | `gunicorn 23.0 -w 1 -k gthread --threads 16 --timeout 120`. |
|  9 | nginx sidecar for `/hls/*` | Shared tmpfs volume, auth via subrequest to `/api/_authcheck`. |
| 10 | Non-YouTube yt-dlp audit | Vimeo / Dailymotion / SoundCloud / direct mp4 / direct m3u8 / Twitch / Reddit verified. README documents the supported set. |
| 11 | Per-source subtitle burn-in | `subtitles=` filter; auto-picks English text track; HDR/HW/CPU branches all wired. **Currently disabled** in prod (`USE_SUBTITLES=0`) pending the cache-extract Open item — the filter pre-loads the full subtitle track and stalls multi-GB sources. |
| 12 | Anonymous live text chat | In-memory ring (200 messages), per-IP rate-limited, sid-color-dotted, polling-based. Optional display name (24-char, defaults to "anonymous", server strips control chars / zero-widths). |
| 13 | Low-latency mode | 1 s segments → ~1.7 s sync floor (was ~3-5 s). Real LL-HLS would need ffmpeg `EXT-X-PART` support. |
| 15 | 4K passthrough + HDR tonemap | `TARGET_HEIGHT` env knob (source-bounded), zscale Hable tonemap for PQ/HLG sources. |
| 16 | `/admin/api/perf` | ffmpeg PID + uptime, run state, segment count, viewer count. |
| 17 | Library scan cache | mtime-invalidated. 26 ms cold → 0.03 ms warm. |
| 18 | Viewer `MANIFEST_PARSED` snap fallback | Belt-and-suspenders for cases where `loadedmetadata` fired before live edge was computable. |
| 19 | NVENC pipeline (NVDEC + h264_nvenc) | Third HW encode branch, picked when `USE_NVENC=1`. Full GPU decode + scale + encode. 4K HEVC HDR on prod runs ~17% CPU vs ~135% on libx264. |
| 20 | GPU overlay + CPU fallback | Compose split into CPU-only base + `docker-compose.gpu.yml` overlay; `bin/install` detects nvidia and chains via `COMPOSE_FILE`. Stack now runs on any host. |
| 21 | Tear down `jetstream-dev` | NVENC was the reason it existed; with NVENC live on prod the dev sister-service is gone. |
| 22 | Friend control tier | Invite tokens get a `level` field (`viewer` / `friend`). Friend links unlock playback + queue control via `/api/control/*` and a `/controls` page (same `admin.html`, host-only panels hidden). No login — same invite-link model. |
| 23 | Emoji reactions | Tap an emoji, it floats up over everyone's video. Ephemeral `/reactions/recent` feed (id + 6 s recency filter). Twemoji renders the floats as SVG images so devices without a color-emoji font still see them. **Removed in `300223e`, then restored + expanded — see #35.** |
| 24 | Vote to skip | Any viewer can vote; passes at a strict majority of active viewers (IP-keyed, matches `_viewer_count`). Tally surfaced in `/api/status`; resets on every new source. Friends/admin keep outright Skip. |
| 25 | Viewers see the queue | Read-only "Up next" panel in `viewer.html` polls `/api/queue` every 5 s and hides itself when empty. Collapsible like chat. |
| 26 | Library search (filename) | `/admin/api/search` + `/api/control/search`. Space-separated terms AND-matched against each file's path. Debounced search input on both `/admin` and `/controls`, capped at 300 results with a truncated flag. |
| 27 | Request to queue | Viewers submit a path; lands in a pending list the host approves/denies. New routes: viewer-gated `/api/library/{browse,search}` + `POST /api/request`; admin `GET/POST/DELETE /admin/api/requests/...`. Persisted to `/data/requests.json`. Rate-limited 10/60 s per IP, dedups on same-path. |
| 28 | Search results show parent folder | Scene-rip filenames (e.g. `clue-sun401.avi`) reveal which show they belong to (`↳ tv/It's Always Sunny in Philadelphia/Season 4`) in both admin search and the viewer request panel — until Sonarr cleans the names. |
| 29 | Deep library-scan invalidation | `_scan_library` cache now keys on a per-directory mtime fingerprint of the whole tree (XOR-hashed name+mtime). Adding a new episode inside an existing `tv/Show/Season N/` folder busts the cache the same way a new top-level folder does. ~1-3 ms signature check vs ~13 ms full scan on miss. |
| 30 | Requests TTL + clear-all | Pending viewer requests auto-expire after 7 days (lazy sweep on list view + on new-add dedup). New `DELETE /admin/api/requests` clears the whole pile; admin panel grows a "Clear all" button when anything's pending. |
| 31 | Chat moderation | `DELETE /admin/api/chat/<id>` drops a single message from the ring and surfaces the id via `/chat/recent.deleted_ids` so already-painted viewers tear it down on next poll. `POST /admin/api/chat/mute {sid, seconds}` rejects further sends from that sid for the window (capped at 7 days; capacity gated by the rate-limit shape, not the mute). Admin chat panel grows hover-reveal `✕`/`mute` buttons on every row. |
| 32 | Subtitle burn-in via cache-extract | First play of a source with a chosen sub track kicks off a background ffmpeg sidecar extract to `/data/subs/<sha256(path+mtime+idx)>.srt` and runs that stream WITHOUT subs. Every subsequent play points `subtitles=filename=<cached.srt>` at the tiny `.srt` — libass parses the few KB instantly and burn-in lands without stalling the encode. Per-key in-flight lock prevents duplicate extracts on concurrent plays; hash includes `st_mtime_ns` so a Sonarr upgrade busts the cache. Gated by `USE_SUBTITLES=1` (default off — set in compose env to opt in). Cache cleanup (orphaned entries) deliberately not implemented; subs are small enough that the dir can grow for a long time before it matters. |
| 33 | Fix fullscreen on iOS Safari | The fs-btn click handler had a stale guard (`&& !playerWrap.requestFullscreen`) that was correct on pre-16.4 iOS Safari but turned false once Safari 16.4 shipped a partial `Element.requestFullscreen` for `<div>` — iOS then fell through to the standard path, which silently rejects on a video-bearing div. Now mirrors the working pip-btn pattern: prefer `video.webkitEnterFullscreen()` whenever it exists, regardless of the standard API's presence, with a `.catch` fallback that drops the wrap div in favour of the bare `<video>` if div-fullscreen rejects on any UA. Also bumped `#vol-controls button` to a 44pt min-width/height hit target (iOS HIG) so near-misses no longer fall through to the unmute-overlay's click handler underneath. |
| 33 | Pre-roll next item across source transitions | The watcher spawns the next queue item's ffmpeg into a fresh run dir `PREROLL_LEAD_SECS` (=6 s) before the current source's EOF. Both encoders run briefly in parallel; the composer naturally stitches their run dirs via `EXT-X-DISCONTINUITY`, and the live-edge buffer keeps draining fresh segments straight through the transition instead of stalling for the ~3-4 s the old shape lost to "watcher 1 s poll + ffprobe + ffmpeg startup + first-GOP wall time." Symptom that drove this: last ~15 s of every play stuttered + flashed before the next queued item picked up. Limited to file items (URL items would race yt-dlp resolution). Cancellation is wired into `_terminate_proc_locked` (skip / pause / set-source / stop) and restores the pre-rolled item to the head of the playlist. `/admin/api/perf` surfaces the `preroll` block (pid / alive / run_id / title) so the transition is observable from the dashboard. |
| 34 | Hide on-screen controls when idle | Player chrome (`#vol-controls`, `#reaction-bar`) fades after 1.8 s of no pointer/touch/keyboard activity and reappears on hover, tap, focus, pause, buffering, or mute (`controls-idle`/`controls-active` classes + `:focus-within`/`:hover` overrides in `jetstream-theme.css`; `shouldHidePlayerChrome` gate in `viewer.html`). `cursor:none` while idle; never hides while an input/menu is focused. |
| 35 | Viewer bug report queue | `⚑ Report` button in `viewer.html` opens a modal (optional message) and POSTs `/api/report` — rate-limited 5/300 s per IP, persisted to `/data/reports.json`. The stored record is agent-readable: server-authoritative `ts`/`ts_iso`/`ip`/`viewer_label` + a `stream_health` snapshot (encoder, ffmpeg alive/uptime, segments, viewers, server position, quality/seg-time) kept separate from the client's `client` block (what the player thought it was showing: title/path/position/mode/mute/fullscreen/UA), plus a reserved `triage` field for the future watcher agent. Admin reports panel (host-only) lists newest-first with mark-handled / delete / clear-all; `/api/status.reports_new` badges the count. |
| 36 | Reactions restored + any-emoji picker + viewer-uploaded customs | Brought back the in-player tap bar (6 quick emoji + Twemoji floats) torn out in `300223e`, then extended: a scrollable **any-emoji** picker in a dedicated `reactions` panel (server validates codepoints via `_is_emoji` — ZWJ sequences / skin tones / flags OK, text/markup rejected — so the 300-emoji client list is just the UI menu, not a hard limit). Viewers **import their own** reaction images (`POST /api/reactions/upload`: magic-byte sniff, 512 KB cap, content-hash dedup, ≤40 set, 5/600 s per IP) served from `/data/reactions/` and surfaced in the bar + a panel gallery; admin moderation panel deletes them (file unlinked when unreferenced). Client caps concurrent floats at 40 so a flood can't jank the device. |
| 41 | Strip dead component CSS | Resolved the "component CSS never loads" issue (#38) by taking the strip path: deleted the dead scoped `<style>` blocks from all three Svelte components (`AdminQueue` −275, `AdminRecent` −126, `ViewerChat` −185 lines; none were ever linked/injected, so zero visual change). `jetstream-theme.css` + each page's inline `<style>` are now the sole, unambiguous source of truth — no more dormant rules that look authoritative but do nothing. |
| 40 | Tokenless now-playing feed (`/api/now-playing`) | External-display endpoint for the living-room hub. Returns `{"title","playing"}`, exempt from the viewer token gate (title-only, non-sensitive), 2 s cache. Title is prettified from the raw scene-release filename via `_display_title` (cuts at the first resolution/source/codec/tag token — `Hokum 2026 REPACK 1080p AMZN WEB-DL ...mkv` → `Hokum 2026`); URL/yt-dlp sources keep their human title as-is. App's own UI still shows the raw filename. Consumed via `JETSTREAM_TITLE_URL` on the pi. |
| 39 | Now-playing title on the player | Small filename-derived title label pinned top-left of the player (`#now-playing` in `viewer.html`), reveals for 5 s on each source change and otherwise fades with the player chrome (same `.controls-idle` rules as `#vol-controls`; JS `.np-reveal` pins it on change). Took the lightweight path — the active title already rides the existing `/api/status` channel (`title` field), so this is a viewer-side render only, no HLS-metadata / ID3 tagging. Accent-dot prefix matches the unified theme (#38). |
| 38 | Frontend consolidation + mobile polish | Unified 4 competing blues → single cyan `--js-accent` (`#38bdf8`) token in `jetstream-theme.css`; emoji→inline-SVG icons (mute/pip/fs/vote-skip/queue actions) on viewer + admin; fixed chat-header double-label and queue-row action overflow (6-column grid + icon cluster); ≥40px touch targets throughout; no horizontal overflow at 360px portrait or landscape. **Latent issue surfaced:** Vite emits Svelte scoped CSS to `static/build/assets/` but nothing loads it — the UI is styled entirely by inline `<style>` + `jetstream-theme.css`, so all fixes routed through the loaded stylesheet. Tracked as an Open cleanup item. |
| 37 | Viewer session time cap | Continuous-watch limit (`VIEWER_MAX_SESSION_HOURS`, default 8, set in compose; 0 disables). The `/hls` auth gate (`/api/_authcheck`) starts returning 403 once `viewer_session_start[ip]` exceeds the cap, so segments cut off; `/api/status.session_expired` drives a "keep watching" overlay in `viewer.html` that resets the clock via `POST /api/session/continue`. A fresh session (reload after the 30 s idle prune, or a new tab) starts over — reclaims forgotten tabs, not a ban. |

Plus, off-list:
- YouTube DASH dual-input fix (separate video + audio URLs through ffmpeg as two `-i` inputs — was 360p, now 1080p).
- Spacebar in chat input no longer eaten by the document-level pause-prevent handler.
- Volume slider on viewer.
- NVENC HDR scale-first tonemap + Dolby Vision detection (subsidiary fixes to #19) — the zscale tonemap ran at 4K on CPU (~0.5× realtime, stalled) and NVDEC couldn't decode the DV enhancement layer at all; scaling before tonemap (1.9–2.2× realtime) plus an `is_dovi` flag that forces CPU decode for DV files unstuck 4K HDR content.
- Token-only viewing (settings `viewer_public=false`) durably enabled on prod; bare URL returns the invite page without a valid `?t=` / cookie.
- Subtitle burn-in disabled globally (`SUBTITLE_BURN_IN`) to stop the `subtitles=`-filter full-file scan; real fix tracked under the Open item.
- Live-edge sync regression: prod was running 4 s HLS segments but the player's `liveSyncDurationCount: 1` + `TARGET_LAG_S: 2.5` tuning assumed 1 s segments. The drift-correction loop kept seeking into the segment still being written → choppy / desyncing playback on every browser (worst on Firefox where MSE doesn't clamp out-of-buffer seeks). Fixed by dropping `HLS_SEG_TIME` back to 1 s (matches ROADMAP #13's design); env-only change, no rebuild.
## Make this usable by others (added 2026-08-27)

- [ ] Universalize the README / docs / code for outside users: document setup
  from scratch on generic infrastructure, replace homelab-specific assumptions
  (private hostnames, LAN addresses, personal paths and defaults) with
  env-driven configuration plus examples, and keep the public GitHub mirror
  directly runnable.
