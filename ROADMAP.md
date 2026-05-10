# jetstream roadmap

Outstanding work, ordered roughly by impact / effort.

## Reliability / UX

- **Verify viewers never need a manual refresh.** Recent work: Hls.js seamless source swap (no destroy on stream change), watchdog (armed-after-first-playing, 5s stuck-detection, single-fire), `visibilitychange` resume handler, no-remute on play() rejection. Suspected remaining gaps: iOS Safari stuck-paused after long backgrounding, native-HLS source swap requiring user gesture on auto-advance.
- **Seamless media-to-media transitions** (deferred mid-design — see below). Today: ~3s of frozen last frame between items. Two scopes:
  - *Pre-warm only* (~3s → ~1s gap, ~150 lines). Start the next ffmpeg in a side dir ~5s before current ends; on exit, swap dir + bump `stream_id` and let the existing Hls.js `loadSource` swap path do the rest. Player still flickers briefly through the swap.
  - *True seamless* (<1s, no flicker, ~400+ lines). Each ffmpeg writes to `/hls/r<run_id>.m3u8` + `/hls/r<run_id>_seg_*.ts`. Python composer thread polls active runs every ~500ms and writes a unified `/hls/stream.m3u8` with `EXT-X-DISCONTINUITY` between runs, maintaining `EXT-X-MEDIA-SEQUENCE` + `EXT-X-DISCONTINUITY-SEQUENCE`. Player never sees a `stream_id` change. Care needed around PDT continuity, segment-cleanup races (don't drop a run's segments while still in the rolling window), and merging the WebVTT `var_stream_map` output if captioning lands first.
- **Closed captioning (sidecar WebVTT).** Half-built in dev (uncommitted): `_probe_tracks()` finds text-based subs (SRT/SSA/ASS/mov_text — bitmap PGS skipped), `_build_ffmpeg_cmd` outputs `var_stream_map` with `master.m3u8` + `stream_av.m3u8` + `stream_cc.m3u8` variants when subs are present, viewer.html has a CC toggle button wired to `hls.subtitleTrack` and native `TextTrack.mode`. Untested end-to-end. Decision required: ship as-is then layer seamless transitions on top, or revert before seamless.
- **English-audio preference.** Same `_probe_tracks()` pass picks an English-tagged audio stream when present (handles `MULTi` rips that put a foreign dub first). Independent of captioning, worth landing on its own.
- **Active-viewer admin endpoint** (`GET /admin/api/viewers`). Reconstructing from logs works but a real-time list with token label + geo would be nicer; only admins see it.
- **Queue UI polish.** Current admin queue panel is functional but ugly: cramped row layout, awkward up/down arrow buttons, no thumbnails, no source-type icons (file vs YouTube vs URL), no duration shown, no drag-to-reorder, no clear visual distinction between "currently playing" and "next up". Targets: drag-to-reorder, duration badges, source-type chips, thumbnails for file sources (ffmpeg can dump a single frame on enqueue), better empty-state copy.

## Performance

- **HW VAAPI decode + colorspace handling.** Sitting in dev with `USE_VAAPI_DECODE=1`. Currently broken because (a) this iGPU has no VPP unit, so `scale_vaapi` fails — workaround is `hwdownload→CPU scale→hwupload` and (b) colorspace metadata is lost across the chain so HDR sources end up "deepfried". Fix path: explicit `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709` on the encoder + a `zscale`-based tonemap step for HDR10 sources. When that's clean on dev, flip prod's `USE_VAAPI_DECODE` back to 1. Expected CPU drop: ~135% → ~30% on 4K HEVC.
- **Replace Werkzeug with gunicorn / waitress.** The dev server handles segments + status + admin. Fine for a few viewers, suboptimal at scale. Drop-in image change.
- **Serve `/hls/*` from nginx sidecar** instead of Flask. Static tmpfs files, no reason to route them through Python. Larger refactor (compose + Traefik path-prefix split).

## Features

- **Broader media-link sources alongside YouTube.** `play_url` already pipes through yt-dlp so anything yt-dlp recognises (Vimeo, Twitch VODs, SoundCloud, Reddit-hosted video, generic direct .mp4/.m3u8 URLs, and platform-specific extractors) should largely work today, but it's never been tested beyond YouTube. Audit which sites actually behave end-to-end (auth-walled, geo-blocked, DRM, livestream-only formats), surface clearer errors when an extractor needs cookies / login, and document the supported set in the README. Likely needs a yt-dlp config volume mount for cookies on sources that require them.
- **Subtitles (per-source toggle).** Burn-in via `subtitles=` filter is the simplest path for iPhone Safari; pick a track index when admin queues a file. Soft `webvtt` track is the alternative but more delicate to mux.
- **Live chat alongside the stream.** Watch-party reactions in a sidebar / collapsible panel on viewer + admin pages. WebSocket pub/sub server-side (Flask-Sock or a tiny standalone gateway), name comes from the existing token label so messages already have an owner (`anonymous` for token-less / public-mode). Persist a short scrollback in memory only — no need for a chat history file. Admin gets a kick / mute control. Probably want rate limiting per IP to avoid spam from a leaked link.
- **LL-HLS sub-segment parts.** Drops sync floor from ~3–5s to ~500ms–1s. Server-side ffmpeg pipeline rework (`+program_date_time` already in place; need `+iframes_only` discipline and `EXT-X-PART` emission). Native iOS support, no client SDK swap.
- **WebRTC media server (LiveKit / MediaMTX).** True frame-sync, sub-200ms. Replaces Hls.js on the client. Multi-day rewrite. Only worth it if "watch parties with audio reactions" become the actual product.
- **ABR ladder (1080p + 720p + 480p).** Multiple ffmpeg encode chains. Useful only when streaming to off-LAN viewers on cell.

## Niceties

- **`/admin/api/perf` endpoint** — exposes ffmpeg PID, encode FPS (from `-progress`), HLS segment count, viewer count. Cheap, makes regressions observable.
- **Cache library scan** with mtime invalidation. Currently 13 ms for 143 files; will matter only if the library grows past a few thousand.
- **Viewer-side `loadedmetadata` snap robustness.** If Hls.js fires before `video.duration` is reliable, the snap is a no-op. Could fall back to `MANIFEST_PARSED` + `hls.liveSyncPosition` for an explicit jump.
