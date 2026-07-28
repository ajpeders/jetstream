# jetstream HOWTO

Step-by-step operator guides. Admin API calls go through Traefik basicauth on prod — every `curl` below needs `-u <basicauth-user>` (prompted for the password), or use the admin UI at `https://live.thelunadog.com/admin` which sends it for you.

## Deploy / rebuild

Code changes (app.py, static, frontend) must be baked into the image — `docker cp` gets clobbered on the next `up -d`.

```sh
# frontend changed? build the bundle first (writes static/build/)
npm run build

cd ~/homelab/services
docker compose up -d --build jetstream nginx-jetstream
```

`nginx-default.conf.template` changes ride the same rebuild (envsubst runs at container start). Env-only changes (compose `.env`): `docker compose up -d` without `--build`.

## Mint an invite link (live stream)

Admin UI → Invites panel → label + level → Create. Or:

```sh
curl -u admin -X POST https://live.thelunadog.com/admin/api/tokens \
  -H 'Content-Type: application/json' -d '{"label": "dave", "level": "viewer"}'
```

- `level`: `viewer` (watch only, default) or `friend` (also gets `/controls` + queue/playback control).
- Send the friend `https://live.thelunadog.com/?t=<token>` — first visit sets the `lt` cookie (90 d), after which the bare URL works.
- Revoke: delete the token in the Invites panel (or `DELETE /admin/api/tokens`). The cookie stops validating immediately.

## Create a user account

Accounts are admin-created only — there is no self-registration. Admin UI → Users panel → username + password → Create. Or:

```sh
curl -u admin -X POST https://live.thelunadog.com/admin/api/users \
  -H 'Content-Type: application/json' -d '{"username": "dave", "password": "<initial-pw>"}'
```

Then send them `https://live.thelunadog.com/login`. They log in (username + password, `js_user` cookie, 30 d), land on the live stream, and get `/library` for private VOD. Passwords are scrypt-hashed in `/data/users.json` — never stored plaintext.

Login is rate-limited 5 attempts / 60 s per IP; a locked-out user just waits a minute.

## Reset a user's password

Users panel → the user's row → Reset password. Or:

```sh
curl -u admin -X POST https://live.thelunadog.com/admin/api/users/<id>/password \
  -H 'Content-Type: application/json' -d '{"password": "<new-pw>"}'
```

This **revokes all of the user's sessions** — every logged-in device is kicked and must log in with the new password. That's the feature: a reset is also a "log out everywhere".

## Disable or delete a user

```sh
# disable (reversible — account kept, login rejected, sessions revoked)
curl -u admin -X POST https://live.thelunadog.com/admin/api/users/<id>/disabled \
  -H 'Content-Type: application/json' -d '{"disabled": true}'

# delete (permanent — account + sessions gone)
curl -u admin -X DELETE https://live.thelunadog.com/admin/api/users/<id>
```

Both revoke sessions immediately — the `js_user` cookie dies server-side, mid-stream VOD included (the next segment auth check 403s). Prefer disable when in doubt; re-enable with `{"disabled": false}`.

## Kill a stuck VOD session

Symptoms: user reports VOD won't start and gets `409 vod_capacity`, or `nvidia-smi` shows an encoder pinned by a session nobody's watching.

1. List sessions — Admin UI → Active VOD panel, or:

   ```sh
   curl -u admin https://live.thelunadog.com/admin/api/vod/sessions
   ```

2. Kill the offender:

   ```sh
   curl -u admin -X DELETE https://live.thelunadog.com/admin/api/vod/sessions/<sid>
   ```

   ffmpeg is killed and `/hls/vod/<sid>/` is removed; the slot frees immediately.

Usually you don't need to: the idle reaper kills any session with no segment fetches for `VOD_IDLE_TIMEOUT_S` (120 s) — closed tabs self-clean within ~2 minutes. Manual kill is for "right now" or for a client that's still fetching but shouldn't be.

## Tune VOD env vars

All set in the compose `.env`; apply with `docker compose up -d` (no rebuild).

| Var | Default | Turn it when |
|---|---|---|
| `VOD_MAX_SESSIONS` | `2` | More/fewer concurrent VOD encodes. Each session is a full ffmpeg encode — raise only if CPU/GPU headroom is real. Beyond the cap users get `409 vod_capacity`. |
| `VOD_IDLE_TIMEOUT_S` | `120` | Reaper patience. Lower = abandoned sessions free up faster; too low and a long pause in playback (buffer still draining) can get reaped. |
| `VOD_READRATE` | `2.0` | Input pacing (VOD uses `-readrate`, not `-re`). Higher builds buffer ahead faster but burns more CPU/GPU per session; `1.0` ≈ realtime. |
| `VOD_FORCE_CPU` | `0` | **Set `1` when NVENC sessions run out.** Consumer GPUs cap concurrent NVENC encodes, and live + preroll already use up to two — VOD sessions on top can hit the cap and fail encoder init. `1` pushes all VOD to libx264, reserving NVENC for the live pipeline. Watch CPU: each libx264 VOD encode is heavy. |
| `VOD_HLS_LIST_SIZE` | `900` | Rolling back-buffer (~15 min @ 1 s segments). `/hls` is a ~1.5 GiB tmpfs shared with live — raising this eats tmpfs across all sessions. Rewinds past the window are seeks anyway (kill + `-ss` restart), so bigger rarely helps. |

Related: `USERS_FILE` (`/data/users.json`) and `USER_SESSIONS_FILE` (`/data/sessions.json`) relocate the account/session stores — normally leave alone.

## Truly stop the live stream

`auto_fill` defaults to on, so Stop just makes the watcher pick something else. To actually stop: Settings → `auto_fill` off, then Stop.

See **README.md** for the full endpoint/env reference, **ARCHITECTURE.md** for how it works.
