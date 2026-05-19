# jetstream roadmap

v1 is feature-complete. Only one item still open, gated on hardware:

## Open

- **Firefox playback.** Currently flaky / non-working on FF despite existing tweaks (fmp4 segments, AUD insertion, explicit BT.709 color tagging). Need to repro and diagnose. Likely suspects: Hls.js path differences in FF MSE, segment boundary IDR handling, fmp4 init segment compatibility, or PDT-based live-edge convergence misbehaving on FF's playback clock. Chrome and Safari work today.
- **HW VAAPI decode + colorspace handling.** Workaround pinned: `USE_VAAPI_DECODE=0` everywhere; CPU decode handles everything correctly. Real fix would unlock ~135% → ~30% CPU on 4K HEVC. Two paths:
  - **Software fix on existing iGPU** (Intel UHD 630 / Coffee Lake). Bug is isolated: HW decode chain runs at 0.995× realtime against `-f null` but produces 100×-oversized segments under `-f hls` — so the bug is in HLS-muxer/timestamp interaction with VAAPI-source PTS, not in decode/scale/encode. Untested fix candidates: `-fps_mode passthrough`, `-output_ts_offset 0`, `-bsf:v setts=pts=PTS-FIRST_PTS`. Needs a session of focused ffmpeg debugging.
  - **Hardware swap.** Intel ≥11th gen iGPU has `VAEntrypointVideoProc` (VPP), so `scale_vaapi` runs natively and the broken `hwdownload→CPU scale→hwupload` filter chain isn't needed. NVIDIA + NVENC is a different software stack entirely (`hevc_cuvid`, `h264_nvenc`) that sidesteps VAAPI. Either swap likely fixes it without code changes beyond an ffmpeg-cmdline branch. Needs `nvidia-container-toolkit` for NVIDIA.

## Closed (won't-do)

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

Plus, off-list:
- YouTube DASH dual-input fix (separate video + audio URLs through ffmpeg as two `-i` inputs — was 360p, now 1080p).
- Spacebar in chat input no longer eaten by the document-level pause-prevent handler.
- Volume slider on viewer.
