# jetstream roadmap

v1 is feature-complete. Only one item still open, gated on hardware:

## Open

- **Subtitle burn-in without stalling the stream.** Burn-in is currently OFF (`SUBTITLE_BURN_IN`/`USE_SUBTITLES=0`). The libass `subtitles=` filter loads the *entire* subtitle track before rendering its first frame, so ffmpeg must demux the whole container to EOF before producing segment 0. On multi-GB sources this is a full-file disk scan that runs the encode at a tiny fraction of realtime — measured ~0.007× on an 18 GB DV remux (17 frames in 80 s), and a 5.6 GB 1080p movie (Asteroid City) stalled identically. Pre-extracting the track to a sidecar `.srt` is no faster up front (still ~100 s on the 18 GB file, ~30 s on 5.6 GB) — same full-file read. **Real fix:** cache-extract subs to `state/subs/<path+mtime hash>.srt` in the background on first play (start without subs), then burn from the tiny cached file on subsequent plays. Optionally a per-source size cutoff for the auto-pick. Filename-vs-stream-index mapping already exists (`_probe_subtitle_tracks` returns the `si=` index). Re-enable via `USE_SUBTITLES=1` once cached extraction lands.
- **Firefox playback.** Currently flaky / non-working on FF despite existing tweaks (fmp4 segments, AUD insertion, explicit BT.709 color tagging). Need to repro and diagnose. Likely suspects: Hls.js path differences in FF MSE, segment boundary IDR handling, fmp4 init segment compatibility, or PDT-based live-edge convergence misbehaving on FF's playback clock. Chrome and Safari work today.

## Closed (won't-do)

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
| 11 | Per-source subtitle burn-in | `subtitles=` filter; auto-picks English text track; HDR/HW/CPU branches all wired. |
| 12 | Anonymous live text chat | In-memory ring (200 messages), per-IP rate-limited, sid-color-dotted, polling-based. Optional display name (24-char, defaults to "anonymous", server strips control chars / zero-widths). |
| 13 | Low-latency mode | 1 s segments → ~1.7 s sync floor (was ~3-5 s). Real LL-HLS would need ffmpeg `EXT-X-PART` support. |
| 15 | 4K passthrough + HDR tonemap | `TARGET_HEIGHT` env knob (source-bounded), zscale Hable tonemap for PQ/HLG sources. |
| 16 | `/admin/api/perf` | ffmpeg PID + uptime, run state, segment count, viewer count. |
| 17 | Library scan cache | mtime-invalidated. 26 ms cold → 0.03 ms warm. |
| 18 | Viewer `MANIFEST_PARSED` snap fallback | Belt-and-suspenders for cases where `loadedmetadata` fired before live edge was computable. |
| 19 | NVENC pipeline (NVDEC + h264_nvenc) | Third HW encode branch, picked when `USE_NVENC=1`. Full GPU decode + scale + encode. 4K HEVC HDR on prod runs ~17% CPU vs ~135% on libx264. |
| 20 | GPU overlay + CPU fallback | Compose split into CPU-only base + `docker-compose.gpu.yml` overlay; `bin/install` detects nvidia and chains via `COMPOSE_FILE`. Stack now runs on any host. |
| 21 | Tear down `jetstream-dev` | NVENC was the reason it existed; with NVENC live on prod the dev sister-service is gone. |

Plus, off-list:
- YouTube DASH dual-input fix (separate video + audio URLs through ffmpeg as two `-i` inputs — was 360p, now 1080p).
- Spacebar in chat input no longer eaten by the document-level pause-prevent handler.
- Volume slider on viewer.
