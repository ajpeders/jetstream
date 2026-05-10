# livestream architecture

```
   media file / URL ──▶ ffmpeg (-re) ──▶ HLS segments on tmpfs (/hls)
                          │                       │
                          │                       ▼
        watcher thread ◀──┘            Flask /hls/* ──▶ Traefik ──▶ Hls.js / Safari
                                       Flask /api/status, /admin/*  (iPhone, desktop)
```

One ffmpeg per source, one HLS playlist on tmpfs, Flask serves manifest + segments + admin/viewer UIs. Everything else is bookkeeping around that core.

## Server

### ffmpeg pipeline (`_build_ffmpeg_cmd`)

```
ffmpeg -hide_banner -loglevel warning -nostdin
       -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va
       [-hwaccel vaapi -hwaccel_output_format vaapi -hwaccel_device va  # if hw_decode]
       [-ss <start_seconds>]
       -re -i <input>
       -map 0:v:0 -map 0:a:0? -sn          # primary V+A, drop subtitles (CPU saver)
       -vf "scale=-2:1080,format=nv12,hwupload"
            # or, if hw_decode is on (broken on this iGPU):
            #   "hwdownload,format=nv12|p010le,scale=-2:1080,format=nv12,hwupload"
       -c:v h264_vaapi -low_power 1 -rc_mode CQP -qp 23 -profile:v main
       -c:a aac -b:a 160k -ac 2
       -f hls -hls_time 4 -hls_list_size 6
       -hls_flags delete_segments+append_list+omit_endlist+independent_segments+program_date_time
       -hls_segment_type mpegts
       -hls_segment_filename /hls/seg_%05d.ts
       /hls/stream.m3u8
```

Why each piece:
- `-init_hw_device + -filter_hw_device` pin the filter chain to renderD128. Without it, ffmpeg silently picks a second VAAPI device that doesn't advertise the right profiles.
- `-map 0:v:0 -map 0:a:0? -sn` is critical — without it, ffmpeg auto-maps every subtitle track and CPU explodes (Superbad's 8 PGS subs once pushed it to 1015%).
- `-re` makes ffmpeg emit at real-time pace so HLS clients can keep up; without it ffmpeg would burn through the file as fast as it can decode.
- `+program_date_time` tags every segment with absolute server wall-clock — clients use it to converge on the same playhead (see *Sync* below).
- Pause is implemented by *killing* ffmpeg and clearing `/hls/`. SIGSTOP doesn't work cleanly with `-re` (wall-clock advances while suspended; SIGCONT then burst-encodes to "catch up"). On resume, a new ffmpeg starts with `-ss <paused_position>`.

### State machine (locks all in `app.py`)

| Lock | Protects |
|---|---|
| `state_lock` | `current_proc`, `current_source`, `current_start_offset`, `current_start_time`, `current_paused`, `paused_position`, `current_stream_id` |
| `playlist_lock` | `playlist[]` |
| `settings_lock` | `settings{}` |
| `tokens_lock` | `tokens[]` |
| `viewers_lock` | `viewers{}` |

`current_stream_id` increments on every fresh `_start_stream` (new source / seek / resume). Clients use it to detect that the stream restarted.

### Watcher thread

`_watcher()` ticks every 1s. Single state machine:
1. If ffmpeg is alive → continue.
2. If `current_paused` → continue (don't auto-anything while paused).
3. Pop next item from `playlist`. If empty + `auto_fill` is on → `_pick_random_from_library()` walks `/media`.
4. If we have an item → `_start_stream(item)`. On failure, sleep 4s (back-off).
5. Else → `_stop_locked()` + `_cleanup_hls()` (fully idle).

This is what gives the "always playing 24/7" behavior. `Stop` from admin clears the queue and turns off until `auto_fill` next ticks (which is why setting `auto_fill=false` is the only true off switch).

### State persistence (`STATE_FILE = /data/state.json`)

Saved on every `_start_stream`, every pause, every ~10 ticks of the watcher. Cleared on explicit stop. On startup `_restore_state_on_startup()` runs *before* the watcher starts so the watcher doesn't auto-fill before resume can run. If the saved state was paused, restore re-enters paused state (terminate ffmpeg, set `current_paused=True`) so playback comes back at exactly the same frame.

### Viewer tracking + logs

`_track_viewer()` runs on every `/hls/*` request, keyed by `X-Forwarded-For` IP. On *new* IP, fires `[viewer] connect ...` to stderr with token label + ipinfo.io geo + UA. Geo lookups happen on a daemon thread to avoid blocking the segment fetch. `_viewer_count()` prunes IPs idle >30s and emits `[viewer] disconnect ...`.

### Tokens / gating

Per-friend invite tokens live in `tokens.json`. Admin mints them with a label, viewer hits `?t=<token>`, server sets `lt` cookie (90d). All non-admin paths gated by `_gate_viewer_routes()` unless `settings.viewer_public=true`. Admin paths gated externally by Traefik basicauth on prod (no auth on dev — localhost-only).

## Client (admin.html, viewer.html)

Both pages share the same playback core. Differences: admin has scrub bar / queue UI / play+pause controls; viewer has just the player + position counter.

### Player setup / teardown

```
softTeardown()   destroy Hls.js, leave <video> with last frame on screen
teardownPlayer() destroy Hls.js + clear video.src — only used when fully idle
setupPlayer()    new Hls.js (cache-buster on URL) → loadSource → attachMedia
```

The split exists so seek / pause / auto-advance keep the last frame visible during the ~3s ffmpeg cold-start gap. iOS Safari path uses `?_=<Date.now()>` cache-busters because reusing the same URL is treated as a cached 404.

Hls.js fatal-error handler:
- If `serverPaused` → expected (manifest cleared); just drop hls and wait for the next stream_id bump.
- Otherwise → softTeardown + retry `setupPlayer` after 800ms.

### Sync (PDT-based)

Server emits `EXT-X-PROGRAM-DATE-TIME` for every segment + `server_unix` in `/api/status`. Client computes:

```
serverClockOffsetMs = s.server_unix * 1000 - Date.now()      # refreshed each poll
playingDateMs       = hls.playingDate                         # Hls.js
                     | video.getStartDate() + currentTime    # Safari native
lagS = (Date.now() + serverClockOffsetMs - playingDateMs) / 1000
```

Every 5s, if `lagS > 8` or `lagS < -3` → `snapToLive()` (sets `currentTime = duration`). Otherwise leave alone. `playbackRate` is held at 1.0 — earlier attempts at continuous rate steering (1.05/0.95) caused audible pitch wobble on iOS and were dropped.

Realistic floor: ~1–2s inter-device skew. Sub-second sync requires LL-HLS or WebRTC (see ROADMAP).

### Position display

Viewer header shows `<position> / <duration>` from `s.position_seconds` and `s.duration_seconds`. URL/live sources fall back to `LIVE`. Updated on each poll (~2.5s).

### iPhone specifics

- Fullscreen uses `video.webkitEnterFullscreen()` (iOS Safari can't fullscreen a `<div>`).
- PiP uses `video.webkitSetPresentationMode("picture-in-picture")` (`requestPictureInPicture` is unsupported).
- `playsinline` is set so video stays inline by default.
- Native HLS (`canPlayType("application/vnd.apple.mpegurl")`) is preferred over Hls.js when available.

## HLS specifics

- Segments live on tmpfs (`/hls`, 1.5 GiB). Container-internal, never persisted.
- Segment time = 4s; list size = 6 → 24s playlist. Tight live-edge by design (admin scrubbing is server-side, no need for long lookback).
- `omit_endlist` keeps clients in live mode (no `EXT-X-ENDLIST`).
- `delete_segments` rolls old segments off disk as new ones are appended.
- `+program_date_time` is the linchpin for cross-device sync.

## VAAPI on this iGPU

`vainfo` shows decode (VLD) for H.264, HEVC (Main + Main10), VP8, VP9, MPEG2. H.264 encode (EncSliceLP) works. **No `VAEntrypointVideoProc`** — meaning `scale_vaapi` cannot run on this hardware. The HW-decode path therefore has to download frames to CPU memory for the scale step, which works but loses colorspace metadata in transit (HDR sources end up "deepfried"). That's why `USE_VAAPI_DECODE=0` on prod. ROADMAP item.

## Network

- `web` Docker network (external) — Traefik front-door for prod.
- Prod `livestream` exposes only port 8080/tcp internally; Traefik handles HTTPS via Cloudflare DNS-01 Let's Encrypt + per-host basicauth on `/admin`.
- Dev `livestream-dev` binds `127.0.0.1:8081:8080` only — no Traefik, no DNS, no cert. Reach via SSH tunnel.
- The `local-only@file` middleware (defined in `services/traefik/dynamic.yml`) restricts to LAN/WireGuard/Docker but is NOT on the prod livestream router because friends connect from public internet via tokens.

## Things that look weird but are deliberate

- Pause clears `/hls/` and kills ffmpeg. The serverPaused-aware client error handler keeps the player alive across this so the Resume button stays clickable.
- Player pane is always visible (no `display:none`) even when idle, so seek/pause/auto-advance can't collapse the layout. `aspect-ratio: 16/9 + object-fit: contain` keeps the box at fixed dimensions.
- `auto_fill` defaults to true. Stop button is mostly a "skip current; watcher will pick something else" unless `auto_fill=false`. To truly stop: flip the setting then stop.
- Admin's `broadcast-seek` slider posts to `/admin/api/seek`; it does **not** scrub the local `<video>` element. Seek is server-side (kill + restart ffmpeg with new `-ss`).
