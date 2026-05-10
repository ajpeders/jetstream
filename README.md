# jetstream

A small Flask + ffmpeg service that broadcasts video files (and yt-dlp-resolvable URLs) to friends over HLS, gated by per-friend invite tokens. Source lives at `~/projects/jetstream/`; deployed via the `livestream` Docker Compose service in `~/homelab/services/livestream/`.

```
~/projects/jetstream/             ← code (this directory)
  app.py                          Flask backend
  static/admin.html               admin UI
  static/viewer.html              viewer UI
  Dockerfile
  ARCHITECTURE.md                 how it works
  ROADMAP.md                      what's next

~/homelab/services/livestream/    ← deployment
  docker-compose.yml              prod (`livestream`) + dev (`livestream-dev`) services
  .env.example                    required env vars
```

## Running

Two containers managed by the same compose file:

| | URL | Image | State dir | HW decode |
|---|---|---|---|---|
| **prod** (`livestream`) | https://live.thelunadog.com | `homelab/livestream:latest` | `state/livestream/` | OFF (kill switch) |
| **dev** (`livestream-dev`) | http://127.0.0.1:8081 | `homelab/livestream:dev` | `state/livestream-dev/` | ON (broken — see ROADMAP) |

```sh
# Prod (rebuilds + recreates; resumes from saved state.json):
cd services/livestream
docker compose up -d --build livestream

# Dev (separate image tag, isolated state):
docker compose up -d --build livestream-dev

# Reach dev from a remote machine via SSH tunnel:
ssh -L 8081:127.0.0.1:8081 ween@192.168.0.176
# then http://localhost:8081 in the browser
```

Static-file changes (HTML/CSS/JS) can be hot-applied without a restart:

```sh
docker cp ~/projects/jetstream/static/viewer.html livestream:/app/static/viewer.html
```

`app.py` changes need a rebuild + recreate (state persistence will resume the position).

## Endpoints

Viewer (token-gated unless `viewer_public=true`):
- `GET /` — viewer page
- `GET /api/status` — current source, position, viewer count, `server_unix` for client clock-sync
- `GET /hls/stream.m3u8` + `/hls/seg_NNNNN.ts` — HLS manifest + segments

Admin (Traefik basicauth on prod; open on dev):
- `POST /admin/api/play` `{path, start_seconds}`
- `POST /admin/api/play_url` `{url}` — yt-dlp resolves; playlists expand into the queue
- `POST /admin/api/seek` `{to_seconds | delta_seconds}`
- `POST /admin/api/pause`, `/admin/api/resume`, `/admin/api/stop`, `/admin/api/skip`
- `GET / POST /admin/api/queue`, `DELETE /admin/api/queue/<idx>`, `POST /admin/api/queue/clear`, `POST /admin/api/queue/shuffle`
- `GET / POST /admin/api/settings` (`viewer_public`, `auto_fill`)
- `GET / POST / DELETE /admin/api/tokens` — invite-token CRUD
- `GET /admin/api/browse?path=…`

## Env (compose)

| Var | Default | Notes |
|---|---|---|
| `USE_VAAPI` | `1` | Enable hardware H.264 encoder (`h264_vaapi`) |
| `USE_VAAPI_DECODE` | `0` (prod), `1` (dev) | Enable HW HEVC/H.264 decode. Broken on this iGPU until colorspace fix lands |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | |
| `VIDEO_BITRATE` | `5M` | Used when not VAAPI; VAAPI uses CQP |
| `VIDEO_QP` | `23` | CQP target for h264_vaapi |
| `AUDIO_BITRATE` | `160k` | |
| `HLS_SEG_TIME` | `4` | Segment seconds |
| `HLS_LIST_SIZE` | `6` | Live playlist depth (24s) |

State files live under `/data/` (mounted from `state/livestream/`):
- `tokens.json` — invite tokens
- `playlist.json` — pending queue
- `settings.json` — viewer_public + auto_fill
- `state.json` — current source + position (auto-saved every ~10s; restored on startup)

See **ARCHITECTURE.md** for the data flow + design rationale, **ROADMAP.md** for outstanding work.
