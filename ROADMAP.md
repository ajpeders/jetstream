# jetstream roadmap

v1 + a viewer-interaction pass shipped. All tracked cleanup items closed.

## Open

(none — outstanding items closed below.)

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
| 23 | Emoji reactions | Tap an emoji, it floats up over everyone's video. Ephemeral `/reactions/recent` feed (id + 6 s recency filter). Twemoji renders the floats as SVG images so devices without a color-emoji font still see them. |
| 24 | Vote to skip | Any viewer can vote; passes at a strict majority of active viewers (IP-keyed, matches `_viewer_count`). Tally surfaced in `/api/status`; resets on every new source. Friends/admin keep outright Skip. |
| 25 | Viewers see the queue | Read-only "Up next" panel in `viewer.html` polls `/api/queue` every 5 s and hides itself when empty. Collapsible like chat. |
| 26 | Library search (filename) | `/admin/api/search` + `/api/control/search`. Space-separated terms AND-matched against each file's path. Debounced search input on both `/admin` and `/controls`, capped at 300 results with a truncated flag. |
| 27 | Request to queue | Viewers submit a path; lands in a pending list the host approves/denies. New routes: viewer-gated `/api/library/{browse,search}` + `POST /api/request`; admin `GET/POST/DELETE /admin/api/requests/...`. Persisted to `/data/requests.json`. Rate-limited 10/60 s per IP, dedups on same-path. |
| 28 | Search results show parent folder | Scene-rip filenames (e.g. `clue-sun401.avi`) reveal which show they belong to (`↳ tv/It's Always Sunny in Philadelphia/Season 4`) in both admin search and the viewer request panel — until Sonarr cleans the names. |
| 29 | Deep library-scan invalidation | `_scan_library` cache now keys on a per-directory mtime fingerprint of the whole tree (XOR-hashed name+mtime). Adding a new episode inside an existing `tv/Show/Season N/` folder busts the cache the same way a new top-level folder does. ~1-3 ms signature check vs ~13 ms full scan on miss. |
| 30 | Requests TTL + clear-all | Pending viewer requests auto-expire after 7 days (lazy sweep on list view + on new-add dedup). New `DELETE /admin/api/requests` clears the whole pile; admin panel grows a "Clear all" button when anything's pending. |
| 31 | Chat moderation | `DELETE /admin/api/chat/<id>` drops a single message from the ring and surfaces the id via `/chat/recent.deleted_ids` so already-painted viewers tear it down on next poll. `POST /admin/api/chat/mute {sid, seconds}` rejects further sends from that sid for the window (capped at 7 days; capacity gated by the rate-limit shape, not the mute). Admin chat panel grows hover-reveal `✕`/`mute` buttons on every row. |
| 32 | Subtitle burn-in via cache-extract | First play of a source with a chosen sub track kicks off a background ffmpeg sidecar extract to `/data/subs/<sha256(path+mtime+idx)>.srt` and runs that stream WITHOUT subs. Every subsequent play points `subtitles=filename=<cached.srt>` at the tiny `.srt` — libass parses the few KB instantly and burn-in lands without stalling the encode. Per-key in-flight lock prevents duplicate extracts on concurrent plays; hash includes `st_mtime_ns` so a Sonarr upgrade busts the cache. Gated by `USE_SUBTITLES=1` (default off — set in compose env to opt in). Cache cleanup (orphaned entries) deliberately not implemented; subs are small enough that the dir can grow for a long time before it matters. |
| 33 | Pre-roll next item across source transitions | The watcher spawns the next queue item's ffmpeg into a fresh run dir `PREROLL_LEAD_SECS` (=6 s) before the current source's EOF. Both encoders run briefly in parallel; the composer naturally stitches their run dirs via `EXT-X-DISCONTINUITY`, and the live-edge buffer keeps draining fresh segments straight through the transition instead of stalling for the ~3-4 s the old shape lost to "watcher 1 s poll + ffprobe + ffmpeg startup + first-GOP wall time." Symptom that drove this: last ~15 s of every play stuttered + flashed before the next queued item picked up. Limited to file items (URL items would race yt-dlp resolution). Cancellation is wired into `_terminate_proc_locked` (skip / pause / set-source / stop) and restores the pre-rolled item to the head of the playlist. `/admin/api/perf` surfaces the `preroll` block (pid / alive / run_id / title) so the transition is observable from the dashboard. |

Plus, off-list:
- YouTube DASH dual-input fix (separate video + audio URLs through ffmpeg as two `-i` inputs — was 360p, now 1080p).
- Spacebar in chat input no longer eaten by the document-level pause-prevent handler.
- Volume slider on viewer.
- NVENC HDR scale-first tonemap + Dolby Vision detection (subsidiary fixes to #19) — the zscale tonemap ran at 4K on CPU (~0.5× realtime, stalled) and NVDEC couldn't decode the DV enhancement layer at all; scaling before tonemap (1.9–2.2× realtime) plus an `is_dovi` flag that forces CPU decode for DV files unstuck 4K HDR content.
- Token-only viewing (settings `viewer_public=false`) durably enabled on prod; bare URL returns the invite page without a valid `?t=` / cookie.
- Subtitle burn-in disabled globally (`SUBTITLE_BURN_IN`) to stop the `subtitles=`-filter full-file scan; real fix tracked under the Open item.
- Live-edge sync regression: prod was running 4 s HLS segments but the player's `liveSyncDurationCount: 1` + `TARGET_LAG_S: 2.5` tuning assumed 1 s segments. The drift-correction loop kept seeking into the segment still being written → choppy / desyncing playback on every browser (worst on Firefox where MSE doesn't clamp out-of-buffer seeks). Fixed by dropping `HLS_SEG_TIME` back to 1 s (matches ROADMAP #13's design); env-only change, no rebuild.
