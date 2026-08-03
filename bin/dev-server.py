#!/usr/bin/env python3
"""Run jetstream locally against .devenv/ — no Docker, no nginx.

    bin/dev-setup.sh      # once
    bin/dev-server.py     # every time

Every state file, the media root and the HLS dir are redirected into
.devenv/, so this never touches /data or /hls and never collides with a real
deployment. Tokens and one user account are re-seeded on each boot and the
codes are printed below, so there's always a known way in.

What differs from prod, and why it's fine:
  * nginx is absent. Flask's own `/hls/<path>` fallback route serves segments
    instead, so live playback and VOD both work; you just don't exercise the
    nginx auth_request path or the LAN bypass.
  * gunicorn is absent (Werkzeug threaded server instead). The app's
    background threads still run in-process, which is what matters.
  * Hardware encode is forced off — libx264 only. The NVENC/VAAPI branches of
    _build_ffmpeg_cmd are not covered here.

Env knobs: JETSTREAM_DEV_PORT (8099), JETSTREAM_DEV_HOST (127.0.0.1).
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEV = ROOT / ".devenv"
DATA, MEDIA, HLS = DEV / "data", DEV / "media", DEV / "hls"

VENV_PY = DEV / "venv" / "bin" / "python"

if not VENV_PY.exists():
    sys.exit("No .devenv/venv — run bin/dev-setup.sh first.")
if not MEDIA.exists():
    sys.exit("No .devenv/media — run bin/dev-setup.sh first.")

# Re-exec under the venv interpreter. The shebang is `/usr/bin/env python3`,
# which is the SYSTEM python — it has no flask, so running this file directly
# would die on `import app`. Doing it here means `bin/dev-server.py` just works
# without the caller having to know about (or activate) the venv.
# Compare sys.prefix, NOT the interpreter path: .devenv/venv/bin/python is a
# symlink to the system interpreter, so resolve()-ing both sides makes them
# compare equal and the re-exec silently never happens. A venv's sys.prefix is
# the venv directory itself, which is the actual thing we care about.
if pathlib.Path(sys.prefix) != (DEV / "venv"):
    os.execv(str(VENV_PY), [str(VENV_PY), str(pathlib.Path(__file__).resolve()), *sys.argv[1:]])

for d in (DATA, HLS):
    d.mkdir(parents=True, exist_ok=True)

os.environ.update({
    # State: every _save_X target redirected out of /data.
    "TOKENS_FILE": str(DATA / "tokens.json"),
    "USERS_FILE": str(DATA / "users.json"),
    "USER_SESSIONS_FILE": str(DATA / "sessions.json"),
    "PROGRESS_FILE": str(DATA / "progress.json"),
    "PLAYLIST_FILE": str(DATA / "playlist.json"),
    "RECENT_FILE": str(DATA / "recent.json"),
    "REQUESTS_FILE": str(DATA / "requests.json"),
    "REPORTS_FILE": str(DATA / "reports.json"),
    "SETTINGS_FILE": str(DATA / "settings.json"),
    "STATE_FILE": str(DATA / "state.json"),
    "VIEWER_LOG_FILE": str(DATA / "viewer_log.jsonl"),
    "REACTIONS_DIR": str(DATA / "reactions"),
    "CUSTOM_REACTIONS_FILE": str(DATA / "reactions.json"),
    "POSTERS_DIR": str(DATA / "posters"),
    "SUBS_CACHE_DIR": str(DATA / "subs"),
    "MEDIA_ROOT": str(MEDIA),
    "HLS_DIR": str(HLS),
    # Sonarr/Radarr are optional everywhere; point at a file that will never
    # exist so the arr refresher skips cleanly instead of reading a real one.
    "SONARR_CONFIG": str(DATA / "no-sonarr.xml"),
    "RADARR_CONFIG": str(DATA / "no-radarr.xml"),
    # CPU encode only — a dev box may have no GPU, and the HW branches need
    # real hardware to mean anything.
    "USE_VAAPI": "0",
    "USE_VAAPI_DECODE": "0",
    "USE_NVENC": "0",
    "VOD_FORCE_CPU": "1",
    # On by default here (prod defaults off) so the v2.3 subtitle pickers have
    # something to do — the fixtures carry eng + fre tracks.
    "USE_SUBTITLES": "1",
    "VIEWER_LIBRARY_ROOTS": "Movies=movies,TV Shows=tv",
})

sys.path.insert(0, str(ROOT))
import app as jet  # noqa: E402  (must follow the env setup above)

FRIEND_CODE, VIEWER_CODE = "devfriend", "devviewer"
USERNAME, PASSWORD = "dev", "devpassword"

with jet.tokens_lock:
    jet.tokens.clear()
    # NB: the key is "id", not "token" — see api_create_token. Seeding it as
    # "token" makes _valid_token raise KeyError inside the before_request gate,
    # which surfaces as a 500 on *every* route rather than an auth failure.
    jet.tokens.extend([
        {"id": FRIEND_CODE, "label": "dev (friend)", "level": "friend", "created": 0},
        {"id": VIEWER_CODE, "label": "dev (viewer)", "level": "viewer", "created": 0},
    ])
    jet._save_tokens()

with jet.users_lock:
    jet.users.clear()
    jet.users.append({
        "id": "u_dev",
        "username": USERNAME,
        "scrypt": jet._hash_password(PASSWORD),
        "created": 0, "last_login": None, "disabled": False,
        # Non-null so the admin Users panel renders its provenance chip.
        "invited_by": "dev (friend)", "invite_token": FRIEND_CODE,
    })
    jet._save_users()

with jet.settings_lock:
    # auto_fill would have the watcher immediately start playing something and
    # keep restarting it forever; leave the box idle until you pick a source.
    jet.settings["auto_fill"] = False
    jet.settings["viewer_public"] = False
    jet._save_settings()

host = os.environ.get("JETSTREAM_DEV_HOST", "127.0.0.1")
port = int(os.environ.get("JETSTREAM_DEV_PORT", "8099"))
base = f"http://{host}:{port}"

print(f"""
  jetstream dev server            state: {DEV}

  home screen (no cookie)   {base}/
  watch as a friend         {base}/?t={FRIEND_CODE}      (unlocks /controls)
  watch as a viewer         {base}/?t={VIEWER_CODE}      (watch only)
  controller                {base}/controls?t={FRIEND_CODE}
  admin (no auth locally)   {base}/admin
  sign in                   {base}/login   ->  {USERNAME} / {PASSWORD}

  Nothing plays until you start something: pick a file on /controls or /admin.
  Live subtitles only appear in the UI while a source is running.
""", flush=True)

jet.app.run(host=host, port=port, threaded=True, use_reloader=False)
