# jetstream

A small Flask + ffmpeg service that broadcasts video files (and yt-dlp-resolvable URLs) to friends over HLS, gated by per-friend invite tokens. Source lives at `~/homelab/apps/jetstream/`; deployed via the `livestream` Docker Compose stack in `~/homelab/services/livestream/`.

```
~/homelab/apps/jetstream/         ← code (this directory)
  app.py                          Flask backend
  static/admin.html               admin UI
  static/viewer.html              viewer UI
  Dockerfile                      python:3.12-slim + ffmpeg + gunicorn + yt-dlp
  ARCHITECTURE.md                 how it works
  ROADMAP.md                      what's left

~/homelab/services/livestream/    ← deployment
  docker-compose.yml              prod + dev stacks (Flask + nginx sidecar each)
  nginx-default.conf.template     shared nginx config (envsubst at start)
  .env.example                    required env vars
```

## Running

Each environment is two containers: a Flask app and an nginx sidecar that fronts it (serves `/hls/*` straight off a shared tmpfs, proxies everything else to Flask).

| | URL | Flask image | nginx | State dir | HW decode |
|---|---|---|---|---|---|
| **prod** | `https://live.thelunadog.com` | `homelab/livestream:latest` | `nginx-livestream` | `state/livestream/` | OFF |
| **dev**  | `http://127.0.0.1:8081` | `homelab/livestream:dev` | `nginx-livestream-dev` | `state/livestream-dev/` | OFF (workaround — see ROADMAP #7) |

```sh
cd services/livestream

# Prod (rebuilds image so app.py changes get baked in; recreates both containers):
docker compose up -d --build livestream nginx-livestream

# Dev (separate image tag, isolated state, app.py + static/ bind-mounted from source):
docker compose up -d --build livestream-dev nginx-livestream-dev

# Reach dev from a remote machine via SSH tunnel:
ssh -L 8081:127.0.0.1:8081 ween@192.168.0.176
# then http://localhost:8081 in the browser
```

**Hot-deploy patterns:**

| Change | Dev | Prod |
|---|---|---|
| `static/*.html` | live (bind-mounted) | `docker cp static/foo.html livestream:/app/static/foo.html` |
| `app.py` | `docker restart livestream-dev` (bind-mounted, picks up new code) | rebuild + recreate (see above) |
| `Dockerfile` / `nginx-default.conf.template` | rebuild + recreate | rebuild + recreate |

Don't rely on `docker cp` for `app.py` on prod — it survives until the next `docker compose up -d`, then gets clobbered by the image-baked copy. Always rebuild for `app.py` changes.

## Endpoints

Viewer (token-gated unless `viewer_public=true`):
- `GET /` — viewer page
- `GET /api/status` — current source, position, viewer count, `server_unix` for client clock-sync
- `GET /hls/stream.m3u8`, `/hls/run/<id>/init_av.mp4`, `/hls/run/<id>/seg_av_NNNNN.m4s` — HLS manifest + segments (served by nginx, auth-gated via subrequest to Flask)
- `GET /api/_authcheck` — internal nginx `auth_request` target. Also fires `_track_viewer` so the active-viewers list keeps working with `/hls/*` no longer hitting Flask.

Admin (Traefik basicauth on prod; open on dev):
- `POST /admin/api/play` `{path, start_seconds, subtitle_idx?}`
- `POST /admin/api/play_url` `{url}` — yt-dlp resolves; playlists expand into the queue
- `POST /admin/api/seek` `{to_seconds | delta_seconds}`
- `POST /admin/api/pause`, `/admin/api/resume`, `/admin/api/stop`, `/admin/api/skip`
- `GET / POST /admin/api/queue`, `DELETE /admin/api/queue/<idx>`, `POST /admin/api/queue/<idx>/move` `{direction|to}`, `POST /admin/api/queue/clear`, `POST /admin/api/queue/shuffle`
- `GET / POST /admin/api/settings` (`viewer_public`, `auto_fill`)
- `GET / POST / DELETE /admin/api/tokens` — invite-token CRUD
- `GET /admin/api/viewers`, `GET /admin/api/viewers/history` — active viewers + 200-line tail of the connect log
- `GET /admin/api/perf` — ffmpeg PID + uptime, segment count, run state, viewer count
- `GET /admin/api/browse?path=…`

## Features

- **Per-source subtitle burn-in.** Files with English-tagged text subs (SRT/ASS/SSA/mov_text) render burned-in via the libavfilter `subtitles=` filter. PGS bitmap subs are skipped (not renderable). Auto-on for files; pass explicit `subtitle_idx: null` at queue/play time to force off. URLs (yt-dlp) don't get subs.
- **HDR → SDR tonemap.** Sources tagged with PQ (`smpte2084`) or HLG (`arib-std-b67`) transfer go through a CPU-side zscale tonemap chain (`linear → BT.709 SDR → Hable`) before encode. SDR sources skip the chain.
- **4K passthrough.** Set `TARGET_HEIGHT=2160` to keep 4K sources at 4K (still capped to source height — no upscaling).
- **Low-latency mode.** Set `HLS_SEG_TIME=1` + `HLS_LIST_SIZE=30` for ~1.7s sync floor (vs ~3–5s with the 4s default). True LL-HLS via `EXT-X-PART` would need a patched ffmpeg; this is the no-code-change win.
- **YouTube DASH.** yt-dlp returns separate video + audio formats for HD; ffmpeg merges them via two `-i` inputs. (YouTube's muxed `best` tops out at 360p.)

## URL sources (`play_url` / queue-by-URL)

`/admin/api/play_url` and queue-by-URL pipe through yt-dlp. Audited (2026-05-10):

| Source | Status | Notes |
|---|---|---|
| YouTube | ✓ | DASH path picks 1080p video + AAC audio. Playlists expand into the queue. |
| Vimeo | ✓ | Tested with public clips. |
| Dailymotion | ✓ | Picks up to 1080p HLS by default. |
| SoundCloud | ✓ | Audio-only — plays as a stream with no video. |
| Direct `.mp4` URL | ✓\* | Works when the host accepts yt-dlp's User-Agent (`googleapis.com` 403s; most CDNs are fine). |
| Direct `.m3u8` URL | ✓ | Generic HLS extractor; handles ABR masters too. |
| Twitch (clips, VODs) | likely ✓ | Extractor loads cleanly; test URLs were stale at audit time. |
| Reddit (`v.redd.it`) | likely ✓ | Same — extractor present, test URL stale. |

Not supported:
- DRM-protected content (Netflix, Disney+, HBO, etc.) — yt-dlp can't bypass.
- Auth-walled / region-locked / age-gated content — would need a yt-dlp cookies file mounted into the container (not currently wired up).
- Live streams that resolve to non-progressive formats may stutter.

## Env (compose)

| Var | Default | Notes |
|---|---|---|
| `USE_VAAPI` | `1` | Enable hardware H.264 encoder (`h264_vaapi`) |
| `USE_VAAPI_DECODE` | `0` (prod), `0` (dev override in `.env`) | HW decode for HEVC/H.264. Broken on this iGPU — see ROADMAP #7. Workaround pinned off. |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | |
| `VIDEO_BITRATE` | `5M` | Used on the libx264 (non-VAAPI) path; VAAPI uses CQP |
| `VIDEO_QP` | `23` | CQP target for h264_vaapi |
| `AUDIO_BITRATE` | `160k` | |
| `TARGET_HEIGHT` | `1080` (prod), `2160` (dev override) | Output cap. Source-bounded — smaller sources don't get upscaled. |
| `HLS_SEG_TIME` | `4` (prod), `1` (dev override) | Segment seconds. Lower = lower live-edge sync, more files. |
| `HLS_LIST_SIZE` | `6` (prod), `30` (dev override) | Live playlist depth. |

WSGI: gunicorn 23.0 (`-w 1 -k gthread --threads 16 --timeout 120`). One worker shares the in-process state (watcher, composer, viewers dict); 16 threads handle concurrent `/api/_authcheck` + admin requests.

## State

State files live under `/data/` (mounted from `state/livestream{,-dev}/`):
- `tokens.json` — invite tokens
- `playlist.json` — pending queue (items keep `subtitle_idx` if set)
- `settings.json` — `viewer_public`, `auto_fill`
- `state.json` — current source + position (auto-saved every ~10s; restored on startup)
- `viewer_log.jsonl` — append-only connect log

See **ARCHITECTURE.md** for the data flow + design rationale, **ROADMAP.md** for what's left.
