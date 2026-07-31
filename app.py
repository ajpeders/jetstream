import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

VIEWER_TIMEOUT = 30  # seconds without an HLS request → viewer dropped
# Continuous-watch session cap. A viewer streaming for longer than this is
# dropped: their /hls auth subrequest starts returning 403 so the player
# stalls, and /api/status flags `session_expired` so the page can explain it.
# A fresh session (a reload after the idle-timeout prune, or a new tab) starts
# the clock over — this reclaims forgotten/abandoned tabs, it isn't a ban.
# Set VIEWER_MAX_SESSION_HOURS=0 in compose to disable.
VIEWER_MAX_SESSION_HOURS = float(os.environ.get("VIEWER_MAX_SESSION_HOURS", "8"))
VIEWER_MAX_SESSION_SECS = VIEWER_MAX_SESSION_HOURS * 3600 if VIEWER_MAX_SESSION_HOURS > 0 else 0
TOKEN_COOKIE = "lt"
TOKEN_COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days
# Stable token id used to grant the admin viewer access. Reaching the admin
# page implies Traefik basicauth already validated the request, so we mint
# this on-the-fly the first time admin is served and reuse it thereafter.
# Hidden from the friends list so it never surfaces as a public invite.
ADMIN_TOKEN_ID = "_admin_"
TOKENS_FILE = Path(os.environ.get("TOKENS_FILE", "/data/tokens.json"))
# Invite-token permission tiers. "viewer" tokens can only watch + chat;
# "friend" tokens additionally unlock the playback/queue controls exposed
# under /api/control/* (and the /controls page). The admin token is implicitly
# the top tier. Tokens minted before this field existed default to "viewer".
TOKEN_LEVELS = ("viewer", "friend")
# Levels permitted to hit /api/control/* and load /controls.
CONTROL_LEVELS = frozenset({"friend", "admin"})
TOKEN_LAST_SEEN_SAVE_INTERVAL = 60
# ---- v2: user accounts (admin-created, username+password) ----
# Users are a tier alongside invite tokens, not a replacement: the `js_user`
# session cookie is deliberately distinct from the `lt` invite cookie so one
# person can hold both. Sessions are server-side records (not signed cookies)
# so password reset / account delete can revoke them instantly.
USERS_FILE = Path(os.environ.get("USERS_FILE", "/data/users.json"))
USER_SESSIONS_FILE = Path(os.environ.get("USER_SESSIONS_FILE", "/data/sessions.json"))
USER_SESSION_COOKIE = "js_user"
USER_SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
LOGIN_RATE_WINDOW = 60   # seconds
LOGIN_RATE_MAX = 5       # per-IP login attempts per window
# Invite-gated self-registration: possessing a FRIEND-level code lets its
# holder create an account (viewer-level invites stay watch-only). There is
# no open registration. Codes are reusable — one friend link can cover a
# household; a leaked link is remedied by deleting the token + accounts.
REGISTER_LEVELS = frozenset({"friend"})
INVITE_RATE_WINDOW = 60   # seconds
INVITE_RATE_MAX = 10      # per-IP code-redeem attempts (guards code guessing)
REGISTER_RATE_WINDOW = 3600  # seconds
REGISTER_RATE_MAX = 5        # per-IP signups per hour

# ---- Continue watching (per-user VOD resume) ----
# {user_id: {rel_path: {"position","duration","updated","title"}}}. Written
# from the client's 5 s status poll, so disk writes are throttled the same
# way token last_seen is — the in-memory map is authoritative between flushes.
PROGRESS_FILE = Path(os.environ.get("PROGRESS_FILE", "/data/progress.json"))
PROGRESS_SAVE_INTERVAL = 30      # seconds between disk flushes
PROGRESS_MAX_PER_USER = 100      # newest-first cap; oldest fall off
# Within this many seconds of the end, treat it as finished: drop it from
# Continue watching rather than resuming someone into the credits.
PROGRESS_DONE_TAIL_S = 90
# Below this, resuming is more annoying than helpful (accidental open).
PROGRESS_MIN_RESUME_S = 30
# Both thresholds are clamped to a fraction of the runtime, or short content
# breaks: with a flat 90 s tail every position in a 60 s clip counts as
# "finished" and nothing is ever resumable. A 2 h film keeps the flat values.
PROGRESS_DONE_TAIL_FRACTION = 0.10
PROGRESS_MIN_RESUME_FRACTION = 0.05
PLAYLIST_FILE = Path(os.environ.get("PLAYLIST_FILE", "/data/playlist.json"))
RECENT_FILE = Path(os.environ.get("RECENT_FILE", "/data/recent.json"))
RECENT_LIMIT = int(os.environ.get("RECENT_LIMIT", "50"))
REQUESTS_FILE = Path(os.environ.get("REQUESTS_FILE", "/data/requests.json"))
# Viewer bug reports. Kept until the host deletes them (no TTL — an unhandled
# report shouldn't vanish on its own), but the pile is capped to the newest
# REPORT_MAX so a misbehaving client can't grow it unbounded. Shaped as
# agent-readable JSON so a watcher can poll, triage, and write back later.
REPORTS_FILE = Path(os.environ.get("REPORTS_FILE", "/data/reports.json"))
REPORT_MAX = int(os.environ.get("REPORT_MAX", "500"))
REPORT_RATE_WINDOW = 300    # seconds
REPORT_RATE_MAX = 5         # per-IP reports per window (reports are low-frequency)
# Custom viewer-uploaded emoji reactions. Image files live under REACTIONS_DIR;
# their metadata (id, file, label, uploader) is a JSON sidecar so the set
# survives restarts. Strict caps because this is a viewer-writable surface:
# bounded count, bounded size, allowed image types only.
REACTIONS_DIR = Path(os.environ.get("REACTIONS_DIR", "/data/reactions"))
CUSTOM_REACTIONS_FILE = Path(os.environ.get("CUSTOM_REACTIONS_FILE", "/data/reactions.json"))
CUSTOM_REACTION_MAX = int(os.environ.get("CUSTOM_REACTION_MAX", "40"))   # total set size
CUSTOM_REACTION_MAX_BYTES = int(os.environ.get("CUSTOM_REACTION_MAX_BYTES", str(512 * 1024)))
CUSTOM_REACTION_UPLOAD_WINDOW = 600   # seconds
CUSTOM_REACTION_UPLOAD_MAX = 5        # uploads per IP per window
# Pending viewer "request to queue" entries auto-expire after this many seconds
# if the host hasn't approved or denied them. Without this the list grows
# forever (the only way it shrinks is explicit host action), and a friend who
# requested something weeks ago has long since lost interest. Swept lazily on
# every list view — no background timer.
REQUEST_TTL_SECS = 7 * 24 * 60 * 60  # 7 days
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/data/settings.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
STATE_SAVE_INTERVAL = float(os.environ.get("STATE_SAVE_INTERVAL", "5"))
VIEWER_LOG_FILE = Path(os.environ.get("VIEWER_LOG_FILE", "/data/viewer_log.jsonl"))

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
HLS_DIR = Path(os.environ.get("HLS_DIR", "/hls")).resolve()
VIEWER_LIBRARY_ROOTS_RAW = os.environ.get("VIEWER_LIBRARY_ROOTS", "Movies=movies,TV Shows=tv")
VIEWER_SEASON_MARKER = "__season__"
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
USE_VAAPI = os.environ.get("USE_VAAPI", "1") == "1"
USE_VAAPI_DECODE = os.environ.get("USE_VAAPI_DECODE", "1") == "1"
# NVIDIA NVENC/NVDEC path. When on, takes precedence over VAAPI: decode on
# GPU via CUDA when the source codec is supported, encode via h264_nvenc.
# Requires nvidia-container-toolkit and a `--gpus all` / `deploy.resources`
# block on the container.
USE_NVENC = os.environ.get("USE_NVENC", "0") == "1"
# ffmpeg codec_name values supported by VAAPI VLD on this GPU (verified via vainfo).
HWACCEL_DECODE_CODECS = {"h264", "hevc", "vp8", "vp9", "mpeg2video"}
# Codecs the NVDEC engine on this card (Ampere GA106 / RTX 3050) decodes.
# AV1 decode landed on Ampere; 10-bit HEVC is fine. Dolby Vision (dvhe) falls
# through to CPU — NVDEC ignores DV metadata and would tonemap incorrectly.
NVDEC_DECODE_CODECS = {"h264", "hevc", "vp8", "vp9", "mpeg2video", "av1"}
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "5M")
VIDEO_QP = os.environ.get("VIDEO_QP", "23")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "160k")
# Output cap. 1080p is the default — fits a 5 Mbps target comfortably and
# keeps the encode cheap on CPU. Set to 2160 for 4K passthrough on capable
# hardware; output still shrinks to source height when source is smaller.
TARGET_HEIGHT = int(os.environ.get("TARGET_HEIGHT", "1080"))
HLS_SEG_TIME = os.environ.get("HLS_SEG_TIME", "1")
# 24 segments × 1s = 24s lookback, matching the low-latency player tuning.
HLS_LIST_SIZE = os.environ.get("HLS_LIST_SIZE", "24")

# ---- v2: private per-user VOD sessions ----
# Each logged-in user can run one on-demand transcode into /hls/vod/<sid>/.
# Sessions are capped (each is a full ffmpeg encode competing with the live
# stream for NVENC slots) and reaped when segment fetches stop arriving.
VOD_DIR_BASE = HLS_DIR / "vod"
VOD_MAX_SESSIONS = int(os.environ.get("VOD_MAX_SESSIONS", "2"))
VOD_IDLE_TIMEOUT_S = int(os.environ.get("VOD_IDLE_TIMEOUT_S", "120"))
# VOD drops -re; -readrate caps transcode speed (2.0 = 2x realtime) so a film
# doesn't peg the encoder. 0 = unlimited.
VOD_READRATE = os.environ.get("VOD_READRATE", "2.0")
# Force VOD encodes onto libx264, reserving NVENC sessions for live + preroll.
VOD_FORCE_CPU = os.environ.get("VOD_FORCE_CPU", "0") == "1"
# VOD keeps the FULL playlist (-hls_list_size 0, no delete_segments): with
# the -readrate paced encoder running ahead of the 1x viewer, ANY rolling
# window eventually slides past the playhead and deletes the segment the
# player needs next (review finding — deterministic stall ~15 min in). Only
# affordable because /hls/vod is a disk-backed volume (docker-compose.yml),
# NOT part of the ~1.5 GiB /hls tmpfs.

STREAM_QUALITY_PRESETS = {
    "default": {
        "label": "Default",
        "height": TARGET_HEIGHT,
        "video_bitrate": VIDEO_BITRATE,
        "bufsize": "10M",
    },
    "high": {
        "label": "1080p High",
        "height": 1080,
        "video_bitrate": "5M",
        "bufsize": "10M",
    },
    "balanced": {
        "label": "720p Balanced",
        "height": 720,
        "video_bitrate": "2500k",
        "bufsize": "5M",
    },
    "data_saver": {
        "label": "480p Data Saver",
        "height": 480,
        "video_bitrate": "1200k",
        "bufsize": "2400k",
    },
}

# ── Adaptive-bitrate (ABR) ladder ────────────────────────────────────────
# Opt-in via ABR_LADDER=1. When OFF (default) the server emits a single
# rendition exactly as before — single-rendition is the safe fallback. When
# ON, a SINGLE ffmpeg decode fans out to N NVENC (or CPU/VAAPI) outputs in one
# process; the composer publishes a real multi-variant master playlist whose
# variants each accumulate runs+discontinuities like the legacy single stream.
# hls.js then auto-selects/downshifts based on the viewer's bandwidth.
#
# The ladder reuses STREAM_QUALITY_PRESETS heights/bitrates (highest → lowest).
# In ABR mode the per-session `stream_quality` admin setting is ignored — the
# player picks the rendition, not the admin.
ABR_LADDER = os.environ.get("ABR_LADDER", "0") == "1"
ABR_LADDER_KEYS = ["high", "balanced", "data_saver"]


def _abr_ladder() -> list[dict]:
    """The active ABR ladder as a list of preset dicts, highest quality first.
    Variant index in the master playlist == index in this list."""
    return [
        STREAM_QUALITY_PRESETS[k]
        for k in ABR_LADDER_KEYS
        if k in STREAM_QUALITY_PRESETS
    ]


def _bitrate_to_bps(value) -> int:
    """Parse an ffmpeg bitrate string ("5M", "2500k", "1200000") to bits/sec.
    Used to fill EXT-X-STREAM-INF BANDWIDTH in the master playlist."""
    s = str(value).strip().lower()
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except ValueError:
        return 0


VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi"}

# Cover art via Sonarr + Radarr. Both run on the same Docker network; we
# read their API keys from the read-only-mounted config.xml files and
# proxy poster images through jetstream so the UI doesn't have to deal
# with arr URLs or keys. Posters are cached on disk forever (per-source
# hash) — Sonarr/Radarr don't refresh artwork often enough to matter and
# stale posters are a minor cosmetic issue, not a correctness one.
POSTERS_DIR = Path(os.environ.get("POSTERS_DIR", "/data/posters"))
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_CONFIG = Path(os.environ.get("SONARR_CONFIG", "/etc/sonarr_config.xml"))
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_CONFIG = Path(os.environ.get("RADARR_CONFIG", "/etc/radarr_config.xml"))
# How often the background refresher re-polls arr for series/movie inventory.
# Picks up newly-added shows / movies without a restart.
ARR_REFRESH_SECS = 30 * 60

# Each ffmpeg "run" writes its segments + init segment + per-run index playlist
# into its own subdir under /hls/run/<run_id>/. The composer thread stitches a
# unified /hls/stream.m3u8 from all active+recent runs, with EXT-X-DISCONTINUITY
# between them. Viewers subscribe to the same stream.m3u8 forever — source
# changes, seek, pause/resume etc. spawn a new run dir but the public manifest
# just gets new entries appended, so the player never tears down.
HLS_DIR.mkdir(parents=True, exist_ok=True)
RUN_DIR_BASE = HLS_DIR / "run"
RUN_DIR_BASE.mkdir(parents=True, exist_ok=True)
COMPOSER_TICK_S = 0.5

app = Flask(__name__, static_folder="static", static_url_path="")

state_lock = threading.Lock()
current_proc: subprocess.Popen | None = None
# Source dict: {"type": "file"|"url", "ref": str, "title": str, "duration": float|None, "is_live": bool}
#   file: ref is the path relative to MEDIA_ROOT
#   url:  ref is the original input URL (so re-resolving on seek picks up fresh signed URLs)
current_source: dict | None = None
current_start_offset: float = 0.0   # seconds into the source at which broadcast began
current_start_time: float = 0.0     # wall-clock time ffmpeg was launched
current_paused: bool = False
paused_position: float = 0.0        # frozen position while paused
# Monotonic run id. Increments on every ffmpeg (re)start. Each ffmpeg owns
# /hls/run/<run_id>/ and writes its own per-run idx.m3u8 there. The composer
# stitches a single /hls/stream.m3u8 across all runs — clients never see the
# id change.
active_run_id: int | None = None
next_run_id: int = 1
finished_run_ids: set[int] = set()  # runs whose ffmpeg has exited; composer cleans dirs once their segments roll out

# Pre-roll: when a source has a known finite duration, the watcher spawns the
# next queue item's ffmpeg PREROLL_LEAD_SECS before the current EOF. Both
# ffmpegs run briefly in parallel, the composer naturally stitches their run
# dirs with an EXT-X-DISCONTINUITY, and the player keeps fresh segments
# landing right through the transition (instead of seeing a 3-4s gap — the
# 1s watcher poll + ffprobe + ffmpeg startup + first-GOP wall time — that
# drains the live-edge buffer and stutters out the last several seconds).
# 6s gives the new run enough lead to produce 2-3 segments before old EOF;
# at 1080p NVENC the two encodes share the GPU's single NVENC engine with
# headroom to spare. All four globals are guarded by state_lock.
PREROLL_LEAD_SECS = 6
preroll_proc: "subprocess.Popen | None" = None
preroll_source: dict | None = None
preroll_run_id: int | None = None
# True when preroll_source was popped from the playlist (vs. drawn from
# auto-fill). Cancellation restores it to the queue head; auto-fill picks
# don't need restoring (the next watcher tick will draw a fresh one).
preroll_source_from_queue: bool = False

# Vote-to-skip: client IPs that have voted to skip the *current* item. Keyed by
# IP to match _viewer_count()'s denominator (both IP-based, so a NAT'd house
# counts as one). Cleared whenever a new source starts (see _start_stream).
skip_votes_lock = threading.Lock()
skip_votes: set[str] = set()

# Composer's view: which media-sequence numbers we've assigned per (run, file)
# and which discontinuity-sequence each run corresponds to. Persistent so
# MEDIA-SEQUENCE / DISCONTINUITY-SEQUENCE advance monotonically across composer
# ticks even as segments roll off the front of the playlist.
_composer_state_lock = threading.Lock()
_composer_state = {
    "seg_to_seq": {},      # (run_id, filename) -> media-sequence number
    "run_to_disc": {},     # run_id -> discontinuity-sequence at its boundary
    "next_seq": 0,
    "next_disc": 1,        # first run gets disc 0; each subsequent run gets next_disc++
    "first_run_seen": False,
}

viewers_lock = threading.Lock()
viewers: dict[str, float] = {}
# Parallel to `viewers`: ip -> last-seen token label, or None for no/invalid
# token. Refreshed on every request that resolves a label so a friend who
# revokes/rotates their token sees the change reflected in the admin list.
viewer_labels: dict[str, str | None] = {}
# Parallel to `viewers`: ip -> wall-clock time the current continuous session
# began. Set when an ip first appears (or reappears after the idle prune) and
# cleared alongside `viewers` when it ages out. Drives VIEWER_MAX_SESSION_SECS.
viewer_session_start: dict[str, float] = {}

# Anonymous live chat. In-memory only — survives no restarts (per roadmap).
# Each viewer's tab generates its own opaque session id (`sid`) client-side
# and includes it in /chat/send; the server doesn't validate it, just stores
# alongside the message so the UI can paint a per-session color dot. No
# usernames anywhere.
import collections as _collections
CHAT_BUFFER_SIZE = 200      # ring-buffer depth visible to late-joiners
CHAT_MSG_MAX_LEN = 500
CHAT_NAME_MAX_LEN = 24      # caps display name; keeps a single message line scannable
CHAT_RATE_WINDOW = 30       # seconds
CHAT_RATE_MAX = 10          # per-IP messages per window
chat_lock = threading.Lock()
chat_messages = _collections.deque(maxlen=CHAT_BUFFER_SIZE)
chat_next_id = 1
# IP -> [timestamps within CHAT_RATE_WINDOW]. Pruned lazily on each send.
chat_rate: dict[str, list[float]] = {}
# sid -> wall-clock expiry. Empty after restart (chat is intentionally
# in-memory; mutes are short-term moderation, not durable bans).
chat_mutes: dict[str, float] = {}
# Ids the admin deleted from `chat_messages`. Surfaces via /chat/recent so
# clients that already saw the message hide it on next poll. Kept small by
# trimming anything older than the oldest live ring entry (clients can't
# show ids outside that window anyway).
chat_deleted_ids: set[int] = set()

# Emoji reactions — ephemeral floaty taps shown over everyone's video. Same
# ring-buffer + poll shape as chat, but /reactions/recent only returns ones
# from the last REACTION_RECENT_WINDOW seconds so a fresh poller animates each
# reaction once and a late joiner doesn't get a backlog dumped on them.
# The built-in set is fixed server-side so a client can't inject arbitrary
# strings into everyone's overlay; custom reactions are referenced by id and
# resolved against the validated custom-reaction set (see custom_reactions).
# The "quick" reactions the in-player tap bar shows. The panel picker isn't
# limited to these — viewers can react with ANY emoji (see _is_emoji); this is
# just the fast set surfaced over the video.
REACTION_EMOJIS = ("😂", "❤️", "🔥", "😮", "👏", "💀")
REACTION_BUFFER_SIZE = 100
REACTION_RECENT_WINDOW = 6  # seconds of lookback in /reactions/recent
REACTION_RATE_WINDOW = 10
REACTION_RATE_MAX = 25      # per-IP reactions per window (spammy by nature)
reactions_lock = threading.Lock()
reactions = _collections.deque(maxlen=REACTION_BUFFER_SIZE)
reaction_next_id = 1
reaction_rate: dict[str, list[float]] = {}

# Custom viewer-uploaded reactions. Each entry:
#   {"id": int, "file": str, "mime": str, "label": str|None,
#    "sid": str, "ip": str, "ts": float}
# `file` is a content-hashed name under REACTIONS_DIR (never the client's).
custom_reactions_lock = threading.Lock()
custom_reactions: list[dict] = []
custom_reaction_next_id = 1
custom_reaction_upload_rate: dict[str, list[float]] = {}


tokens_lock = threading.Lock()
tokens: list[dict] = []

playlist_lock = threading.Lock()
playlist: list[dict] = []

recent_lock = threading.Lock()
recent_items: list[dict] = []

# Viewer "request to queue": watch-only viewers can't drive playback, but they
# can suggest media that lands in a pending list the host approves/denies. Each
# request: {"id", "ts", "path", "title", "requester", "ip"}. Persisted so a
# restart doesn't drop a backlog the host hasn't gotten to yet.
REQUEST_RATE_WINDOW = 60    # seconds
REQUEST_RATE_MAX = 10       # per-IP requests per window
requests_lock = threading.Lock()
media_requests: list[dict] = []
request_next_id = 1
# IP -> [timestamps within REQUEST_RATE_WINDOW]. Pruned lazily on each request.
request_rate: dict[str, list[float]] = {}

# Viewer bug reports — same persisted-list shape as media_requests.
reports_lock = threading.Lock()
reports: list[dict] = []
report_next_id = 1
# IP -> [timestamps within REPORT_RATE_WINDOW]. Pruned lazily on each report.
report_rate: dict[str, list[float]] = {}


def _warn_state_load_failed(name: str, path: Path, err: Exception) -> None:
    """Loud stderr warning when a JSON state file fails to parse. Each loader
    previously swallowed the exception silently and reset to an empty default,
    which on a corrupted tokens file would wipe every invite on the next
    restart with no signal that anything went wrong. Print enough to find the
    incident in `docker logs`: the state name, the path, and the error."""
    print(
        f"WARN: failed to load {name} state from {path}: "
        f"{type(err).__name__}: {err}; falling back to empty defaults",
        file=sys.stderr,
        flush=True,
    )


def _load_tokens():
    global tokens
    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            return
        except Exception as e:
            _warn_state_load_failed("tokens", TOKENS_FILE, e)
    tokens = []


def _save_tokens():
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def _valid_token(t: str | None) -> bool:
    if not t:
        return False
    with tokens_lock:
        return any(x["id"] == t for x in tokens)


def _token_level(t: str | None) -> str | None:
    """Permission tier for a token id: "admin", "friend", or "viewer".
    None if the token is missing/unknown. Tokens predating the level field
    are treated as "viewer"."""
    if not t:
        return None
    if t == ADMIN_TOKEN_ID:
        return "admin"
    with tokens_lock:
        for x in tokens:
            if x["id"] == t:
                return x.get("level", "viewer")
    return None


def _token_label(t: str | None) -> str | None:
    """Human label for a token id, or None if unknown. The admin pseudo-token
    reports as "admin"; real invite tokens return their host-assigned label."""
    if not t:
        return None
    if t == ADMIN_TOKEN_ID:
        return "admin"
    with tokens_lock:
        for x in tokens:
            if x["id"] == t:
                return x.get("label")
    return None


def _mark_token_seen(t: str | None, now: float | None = None) -> str | None:
    """Record invite-token activity, throttling disk writes for hot HLS auth."""
    if not t or t == ADMIN_TOKEN_ID:
        return None
    now = now or time.time()
    with tokens_lock:
        for x in tokens:
            if x.get("id") != t:
                continue
            x["last_seen"] = now
            last_saved = float(x.get("last_seen_saved") or 0)
            if now - last_saved >= TOKEN_LAST_SEEN_SAVE_INTERVAL:
                x["last_seen_saved"] = now
                _save_tokens()
            return x.get("label")
    return None


def _caller_can_control() -> bool:
    """True if the request's token cookie grants playback control."""
    return _token_level(request.cookies.get(TOKEN_COOKIE)) in CONTROL_LEVELS


# ==== v2: users & sessions =================================================
# Admin-created accounts (no self-registration). Follows the tokens idiom:
# module list + lock + load/save to /data. Passwords are scrypt-hashed
# (stdlib only — no new deps); sessions are server-side so they can be
# revoked on password reset / delete / disable.

users_lock = threading.Lock()
users: list[dict] = []
# {"id","username","scrypt":{"salt","hash","n","r","p"},"created",
#  "last_login","disabled"}

user_sessions_lock = threading.Lock()
user_sessions: dict[str, dict] = {}
# sid -> {"user_id","created","expires"}

# IP -> [timestamps within LOGIN_RATE_WINDOW]. Pruned lazily per attempt.
login_rate_lock = threading.Lock()
login_rate: dict[str, list[float]] = {}

# Same shape for the two pre-auth home-screen endpoints. Separate buckets so
# a burst of bad code guesses can't lock out a legitimate signup.
invite_rate_lock = threading.Lock()
invite_rate: dict[str, list[float]] = {}
register_rate_lock = threading.Lock()
register_rate: dict[str, list[float]] = {}

# Continue watching. {user_id: {rel_path: {...}}}
progress_lock = threading.Lock()
watch_progress: dict[str, dict[str, dict]] = {}
_progress_last_saved = 0.0


def _atomic_write_json(path: Path, obj) -> None:
    """Write-then-rename so a crash mid-write can't leave a truncated JSON
    file. The pre-v2 stores write in place (a corrupt tokens.json at least
    fails loudly via _warn_state_load_failed); the credential stores get the
    stronger guarantee from day one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _load_users():
    global users
    if USERS_FILE.exists():
        try:
            users = json.loads(USERS_FILE.read_text())
            return
        except Exception as e:
            _warn_state_load_failed("users", USERS_FILE, e)
    users = []


def _save_users():
    _atomic_write_json(USERS_FILE, users)


def _load_user_sessions():
    global user_sessions
    if USER_SESSIONS_FILE.exists():
        try:
            user_sessions = json.loads(USER_SESSIONS_FILE.read_text())
            return
        except Exception as e:
            _warn_state_load_failed("user sessions", USER_SESSIONS_FILE, e)
    user_sessions = {}


def _save_user_sessions():
    """Persist sessions, pruning expired ones so the file can't grow
    unbounded. Caller holds user_sessions_lock."""
    now = time.time()
    stale = [sid for sid, rec in user_sessions.items()
             if float(rec.get("expires") or 0) < now]
    for sid in stale:
        del user_sessions[sid]
    _atomic_write_json(USER_SESSIONS_FILE, user_sessions)


_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def _hash_password(pw: str) -> dict:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                       p=_SCRYPT_P, dklen=32)
    return {"salt": salt.hex(), "hash": h.hex(),
            "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}


def _verify_password(pw: str, rec: dict) -> bool:
    try:
        h = hashlib.scrypt(
            pw.encode(), salt=bytes.fromhex(rec["salt"]),
            n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]), dklen=32)
        return hmac.compare_digest(h.hex(), rec["hash"])
    except Exception:
        return False


def _session_user() -> dict | None:
    """The user record behind the request's `js_user` cookie, or None.
    Mirrors _token_level as the capability chokepoint for the user tier:
    unknown/expired session or disabled user both read as logged-out."""
    sid = request.cookies.get(USER_SESSION_COOKIE)
    if not sid:
        return None
    now = time.time()
    with user_sessions_lock:
        rec = user_sessions.get(sid)
        if not rec or float(rec.get("expires") or 0) < now:
            return None
        uid = rec["user_id"]
    with users_lock:
        for u in users:
            if u["id"] == uid and not u.get("disabled"):
                return u
    return None


def _create_user_session(user_id: str) -> str:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with user_sessions_lock:
        user_sessions[sid] = {
            "user_id": user_id, "created": now, "expires": now + USER_SESSION_TTL,
        }
        _save_user_sessions()
    return sid


def _revoke_user_sessions(user_id: str) -> None:
    """Drop every session for a user — password reset, disable, delete."""
    with user_sessions_lock:
        dead = [sid for sid, rec in user_sessions.items()
                if rec.get("user_id") == user_id]
        for sid in dead:
            del user_sessions[sid]
        _save_user_sessions()


def _ip_rate_check(bucket: dict[str, list[float]], lock: threading.Lock,
                   ip: str, window: float, max_n: int,
                   record: bool = True) -> bool:
    """Generic per-IP rolling-window rate check (the shape _chat_rate_check
    and friends each hand-roll). Records the attempt timestamp on success;
    sweeps fully-stale rows each call so scanner IPs that never return
    don't leave permanent entries.

    `record=False` peeks without consuming budget — for endpoints that should
    only charge on the outcome worth limiting (e.g. registration charges a
    created account, not a mistyped password; otherwise two typos lock a
    household out for an hour)."""
    now = time.time()
    cutoff = now - window
    with lock:
        for stale in [k for k, v in bucket.items()
                      if k != ip and (not v or v[-1] <= cutoff)]:
            del bucket[stale]
        timestamps = [t for t in bucket.get(ip, []) if t > cutoff]
        if len(timestamps) >= max_n:
            bucket[ip] = timestamps
            return False
        if record:
            timestamps.append(now)
        bucket[ip] = timestamps
        return True


def _trusted_client_ip() -> str:
    """Client IP for SECURITY decisions (rate limiting). _client_ip trusts
    the leftmost X-Forwarded-For entry, which the CLIENT controls — fine for
    viewer-tracking labels, useless against an attacker rotating forged XFF
    values to dodge a limiter. X-Real-IP is set by our own nginx from its
    realip-resolved $remote_addr (Traefik appends the true peer last, nginx
    walks it recursively), so it can't be forged from outside; fall back to
    the socket peer when nginx isn't in front (dev/test client)."""
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


# ---- Continue watching store ----

def _load_progress():
    global watch_progress
    if PROGRESS_FILE.exists():
        try:
            loaded = json.loads(PROGRESS_FILE.read_text())
            watch_progress = loaded if isinstance(loaded, dict) else {}
            return
        except Exception as e:
            _warn_state_load_failed("watch progress", PROGRESS_FILE, e)
    watch_progress = {}


def _save_progress_locked(force: bool = False) -> None:
    """Flush progress to disk at most every PROGRESS_SAVE_INTERVAL. Callers
    hold progress_lock. Progress arrives on every client's 5 s poll, so
    writing through each time would hammer the disk for data that's cheap to
    lose — worst case a viewer re-watches the last few seconds."""
    global _progress_last_saved
    now = time.time()
    if not force and now - _progress_last_saved < PROGRESS_SAVE_INTERVAL:
        return
    _progress_last_saved = now
    _atomic_write_json(PROGRESS_FILE, watch_progress)


def _progress_record(user_id: str, rel_path: str, position: float,
                     duration: float | None, title: str | None) -> None:
    """Remember where `user_id` is in `rel_path`. Finishing (within
    PROGRESS_DONE_TAIL_S of the end) clears the entry instead of storing it,
    so a watched film leaves Continue watching by itself."""
    if not user_id or not rel_path:
        return
    position = max(0.0, float(position or 0.0))
    if duration and duration > 0:
        tail = min(PROGRESS_DONE_TAIL_S, duration * PROGRESS_DONE_TAIL_FRACTION)
        floor = min(PROGRESS_MIN_RESUME_S, duration * PROGRESS_MIN_RESUME_FRACTION)
        finished = position >= duration - tail
    else:
        floor = PROGRESS_MIN_RESUME_S
        finished = False
    with progress_lock:
        mine = watch_progress.setdefault(user_id, {})
        if finished or position < floor:
            # Also clears a previously-saved position: rewinding to the start
            # and leaving means "I'm done with this", not "resume at 0:05".
            if mine.pop(rel_path, None) is not None:
                _save_progress_locked(force=True)
            return
        mine[rel_path] = {
            "position": position,
            "duration": duration,
            "updated": time.time(),
            "title": title or Path(rel_path).name,
        }
        if len(mine) > PROGRESS_MAX_PER_USER:
            for stale in sorted(mine, key=lambda k: mine[k].get("updated", 0),
                                )[:len(mine) - PROGRESS_MAX_PER_USER]:
                del mine[stale]
        _save_progress_locked()


def _progress_lookup(user_id: str, rel_path: str) -> float | None:
    """Saved position for this user+file, or None."""
    with progress_lock:
        rec = (watch_progress.get(user_id) or {}).get(rel_path)
        return float(rec["position"]) if rec else None


def _progress_forget(user_id: str, rel_path: str) -> bool:
    with progress_lock:
        removed = (watch_progress.get(user_id) or {}).pop(rel_path, None)
        if removed is not None:
            _save_progress_locked(force=True)
        return removed is not None


# ==== v2: vod session registry (engine lands in WP-2A) =====================
# sid -> {"user_id","rel_path","title","proc","start_offset","started_at",
#         "last_access","duration","status","generation"}
vod_lock = threading.Lock()
vod_sessions: dict[str, dict] = {}


def _vod_session_dir(sid: str) -> Path:
    return VOD_DIR_BASE / sid


def _vod_detach_locked(sid: str) -> dict | None:
    """Pop a session out of the registry and return it for disposal. Caller
    holds vod_lock. Deliberately does NOT touch the process or the dir — the
    per-segment _authcheck_vod heartbeat contends on vod_lock, so anything
    slow (proc.wait, rmtree) must happen in _vod_dispose AFTER the lock is
    released (review finding: a 5 s proc.wait under this lock stalled every
    viewer's segment auth)."""
    return vod_sessions.pop(sid, None)


def _vod_dispose(sid: str, sess: dict | None) -> None:
    """Terminate a detached session's ffmpeg and remove its dir. Caller must
    NOT hold vod_lock."""
    if not sess:
        return
    proc = sess.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    shutil.rmtree(_vod_session_dir(sid), ignore_errors=True)


def _vod_kill_session(sid: str) -> bool:
    """Detach + dispose in one call. Returns False for an unknown sid."""
    with vod_lock:
        sess = _vod_detach_locked(sid)
    if sess is None:
        return False
    _vod_dispose(sid, sess)
    return True


def _vod_kill_sessions_for_user(user_id: str) -> None:
    with vod_lock:
        dead = [(sid, _vod_detach_locked(sid))
                for sid in [s for s, rec in vod_sessions.items()
                            if rec.get("user_id") == user_id]]
    for sid, sess in dead:
        _vod_dispose(sid, sess)


def _load_playlist():
    global playlist
    if PLAYLIST_FILE.exists():
        try:
            playlist = json.loads(PLAYLIST_FILE.read_text())
            return
        except Exception as e:
            _warn_state_load_failed("playlist", PLAYLIST_FILE, e)
    playlist = []


def _save_playlist():
    PLAYLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYLIST_FILE.write_text(json.dumps(playlist, indent=2))


def _load_recent():
    global recent_items
    if RECENT_FILE.exists():
        try:
            loaded = json.loads(RECENT_FILE.read_text())
            recent_items = loaded if isinstance(loaded, list) else []
            return
        except Exception as e:
            _warn_state_load_failed("recent", RECENT_FILE, e)
    recent_items = []


def _save_recent():
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(json.dumps(recent_items[:RECENT_LIMIT], indent=2))


def _source_key(source: dict | None) -> tuple[str | None, str | None]:
    if not source:
        return (None, None)
    return (source.get("type"), source.get("ref"))


def _recent_record(source: dict | None, position: float | None = None) -> None:
    """Record a source that just left active playback. Adjacent duplicates are
    ignored so seek/resume operations don't spam the list."""
    if not source or not source.get("type") or not source.get("ref"):
        return
    entry = {
        "ts": time.time(),
        "source": {
            k: v for k, v in dict(source).items()
            if k in {"type", "ref", "title", "duration", "is_live", "subtitle_idx"}
        },
    }
    if position is not None:
        entry["position_seconds"] = position
    with recent_lock:
        if recent_items and _source_key(recent_items[0].get("source")) == _source_key(source):
            recent_items[0] = entry
        else:
            recent_items.insert(0, entry)
            del recent_items[RECENT_LIMIT:]
        _save_recent()


def _load_requests():
    global media_requests, request_next_id
    if REQUESTS_FILE.exists():
        try:
            media_requests = json.loads(REQUESTS_FILE.read_text())
            # Resume the id counter past the highest persisted id so approvals
            # by id stay unambiguous across restarts.
            request_next_id = max((r.get("id", 0) for r in media_requests), default=0) + 1
            return
        except Exception as e:
            _warn_state_load_failed("requests", REQUESTS_FILE, e)
    media_requests = []


def _save_requests():
    REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REQUESTS_FILE.write_text(json.dumps(media_requests, indent=2))


def _expire_old_requests() -> int:
    """Drop pending requests older than REQUEST_TTL_SECS. Lazy sweep called
    from /admin/api/requests (admin view) and api_request_add (so a fresh
    request never collides with a ghost duplicate). Caller holds
    requests_lock. Returns count expired. Also drops dead entries from
    `request_rate` while we're already inside the lock — same bounded-by-
    recent-IPs property as the chat/reaction rate dicts."""
    global media_requests
    cutoff = time.time() - REQUEST_TTL_SECS
    before = len(media_requests)
    media_requests = [r for r in media_requests if r.get("ts", 0) >= cutoff]
    expired = before - len(media_requests)
    if expired:
        _save_requests()
    rate_cutoff = time.time() - REQUEST_RATE_WINDOW
    for ip in [k for k, ts in request_rate.items() if not any(t > rate_cutoff for t in ts)]:
        del request_rate[ip]
    return expired


def _load_reports():
    global reports, report_next_id
    if REPORTS_FILE.exists():
        try:
            reports = json.loads(REPORTS_FILE.read_text())
            report_next_id = max((r.get("id", 0) for r in reports), default=0) + 1
            return
        except Exception as e:
            _warn_state_load_failed("reports", REPORTS_FILE, e)
    reports = []


def _save_reports():
    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_FILE.write_text(json.dumps(reports, indent=2))


def _load_custom_reactions():
    global custom_reactions, custom_reaction_next_id
    if CUSTOM_REACTIONS_FILE.exists():
        try:
            loaded = json.loads(CUSTOM_REACTIONS_FILE.read_text())
            # Drop entries whose backing image vanished (manual cleanup, restore
            # gaps) so the bar never points at a 404.
            custom_reactions = [r for r in loaded if (REACTIONS_DIR / r.get("file", "")).exists()]
            custom_reaction_next_id = max((r.get("id", 0) for r in custom_reactions), default=0) + 1
            return
        except Exception as e:
            _warn_state_load_failed("custom_reactions", CUSTOM_REACTIONS_FILE, e)
    custom_reactions = []


def _save_custom_reactions():
    CUSTOM_REACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_REACTIONS_FILE.write_text(json.dumps(custom_reactions, indent=2))


# Magic-byte sniff → (extension, mime). We don't trust the client's
# Content-Type or filename: only these four raster types are accepted, and the
# stored name is derived from the content hash, never from the upload.
def _sniff_image(data: bytes) -> tuple[str, str] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("gif", "image/gif")
    if data[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


def _iso_now() -> str:
    """UTC ISO-8601 timestamp (no microseconds) — the human/agent-readable
    twin of the float `ts`, so a triage agent doesn't have to convert."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _coerce_float(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _stream_health_snapshot() -> dict:
    """Compact, agent-readable snapshot of what the encoder/stream were doing,
    attached to a bug report so a watcher agent can correlate the report
    against server state without re-deriving it. No external probes — every
    field is O(small)."""
    with state_lock:
        proc = current_proc
        source = current_source
        start_time = current_start_time
        paused = current_paused
        run_id = active_run_id
        running = proc is not None and proc.poll() is None
        position = _current_position(running=running)
    uptime = (time.time() - start_time) if (running and start_time > 0) else None
    segments = 0
    try:
        for d in RUN_DIR_BASE.iterdir():
            if d.is_dir():
                for f in d.iterdir():
                    if f.suffix == ".m4s":
                        segments += 1
    except FileNotFoundError:
        pass
    return {
        "ffmpeg_alive": running,
        "ffmpeg_uptime_seconds": round(uptime, 1) if uptime is not None else None,
        "active_run_id": run_id,
        "segments_on_disk": segments,
        "viewers": _viewer_count(),
        "paused": paused,
        "source_type": (source or {}).get("type"),
        "is_live": (source or {}).get("is_live", False),
        "title": (source or {}).get("title"),
        "path": (source or {}).get("ref"),
        "server_position_seconds": round(position, 1) if position is not None else None,
        "duration_seconds": (source or {}).get("duration"),
        # Which encode branch is live, so triage knows where to look.
        "encoder": "nvenc" if USE_NVENC else ("vaapi" if USE_VAAPI else "libx264"),
        "stream_quality": _stream_quality_key(),
        "hls_seg_time": HLS_SEG_TIME,
        "subtitles": SUBTITLE_BURN_IN,
        # ABR ladder: false in single-rendition mode, else the rung heights the
        # master playlist exposes (highest first). Lets triage/the UI see that
        # the player is choosing the rendition, not the admin's stream_quality.
        "abr_ladder": [int(r["height"]) for r in _abr_ladder()] if ABR_LADDER else False,
    }


settings_lock = threading.Lock()
settings: dict = {
    "viewer_public": False,
    "auto_fill": True,
    "stream_quality": "default",
}


def _load_settings():
    global settings
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text())
            if isinstance(loaded, dict):
                settings.update(loaded)
        except Exception as e:
            _warn_state_load_failed("settings", SETTINGS_FILE, e)


def _save_settings():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def _stream_quality_key() -> str:
    with settings_lock:
        key = settings.get("stream_quality", "default")
    return key if key in STREAM_QUALITY_PRESETS else "default"


def _stream_quality_preset() -> dict:
    return STREAM_QUALITY_PRESETS[_stream_quality_key()]


def _save_state(snapshot: dict | None):
    """Persist a playback snapshot (or `None` to mark idle). Never raises."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(snapshot))
    except Exception as e:
        print(f"state: save failed: {e}", file=sys.stderr)


def _clear_state():
    """Mark playback as idle (next startup goes idle, not auto-resume)."""
    _save_state(None)


def _load_state() -> dict | None:
    """Load persisted playback snapshot. Returns None if absent/invalid/idle."""
    if not STATE_FILE.exists():
        return None
    try:
        loaded = json.loads(STATE_FILE.read_text())
    except Exception as e:
        print(f"state: load failed: {e}", file=sys.stderr)
        return None
    if not isinstance(loaded, dict):
        return None
    src = loaded.get("source")
    if not isinstance(src, dict) or not src.get("type") or not src.get("ref"):
        return None
    return loaded


_load_tokens()
_load_users()
_load_user_sessions()
_load_progress()
_load_playlist()
_load_recent()
_load_requests()
_load_reports()
_load_custom_reactions()
_load_settings()


# (The old inline NO_TOKEN_PAGE lived here. It was a dead end — no way to
# type a code, no way in for an account holder — and is now static/home.html,
# served by the gate for unauthenticated `/` and `/controls`.)


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


# OrderedDict + an LRU cap so the cache can't grow without bound on a
# long-running instance pulled by random scanner IPs. 1024 entries is a few
# hundred KB at most; eviction is move-to-end on hit, popitem(last=False) on
# overflow. ~all real-world viewer pools fit well inside the cap.
_GEO_CACHE_MAX = 1024
_geo_cache: "_collections.OrderedDict[str, str]" = _collections.OrderedDict()
_geo_cache_lock = threading.Lock()
_viewer_log_lock = threading.Lock()


def _append_viewer_log(entry: dict) -> None:
    """Append one JSONL record to /data/viewer_log.jsonl. Survives restarts so
    historical "who viewed" can be reconstructed without scraping container logs."""
    try:
        VIEWER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _viewer_log_lock:
            with VIEWER_LOG_FILE.open("a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"viewer_log: append failed: {e}", file=sys.stderr)


def _is_private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


def _geo_lookup(ip: str) -> str:
    """Resolve ip → 'City, CC' via ipinfo.io. Returns 'LAN' for private IPs and
    cached strings on subsequent calls. Best-effort; returns '?' on any failure.
    Cache is LRU-bounded — see _GEO_CACHE_MAX."""
    with _geo_cache_lock:
        cached = _geo_cache.get(ip)
        if cached is not None:
            _geo_cache.move_to_end(ip)
            return cached
    if _is_private_ip(ip):
        loc = "LAN"
    else:
        try:
            req = urllib.request.Request(
                f"https://ipinfo.io/{ip}/json",
                headers={"User-Agent": "homelab-jetstream/1"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read())
            parts = [data.get("city"), data.get("region"), data.get("country")]
            loc = ", ".join(p for p in parts if p) or "?"
        except Exception:
            loc = "?"
    with _geo_cache_lock:
        _geo_cache[ip] = loc
        while len(_geo_cache) > _GEO_CACHE_MAX:
            _geo_cache.popitem(last=False)
    return loc


def _track_viewer():
    ip = _client_ip()
    # Resolve label every request — cheap (a token-list scan) and lets admin
    # see token rotation propagate without waiting for the IP to age out.
    token = request.cookies.get(TOKEN_COOKIE) or request.args.get("t")
    label = None
    if token:
        label = _mark_token_seen(token)
    is_new = False
    with viewers_lock:
        if ip not in viewers:
            is_new = True
            viewer_session_start[ip] = time.time()
        viewers[ip] = time.time()
        viewer_labels[ip] = label
    if is_new:
        ua = (request.headers.get("User-Agent") or "").replace("\n", " ")[:160]
        # Geo lookup hits the network; do it off the request thread so /hls
        # latency for the first segment isn't dragged into ipinfo.io's response time.
        def _emit():
            loc = _geo_lookup(ip)
            print(
                f"[viewer] connect ip={ip} loc={loc!r} who={label or 'anonymous'} ua={ua!r}",
                file=sys.stderr, flush=True,
            )
            _append_viewer_log({
                "ts": time.time(), "ip": ip, "loc": loc,
                "who": label, "ua": ua,
            })
        threading.Thread(target=_emit, daemon=True, name="viewer-log").start()


def _viewer_count() -> int:
    cutoff = time.time() - VIEWER_TIMEOUT
    with viewers_lock:
        stale = [ip for ip, ts in viewers.items() if ts < cutoff]
        for ip in stale:
            del viewers[ip]
            viewer_labels.pop(ip, None)
            viewer_session_start.pop(ip, None)
        count = len(viewers)
    # Print AFTER releasing the lock — stderr writes can block briefly and
    # we don't want to hold the (hot-path) lock over them.
    for ip in stale:
        print(f"[viewer] disconnect ip={ip} (idle > {VIEWER_TIMEOUT}s)",
              file=sys.stderr, flush=True)
    return count


def _viewer_session_expired(ip: str) -> bool:
    """True if this ip's continuous session has run past VIEWER_MAX_SESSION_SECS.
    Cheap dict read — safe to call on the hot /hls auth path."""
    if not VIEWER_MAX_SESSION_SECS:
        return False
    with viewers_lock:
        start = viewer_session_start.get(ip)
    return start is not None and (time.time() - start) >= VIEWER_MAX_SESSION_SECS


def _safe_resolve(rel: str, must_be_dir: bool = False, must_be_file: bool = False) -> Path:
    rel = (rel or "").lstrip("/")
    target = (MEDIA_ROOT / rel).resolve()
    if target != MEDIA_ROOT and MEDIA_ROOT not in target.parents:
        abort(400)
    if must_be_dir and not target.is_dir():
        abort(404)
    if must_be_file and not target.is_file():
        abort(404)
    return target


def _list_dir(rel: str):
    base = _safe_resolve(rel, must_be_dir=True)
    items = []
    for entry in base.iterdir():
        if entry.name.startswith("."):
            continue
        rel_path = str(entry.relative_to(MEDIA_ROOT))
        if entry.is_dir():
            items.append({"name": entry.name, "path": rel_path, "type": "directory"})
        elif entry.suffix.lower() in VIDEO_EXTS:
            items.append({
                "name": entry.name,
                "path": rel_path,
                "type": "file",
                "size": entry.stat().st_size,
            })
    items.sort(key=lambda i: (i["type"] != "directory", _natural_sort_key(i["name"])))
    return items


def _natural_sort_key(value: str):
    """Case-insensitive natural sort: Season 2 before Season 10, S01E02
    before S01E10. Used by browse UIs where plain lexicographic order makes
    TV folders hard to scan."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value or "")]


def _viewer_library_roots() -> list[dict]:
    """Viewer-facing library roots from VIEWER_LIBRARY_ROOTS.

    Format: "Label=relative/path,Other Label=other/path". Missing labels fall
    back to the basename. Only existing directories under MEDIA_ROOT are
    exposed. Admin/control browse is intentionally unaffected."""
    roots = []
    seen = set()
    for raw in VIEWER_LIBRARY_ROOTS_RAW.split(","):
        part = raw.strip()
        if not part:
            continue
        if "=" in part:
            label, rel = part.split("=", 1)
            label = label.strip()
            rel = rel.strip().strip("/")
        else:
            rel = part.strip().strip("/")
            label = Path(rel).name or rel
        if not rel or rel in seen:
            continue
        try:
            target = _safe_resolve(rel, must_be_dir=True)
        except Exception:
            continue
        seen.add(rel)
        roots.append({
            "name": label or target.name,
            "path": str(target.relative_to(MEDIA_ROOT)),
            "type": "directory",
            "viewer_root": True,
        })
    return roots


def _viewer_path_allowed(rel: str) -> bool:
    rel = (rel or "").strip("/")
    roots = [r["path"].strip("/") for r in _viewer_library_roots()]
    if not roots:
        return False
    if not rel:
        return True
    return any(rel == root or rel.startswith(root + "/") for root in roots)


def _viewer_library_files() -> list[Path]:
    roots = _viewer_library_roots()
    if not roots:
        return []
    allowed = tuple(r["path"].strip("/") for r in roots)
    files = []
    for p in _scan_library():
        try:
            rel = str(p.relative_to(MEDIA_ROOT))
        except ValueError:
            continue
        if any(rel == root or rel.startswith(root + "/") for root in allowed):
            files.append(p)
    return files


def _viewer_tv_root_paths() -> set[str]:
    roots = set()
    for root in _viewer_library_roots():
        rel = root["path"].strip("/")
        label = (root.get("name") or "").lower()
        basename = Path(rel).name.lower()
        if basename in {"tv", "shows", "series"} or "tv" in label or "show" in label:
            roots.add(rel)
    return roots


def _viewer_is_tv_show_path(rel: str) -> bool:
    rel = (rel or "").strip("/")
    for root in _viewer_tv_root_paths():
        prefix = root + "/"
        if rel.startswith(prefix):
            rest = rel[len(prefix):]
            return bool(rest) and "/" not in rest
    return False


def _season_number_from_name(name: str) -> int | None:
    text = name or ""
    match = re.search(r"\b[Ss](\d{1,3})[Ee]\d{1,3}\b", text)
    if not match:
        match = re.search(r"\bSeason[ ._-]*(\d{1,3})\b", text, flags=re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _season_label(season: int) -> str:
    return "Specials" if season == 0 else f"Season {season}"


def _viewer_virtual_season_path(show_rel: str, season: int) -> str:
    return f"{show_rel.strip('/')}/{VIEWER_SEASON_MARKER}/{season}"


def _viewer_parse_virtual_season(rel: str) -> tuple[str, int] | None:
    rel = (rel or "").strip("/")
    marker = f"/{VIEWER_SEASON_MARKER}/"
    if marker not in rel:
        return None
    show_rel, season_raw = rel.rsplit(marker, 1)
    if not show_rel or "/" in season_raw or not _viewer_is_tv_show_path(show_rel):
        return None
    try:
        season = int(season_raw)
    except ValueError:
        return None
    return show_rel, season


def _viewer_group_tv_show_items(show_rel: str, items: list[dict]) -> list[dict]:
    seasons = set()
    loose_files = []
    directories = []
    for item in items:
        if item["type"] == "directory":
            directories.append(item)
            continue
        season = _season_number_from_name(item["name"])
        if season is None:
            loose_files.append(item)
        else:
            seasons.add(season)
    if not seasons:
        return items
    grouped = [
        {
            "name": _season_label(season),
            "path": _viewer_virtual_season_path(show_rel, season),
            "type": "directory",
            "virtual_season": season,
        }
        for season in sorted(seasons)
    ]
    return grouped + directories + loose_files


def _search_library(query: str, limit: int = 300) -> tuple[list[dict], bool]:
    """Filter the (cached) library scan by a query. Space-separated terms are
    AND-matched, case-insensitively, against each file's path relative to
    MEDIA_ROOT — so "office s02" finds files with both. Returns file entries in
    the same shape as _list_dir (name/path/type/size) plus a truncated flag."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return [], False
    matched = [
        p for p in _scan_library()
        if all(t in str(p.relative_to(MEDIA_ROOT)).lower() for t in terms)
    ]
    matched.sort(key=lambda p: p.name.lower())
    truncated = len(matched) > limit
    items = []
    for p in matched[:limit]:
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        items.append({
            "name": p.name,
            "path": str(p.relative_to(MEDIA_ROOT)),
            "type": "file",
            "size": size,
        })
    return items, truncated


def _search_viewer_library(query: str, limit: int = 300) -> tuple[list[dict], bool]:
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return [], False
    matched = [
        p for p in _viewer_library_files()
        if all(t in str(p.relative_to(MEDIA_ROOT)).lower() for t in terms)
    ]
    matched.sort(key=lambda p: p.name.lower())
    truncated = len(matched) > limit
    items = []
    for p in matched[:limit]:
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        items.append({
            "name": p.name,
            "path": str(p.relative_to(MEDIA_ROOT)),
            "type": "file",
            "size": size,
        })
    return items, truncated


def _viewer_list_dir(rel: str):
    rel = (rel or "").strip("/")
    if not rel:
        return _viewer_library_roots()
    virtual_season = _viewer_parse_virtual_season(rel)
    if virtual_season:
        show_rel, season = virtual_season
        if not _viewer_path_allowed(show_rel):
            abort(404)
        return [
            item for item in _list_dir(show_rel)
            if item["type"] == "file" and _season_number_from_name(item["name"]) == season
        ]
    if not _viewer_path_allowed(rel):
        abort(404)
    items = _list_dir(rel)
    if _viewer_is_tv_show_path(rel):
        return _viewer_group_tv_show_items(rel, items)
    return items


def _viewer_file_allowed(rel: str) -> bool:
    rel = (rel or "").strip("/")
    return bool(rel) and VIEWER_SEASON_MARKER not in rel.split("/") and _viewer_path_allowed(rel)


def _cleanup_hls():
    """Wipe ALL HLS state — master playlist, every run dir, every segment.
    Used on full stop / idle, and on startup before any ffmpeg spawns.

    Caller MUST NOT hold state_lock — this acquires it. (Also acquires
    _composer_state_lock; lock order matches the composer's tick path so
    they can't deadlock.)"""
    for entry in HLS_DIR.iterdir():
        # The VOD tree is not ours: private per-user sessions live under
        # /hls/vod/ with their own reaper. A live-stream stop/idle must not
        # nuke someone's in-flight film.
        if entry == VOD_DIR_BASE:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except FileNotFoundError:
            pass
    RUN_DIR_BASE.mkdir(parents=True, exist_ok=True)
    VOD_DIR_BASE.mkdir(parents=True, exist_ok=True)
    # Reset run state and composer counters — without /hls/ on disk there's
    # nothing for those numbers to refer to. Lock order: state_lock first,
    # _composer_state_lock second (same as composer cleanup path).
    with state_lock:
        global active_run_id
        active_run_id = None
        finished_run_ids.clear()
    with _composer_state_lock:
        _composer_state["seg_to_seq"].clear()
        _composer_state["run_to_disc"].clear()
        _composer_state["next_seq"] = 0
        _composer_state["next_disc"] = 1
        _composer_state["first_run_seen"] = False


def _run_dir(run_id: int) -> Path:
    return RUN_DIR_BASE / str(run_id)


def _spawn_ffmpeg(cmd: list[str], log_path: Path) -> subprocess.Popen:
    """Launch ffmpeg with stdout+stderr redirected to a per-run log file.
    start_new_session detaches from the gunicorn worker's process group so
    signals aimed at the worker don't propagate to ffmpeg (and vice versa).
    The log file is accessible at /hls/run/<id>/ffmpeg.log for postmortems."""
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL,
        stdout=log, stderr=log,
        close_fds=True,
        start_new_session=True,
    )
    log.close()  # Popen dup'd the fd; ours can go.
    return proc


def _terminate_proc_locked():
    """Terminate the running ffmpeg (if any), leaving source/position state alone.
    Also cancels the pre-rolled ffmpeg — any explicit kill of the current source
    means whatever's "next" is no longer determined (user might be skipping,
    pausing, or swapping the source), so the pre-roll is no longer valid.
    Caller must hold state_lock."""
    global current_proc
    _cancel_preroll_locked()
    if current_proc and current_proc.poll() is None:
        try:
            current_proc.send_signal(signal.SIGTERM)
            current_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            current_proc.kill()
            current_proc.wait(timeout=2)
    current_proc = None


def _cancel_preroll_locked() -> None:
    """Tear down the pre-rolled ffmpeg (if any) and restore its source to the
    head of the playlist when it was popped from there. Run dir gets retired
    so the composer's next tick cleans it up. Caller must hold state_lock.

    Touches playlist_lock — state_lock → playlist_lock is the established
    order in this module (e.g. the watcher loop), so this won't deadlock."""
    global preroll_proc, preroll_source, preroll_run_id, preroll_source_from_queue
    if preroll_proc is None:
        return
    if preroll_proc.poll() is None:
        try:
            preroll_proc.send_signal(signal.SIGTERM)
            preroll_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            preroll_proc.kill()
            preroll_proc.wait(timeout=2)
    if preroll_run_id is not None:
        finished_run_ids.add(preroll_run_id)
    if preroll_source_from_queue and preroll_source is not None:
        with playlist_lock:
            playlist.insert(0, preroll_source)
            _save_playlist()
    preroll_proc = None
    preroll_source = None
    preroll_run_id = None
    preroll_source_from_queue = False


def _start_preroll_locked() -> bool:
    """Spawn ffmpeg for the next queue item into a fresh run dir, without
    touching current_proc / current_source / active_run_id. Returns True if
    a preroll was started, False on any no-op condition (already pre-rolled,
    no current source, unknown duration, not yet near EOF, nothing eligible
    in the queue, or the next item isn't a regular file that's safe to
    pre-roll). Caller must hold state_lock.

    URL items are skipped: yt-dlp resolution can take 30+s, so racing it
    against the live-edge buffer drain is fragile. Live URLs are also skipped
    (no EOF to anticipate). File items cover the common case (a queued
    movie or episode following the current one)."""
    global preroll_proc, preroll_source, preroll_run_id, preroll_source_from_queue
    global next_run_id
    if preroll_proc is not None:
        return False
    if current_source is None or current_proc is None:
        return False
    if current_proc.poll() is not None:
        return False  # already exited — natural EOF path handles promote/advance
    if current_paused or current_source.get("is_live"):
        return False
    duration = current_source.get("duration")
    if duration is None or duration <= PREROLL_LEAD_SECS:
        return False
    pos = _current_position()
    if pos is None or pos < duration - PREROLL_LEAD_SECS:
        return False
    # Peek the next item without committing — only pop if we successfully
    # build and spawn the ffmpeg for it.
    with playlist_lock:
        if not playlist:
            return False
        candidate = playlist[0]
    if candidate.get("type") != "file" or candidate.get("is_live"):
        return False
    ref = candidate.get("ref")
    if not ref:
        return False
    try:
        full = _safe_resolve(ref, must_be_file=True)
    except Exception:
        return False
    # Subtitle index: auto-pick when the source dict has no usable value (key
    # missing OR explicit null). Matches _start_stream's policy — a stale null
    # left over from when USE_SUBTITLES was off shouldn't keep blocking subs
    # after burn-in is flipped back on.
    next_source = dict(candidate)
    if next_source.get("subtitle_idx") is None:
        next_source["subtitle_idx"] = _pick_default_subtitle(str(full))
    if next_source.get("duration") is None:
        next_source["duration"] = _probe_duration(full)
    audio_idx = _probe_english_audio(str(full))
    run_id = next_run_id
    run_dir = _run_dir(run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        builder = _build_ffmpeg_abr_cmd if ABR_LADDER else _build_ffmpeg_cmd
        cmd = builder(
            str(full), run_dir, 0.0, audio_idx, next_source.get("subtitle_idx"),
        )
        proc = _spawn_ffmpeg(cmd, run_dir / "ffmpeg.log")
    except Exception as e:
        print(f"preroll: failed to spawn for {next_source.get('title')!r}: {e}",
              file=sys.stderr)
        return False
    # Commit: pop from queue, advance run id, install preroll globals.
    with playlist_lock:
        if playlist and playlist[0] is candidate:
            playlist.pop(0)
            _save_playlist()
        else:
            # Queue head changed between peek and spawn — caller raced us.
            # Kill the just-spawned ffmpeg and drop the run dir.
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, Exception):
                pass
            finished_run_ids.add(run_id)
            return False
    next_run_id += 1
    preroll_proc = proc
    preroll_source = next_source
    preroll_run_id = run_id
    preroll_source_from_queue = True
    return True


def _promote_preroll_locked() -> bool:
    """Swap the pre-rolled ffmpeg into the current slot — the new source has
    been seamlessly streaming alongside; this just flips the bookkeeping so
    /api/status, the watcher, and termination paths point at it. Returns
    True if a promotion happened. Caller must hold state_lock."""
    global current_proc, current_source, current_start_offset, current_start_time
    global current_paused, paused_position, active_run_id
    global preroll_proc, preroll_source, preroll_run_id, preroll_source_from_queue
    if preroll_proc is None or preroll_proc.poll() is not None:
        return False
    old_source = current_source
    old_pos = _current_position()
    # Retire the old run so the composer draws a DISCONTINUITY before the
    # new run's segments and eventually cleans up the empty dir.
    if active_run_id is not None:
        finished_run_ids.add(active_run_id)
    current_proc = preroll_proc
    current_source = preroll_source
    active_run_id = preroll_run_id
    current_start_offset = 0.0
    current_start_time = time.time()
    current_paused = False
    paused_position = 0.0
    preroll_proc = None
    preroll_source = None
    preroll_run_id = None
    preroll_source_from_queue = False
    # Reset vote-to-skip for the new item.
    with skip_votes_lock:
        skip_votes.clear()
    _recent_record(old_source, old_pos)
    return True


def _stop_locked():
    """Full stop: terminate ffmpeg AND clear all source/playback state. Also
    retires the active run (the composer's tick will then drop /hls/stream.m3u8
    once no playable segments remain anywhere)."""
    global current_source, current_start_offset, current_start_time
    global current_paused, paused_position, active_run_id
    old_source = current_source
    old_pos = _current_position()
    if old_pos is None and current_paused:
        old_pos = paused_position
    _terminate_proc_locked()
    if active_run_id is not None:
        finished_run_ids.add(active_run_id)
        active_run_id = None
    current_source = None
    current_start_offset = 0.0
    current_start_time = 0.0
    current_paused = False
    paused_position = 0.0
    _recent_record(old_source, old_pos)


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            timeout=10,
        ).decode().strip()
        return float(out) if out else None
    except Exception:
        return None


def _current_position(running: bool | None = None) -> float | None:
    """Compute current playback position. `running` lets a caller that just
    did `proc.poll()` pass the result in to save a syscall on the hot
    /api/status path; default of None re-checks here."""
    if running is None:
        running = current_proc is not None and current_proc.poll() is None
    if not running:
        return None
    if current_paused:
        return paused_position
    pos = current_start_offset + max(0.0, time.time() - current_start_time)
    duration = (current_source or {}).get("duration")
    if duration:
        pos = min(pos, duration)
    return pos


def _state_snapshot_locked() -> dict | None:
    """Return the recoverable playback state. Caller must hold state_lock."""
    if current_source is None:
        return None
    if current_paused:
        pos = paused_position
    else:
        pos = _current_position()
        if pos is None:
            pos = current_start_offset
    return {
        "source": current_source,
        "position_seconds": max(0.0, float(pos or 0.0)),
        "paused": current_paused,
        "saved_at": time.time(),
    }


def _resolve_url(url: str) -> dict:
    """Full-resolve a single video URL to direct stream URL(s) via yt-dlp.

    YouTube only ships *muxed* formats (single file, video+audio together) up
    to 360p — anything HD is DASH (separate video + audio streams). The
    selector prefers the DASH path so we get real 1080p, falls back to muxed
    when the site only offers that, and finally to "whatever yt-dlp can find."
    h264 + m4a are constrained on the DASH branch so the encode side doesn't
    have to handle vp9/opus on top of everything else.
    """
    try:
        out = subprocess.check_output(
            [
                "yt-dlp", "-J", "--no-playlist", "--no-warnings",
                "-f",
                "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
                "/bestvideo[height<=1080]+bestaudio"
                "/best[height<=1080][ext=mp4]"
                "/best[height<=1080]"
                "/best",
                url,
            ],
            timeout=45,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or ["yt-dlp failed"]
        raise ValueError(f"yt-dlp: {msg[0][:300]}")
    except subprocess.TimeoutExpired:
        raise ValueError("yt-dlp timed out resolving URL")
    info = json.loads(out)
    stream_url: str | None = info.get("url")
    audio_url: str | None = None
    if not stream_url:
        # DASH path — yt-dlp returns the chosen formats in `requested_formats`,
        # one per stream. Pick the first video stream and (if present) the
        # first audio stream so the caller can hand both to ffmpeg as
        # separate -i inputs.
        formats = info.get("requested_formats") or []
        for f in formats:
            vcodec = (f.get("vcodec") or "").lower()
            acodec = (f.get("acodec") or "").lower()
            if stream_url is None and vcodec and vcodec != "none":
                stream_url = f.get("url")
            elif audio_url is None and acodec and acodec != "none":
                audio_url = f.get("url")
    if not stream_url:
        raise ValueError("could not extract a direct stream URL")
    return {
        "stream_url": stream_url,
        "audio_url": audio_url,
        "title": info.get("title") or url,
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
    }


def _expand_url(url: str) -> list[dict]:
    """Cheap metadata pass: returns one source dict per video. Playlists expand to N items;
    single videos return [one]. The returned source dicts don't include a direct stream URL
    — that's fetched by _resolve_url on-demand when the item starts playing."""
    try:
        out = subprocess.check_output(
            ["yt-dlp", "-J", "--flat-playlist", "--no-warnings", url],
            timeout=45, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or ["yt-dlp failed"]
        raise ValueError(f"yt-dlp: {msg[0][:300]}")
    except subprocess.TimeoutExpired:
        raise ValueError("yt-dlp timed out")
    info = json.loads(out)
    entries = info.get("entries")
    if entries:
        items = []
        for e in entries:
            video_url = e.get("webpage_url") or e.get("url")
            if not (video_url and str(video_url).startswith("http")):
                # yt-dlp sometimes returns just the bare ID for YouTube.
                if e.get("ie_key") == "Youtube" and e.get("id"):
                    video_url = f"https://www.youtube.com/watch?v={e['id']}"
                else:
                    continue
            items.append({
                "type": "url", "ref": video_url,
                "title": e.get("title") or video_url,
                "duration": e.get("duration"),
                "is_live": False,
                # URLs don't get burn-in subs — the source is a remote stream,
                # libavfilter `subtitles=` reads the input file for sub data
                # and there's no sidecar track for yt-dlp output.
                "subtitle_idx": None,
            })
        if not items:
            raise ValueError("playlist has no playable entries")
        return items
    return [{
        "type": "url", "ref": url,
        "title": info.get("title") or url,
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
        "subtitle_idx": None,
    }]


# transfer characteristics that signal HDR. smpte2084 = HDR10/PQ, arib-std-b67 = HLG.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _probe_video_info(input_path: str) -> dict | None:
    """One-shot ffprobe for the first video stream: codec, width, height, and an
    is_hdr flag derived from color_transfer (PQ / HLG). Returns None if probe
    fails. Cached per-source via the call sites — _start_stream calls this
    once per ffmpeg launch, no need for a memoization layer here."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=codec_name,width,height,color_transfer,color_primaries,r_frame_rate,avg_frame_rate"
                ":stream_side_data=side_data_type",
                "-of", "json",
                input_path,
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).decode()
        data = json.loads(out)
    except Exception:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    s = streams[0]
    transfer = (s.get("color_transfer") or "").lower()
    primaries = (s.get("color_primaries") or "").lower()
    # HDR: PQ/HLG transfer is the canonical signal. bt2020 primaries alone
    # aren't enough — some BT.2020-tagged sources are still SDR.
    is_hdr = transfer in HDR_TRANSFERS

    # Parse ffprobe's "num/den" fps strings. Prefer r_frame_rate (declared)
    # over avg_frame_rate (computed), falling back to None when both are
    # unparseable. Used to set NVENC's GOP size — h264_nvenc ignores
    # -force_key_frames, so we need an explicit -g for 1s segments to land
    # on keyframe boundaries.
    def _parse_fps(rate: str | None) -> float | None:
        if not rate or "/" not in rate:
            return None
        try:
            num, den = rate.split("/", 1)
            num_f, den_f = float(num), float(den)
            return num_f / den_f if den_f > 0 else None
        except (ValueError, ZeroDivisionError):
            return None
    fps = _parse_fps(s.get("r_frame_rate")) or _parse_fps(s.get("avg_frame_rate"))

    # Dolby Vision detection. DV streams carry a "DOVI configuration record"
    # side-data entry. NVDEC on consumer cards can't decode the DV
    # enhancement layer — it emits a "Dolby Vision enhancement-layer HEVC
    # configuration" error and produces zero output, stalling the stream.
    # Flagged so the decode path forces CPU decode (the HEVC software decoder
    # reads the base layer fine; HDR tonemap then runs as usual).
    is_dovi = any(
        "dovi" in (sd.get("side_data_type") or "").lower()
        or "dolby vision" in (sd.get("side_data_type") or "").lower()
        for sd in (s.get("side_data_list") or [])
    )

    return {
        "codec": (s.get("codec_name") or "").lower() or None,
        "width": s.get("width"),
        "height": s.get("height"),
        "transfer": transfer or None,
        "primaries": primaries or None,
        "is_hdr": is_hdr,
        "is_dovi": is_dovi,
        "fps": fps,
    }


ENGLISH_LANG_TAGS = {"eng", "en", "en-us", "en-gb"}


def _is_english(lang: str | None) -> bool:
    return (lang or "").lower() in ENGLISH_LANG_TAGS


def _probe_english_audio(input_path: str) -> int | None:
    """Pick the first English-tagged audio stream's position (0,1,2,…) suitable
    for `-map 0:a:N`. Handles MULTi rips that put a foreign dub first. Returns
    None to let ffmpeg's default pick the first audio stream."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type:stream_tags=language",
                "-of", "json",
                input_path,
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).decode()
        data = json.loads(out)
    except Exception:
        return None
    audio_pos = -1
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        audio_pos += 1
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        if _is_english(lang):
            return audio_pos
    return None


# Text-based subtitle codecs the libavfilter `subtitles=` filter can render.
# Bitmap formats (hdmv_pgs_subtitle, dvd_subtitle, dvb_subtitle) are skipped
# because the filter rasterizes via libass and only handles text streams —
# burning in PGS would need an OCR pass we don't have.
TEXT_SUBTITLE_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def _probe_subtitle_tracks(input_path: str) -> list[dict]:
    """Return text-based subtitle tracks as `[{index, codec, language}, …]`
    where `index` is the position among ALL subtitle streams (0,1,2,…) in
    container order — this is what the `subtitles=…:si=N` filter expects.
    Bitmap formats (PGS, DVD, DVB) are filtered out: `subtitles=` can't render
    them. Returns [] on probe failure or no usable tracks."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language",
                "-of", "json",
                input_path,
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).decode()
        data = json.loads(out)
    except Exception:
        return []
    tracks: list[dict] = []
    sub_pos = -1
    for s in data.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        sub_pos += 1
        codec = (s.get("codec_name") or "").lower()
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        lang = ((s.get("tags") or {}).get("language") or "").lower() or None
        tracks.append({"index": sub_pos, "codec": codec, "language": lang})
    return tracks


# Subtitle burn-in is gated by USE_SUBTITLES. The libass `subtitles=` filter
# loads the *entire* subtitle track before it renders the first frame — when
# pointed at a multi-GB container that means demuxing the whole file to EOF
# before segment 0 (~0.007x realtime on an 18 GB remux). The workaround is the
# cache-extract path below: on first play of a source the chosen sub track is
# pulled to a tiny `.srt` in SUBS_CACHE_DIR in the background, and that play
# runs without subs. Every subsequent play points `subtitles=` at the cached
# `.srt` (a few KB, parses instantly) so burn-in lands without stalling.
SUBTITLE_BURN_IN = os.environ.get("USE_SUBTITLES", "0") == "1"
SUBS_CACHE_DIR = Path(os.environ.get("SUBS_CACHE_DIR", "/data/subs"))
# Hard cap on a single extract — if it's not done in 15 min something else is
# wrong (failing decode, dead disk). Lets the thread die instead of leaking.
SUBS_EXTRACT_TIMEOUT_SECS = 15 * 60
# Per-cache-file in-flight locks so a second play of the same source during
# extraction doesn't kick off a duplicate ffmpeg. Master lock protects the
# dict itself; each value is a per-key threading.Lock used as a try-acquire
# flag, NOT held across the extract — the extract runs in a separate thread
# that owns the lock until it finishes.
_sub_extract_locks_master = threading.Lock()
_sub_extract_locks: dict[str, threading.Lock] = {}


def _sub_cache_key(input_path: str, sub_idx: int) -> str:
    """Stable cache filename for a given source + sub-track combo. Keys on the
    resolved absolute path + the source's `st_mtime_ns` + the sub index — so a
    Sonarr upgrade (new file at the same path) or a different track pick
    yields a fresh cache entry, and the old one ages out via the next prune."""
    try:
        mtime_ns = Path(input_path).stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    key = f"{Path(input_path).resolve()}\0{mtime_ns}\0{sub_idx}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def _cached_sub_path(input_path: str, sub_idx: int) -> Path:
    return SUBS_CACHE_DIR / f"{_sub_cache_key(input_path, sub_idx)}.srt"


def _start_sub_extract(input_path: str, sub_idx: int) -> None:
    """Spawn a background ffmpeg extract of one sub track to the cache. No-op
    if the cached `.srt` already exists or another extract for the same key
    is already running. Errors log to stderr — failure means "no subs this
    play and the next" rather than a stream failure, so silent here is fine."""
    out = _cached_sub_path(input_path, sub_idx)
    if out.exists():
        return
    try:
        SUBS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"sub cache dir create failed: {e}", file=sys.stderr)
        return
    with _sub_extract_locks_master:
        lock = _sub_extract_locks.setdefault(str(out), threading.Lock())
    if not lock.acquire(blocking=False):
        return  # extract already in flight for this key

    def run():
        tmp = out.with_suffix(".srt.tmp")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", input_path,
                    # 0:s:N matches the picker's index basis (position among
                    # all subtitle streams, container order). srt is the
                    # safest target — libass renders it directly, and any
                    # text-based source codec (subrip/ass/ssa/mov_text/webvtt)
                    # converts in cleanly.
                    "-map", f"0:s:{sub_idx}",
                    "-c:s", "srt",
                    # Force the muxer explicitly: the atomic-write temp name ends
                    # in `.srt.tmp`, and ffmpeg picks the output format from the
                    # extension — `.tmp` is unknown, so without -f it bails with
                    # "Unable to choose an output format" (exit 234) and burn-in
                    # silently never happens.
                    "-f", "srt",
                    str(tmp),
                ],
                check=True,
                timeout=SUBS_EXTRACT_TIMEOUT_SECS,
                stderr=subprocess.DEVNULL,
            )
            tmp.replace(out)
        except Exception as e:
            print(f"sub extract failed for {input_path} #{sub_idx}: {e}",
                  file=sys.stderr)
            try:
                tmp.unlink()
            except OSError:
                pass
        finally:
            lock.release()

    threading.Thread(target=run, daemon=True, name="sub-extract").start()


# Cover art via arr APIs ---------------------------------------------------
# Map of `folder basename` -> (arr base URL, image url, api key). The folder
# basename comes from each Sonarr series / Radarr movie's `path`; we match
# requested viewer paths by walking up their components until a basename
# hits the map. arr's `images` array exposes a relative `/MediaCover/<id>/
# poster.jpg` URL that's served by arr itself (needs the X-Api-Key header),
# so we don't depend on TMDB or any external CDN.
_arr_cover_map_lock = threading.Lock()
_arr_cover_map: dict[str, tuple[str, str, str]] = {}


def _read_arr_api_key(path: Path) -> str | None:
    """Extract <ApiKey>…</ApiKey> from a Sonarr/Radarr config.xml mounted into
    the container. Returns None if the file is missing or unparseable; the
    arr inventory fetch then silently skips that service."""
    try:
        text = path.read_text()
    except OSError:
        return None
    m = re.search(r"<ApiKey>\s*([a-f0-9]+)\s*</ApiKey>", text)
    return m.group(1) if m else None


def _arr_fetch_inventory() -> None:
    """Pull series + movie lists from Sonarr and Radarr, update the cover
    map. Errors log to stderr; we never raise — a missing arr just means
    that branch's covers are blank, not that the page breaks."""
    new_map: dict[str, tuple[str, str, str]] = {}
    for base, cfg, endpoint in [
        (SONARR_URL, SONARR_CONFIG, "/api/v3/series"),
        (RADARR_URL, RADARR_CONFIG, "/api/v3/movie"),
    ]:
        key = _read_arr_api_key(cfg)
        if not key:
            continue
        try:
            req = urllib.request.Request(
                f"{base}{endpoint}", headers={"X-Api-Key": key}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.loads(r.read())
        except Exception as e:
            print(f"arr inventory fetch failed for {base}: {e}", file=sys.stderr)
            continue
        for item in items:
            folder = Path(item.get("path", "") or "").name
            if not folder:
                continue
            poster = next(
                (i for i in (item.get("images") or [])
                 if i.get("coverType") == "poster"),
                None,
            )
            if not poster:
                continue
            # Prefer the upstream remoteUrl (TVDB / TMDB CDN) over arr's own
            # /MediaCover endpoint — the local mirror requires basic auth
            # when `AuthenticationRequired` is on, and we don't have the
            # user's password (config.xml stores only the API key). Public
            # CDN avoids the auth handshake entirely. Falls back to arr's
            # path if remoteUrl is somehow missing, which we'll then need to
            # absolutize against `base`.
            remote = poster.get("remoteUrl")
            local = poster.get("url")
            if remote:
                url = remote
            elif local:
                url = local if local.startswith("http") else f"{base}{local}"
            else:
                continue
            new_map[folder] = (base, url, key)
    with _arr_cover_map_lock:
        _arr_cover_map.clear()
        _arr_cover_map.update(new_map)


def _arr_refresh_loop() -> None:
    """Daemon thread: refresh inventory on boot, then every ARR_REFRESH_SECS.
    First call waits briefly so the rest of the app finishes booting (arr is
    a slow dependency on cold start)."""
    time.sleep(5)
    while True:
        _arr_fetch_inventory()
        time.sleep(ARR_REFRESH_SECS)


def _arr_cover_for_path(path: str) -> tuple[str, str, str] | None:
    """Walk a viewer-requested path's components from leaf to root, looking
    for a basename match in the arr cover map. Returns (arr_base, url, key)
    or None."""
    if not path:
        return None
    parts = Path(path).parts
    with _arr_cover_map_lock:
        for i in range(len(parts) - 1, -1, -1):
            hit = _arr_cover_map.get(parts[i])
            if hit:
                return hit
    return None


def _pick_default_subtitle(input_path: str) -> int | None:
    """Auto-pick a sub track index for burn-in: prefer English-tagged, fall
    back to the first text-based track. Returns None when burn-in is disabled
    or nothing usable is present. The actual cache-extraction happens later in
    `_build_ffmpeg_cmd`, not here — this picker is also called from queue/add
    paths to record the index, where kicking off an extract early would be
    wasted work on items that may never play."""
    if not SUBTITLE_BURN_IN:
        return None
    tracks = _probe_subtitle_tracks(input_path)
    if not tracks:
        return None
    for t in tracks:
        if _is_english(t.get("language")):
            return t["index"]
    return tracks[0]["index"]


def _escape_subtitles_path(path: str) -> str:
    """Escape a filesystem path for use as the `subtitles=filename=…` value
    in an ffmpeg `-vf` argument. libavfilter parses this string twice — once
    as a filtergraph (commas/semicolons/brackets are structural) and once as
    a key=value list (colons split kv pairs). Backslash-escape every
    metacharacter; ffmpeg's docs spell out this exact set."""
    out = []
    for ch in path:
        if ch in ("\\", ":", "'", "[", "]", ",", ";"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


# Subtitle styling for burn-in. Fontsize=24 is readable on 1080p without
# overpowering the frame; OutlineColour with alpha + BorderStyle=3 puts a
# semi-transparent box behind each line so subs stay legible on bright /
# busy backgrounds. ASS-style force_style — applied to every track regardless
# of source format.
SUBTITLE_FORCE_STYLE = "Fontsize=24,OutlineColour=&H40000000,BorderStyle=3"


def _subtitles_filter(input_path: str, sub_idx: int) -> str:
    """Render the `subtitles=` filter expression with a properly escaped
    filename and the chosen subtitle stream index."""
    return (
        f"subtitles=filename={_escape_subtitles_path(input_path)}"
        f":si={sub_idx}:force_style='{SUBTITLE_FORCE_STYLE}'"
    )


def _build_ffmpeg_cmd(
    input_path: Path | str,
    run_dir: Path,
    start_seconds: float = 0.0,
    audio_idx: int | None = None,
    subtitle_idx: int | None = None,
    audio_input: str | None = None,
    mode: str = "live",
) -> list[str]:
    """Build the ffmpeg HLS command. Output goes into `run_dir/`:
    `init.mp4` (fmp4 init segment), `seg_NNNNN.m4s` (media segments), and
    `idx.m3u8` (the per-run media playlist that the composer reads).

    `audio_idx` (the position of an English-tagged audio stream from
    `_probe_english_audio`, or None) drives English-audio preference for
    MULTi rips.

    `subtitle_idx` (a sub-stream position from `_probe_subtitle_tracks`, or
    None) selects a text-based track to burn in via the libavfilter
    `subtitles=` filter. Burn-in only — there's no client-side toggle.

    `audio_input` (a separate URL, only set on the YouTube DASH path) feeds
    audio from a 2nd `-i` input. Without this, YouTube's "best muxed" tops
    out at 360p — we ask yt-dlp for separate video+audio formats so we can
    get real 1080p, then merge here at encode time.

    `mode` ("live" | "vod"): "live" is the original behavior, unchanged.
    "vod" (v2 per-user sessions) drops -re for -readrate pacing, drops the
    zerolatency tune on libx264, honors VOD_FORCE_CPU, and writes a full
    never-deleting playlist with a final ENDLIST (disk-backed session dir).
    `run_dir` is the VOD session dir."""
    input_str = str(input_path)
    # v2: local encoder flags so VOD_FORCE_CPU can demote a VOD encode to
    # libx264 without touching the live stream's globals. In live mode these
    # equal the globals exactly.
    force_cpu = mode == "vod" and VOD_FORCE_CPU
    use_nvenc = USE_NVENC and not force_cpu
    use_vaapi = USE_VAAPI and not force_cpu
    # Single ffprobe pass — codec for HW-decode eligibility, height for output
    # cap, transfer/primaries for HDR detection.
    info = _probe_video_info(input_str) or {}
    src_codec = info.get("codec")
    src_height = info.get("height")
    is_hdr = bool(info.get("is_hdr"))
    quality = _stream_quality_preset()
    target_height = int(quality["height"])
    video_bitrate = str(quality["video_bitrate"])
    video_bufsize = str(quality["bufsize"])
    # Output height: clamp to TARGET_HEIGHT, but don't upscale a smaller source
    # (a 720p WEB-DL has nothing to gain from being upscaled to 1080p, just CPU).
    out_h = min(src_height, target_height) if src_height else target_height
    # Decide whether the GPU can decode this source; falls back to CPU decode
    # for codecs the iHD VLD engine doesn't support (e.g. AV1) or if ffprobe fails.
    # HDR sources go through CPU decode unconditionally — the HW path can't
    # tonemap on this iGPU (no VPP), and the zscale tonemap chain only works
    # on CPU-side frames.
    hw_decode = (
        use_vaapi and USE_VAAPI_DECODE
        and src_codec in HWACCEL_DECODE_CODECS
        and not is_hdr
    )
    # NVDEC decode is eligible whenever the source codec is supported. HDR
    # frames stay on the GPU after decode and get tonemapped via a quick
    # hwdownload→zscale→hwupload_cuda round-trip in the filter chain below.
    # Dolby Vision is excluded: NVDEC chokes on the DV enhancement layer and
    # produces no output, so DV sources fall back to CPU decode (still NVENC
    # encoded). The is_hdr branch below handles the nv_decode=False case.
    nv_decode = (
        use_nvenc and src_codec in NVDEC_DECODE_CODECS
        and not info.get("is_dovi")
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if use_nvenc:
        # Frames are kept in `cuda` hw frames format when NVDEC is used so the
        # whole pipeline (decode → optional scale_cuda → nvenc) stays on-GPU.
        if nv_decode:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif use_vaapi:
        # Bind a named device "va" to our renderD128 and pin the filter chain
        # to it explicitly. Without -filter_hw_device, scale_vaapi/hwupload
        # picks the default vaapi device, which may be a second iGPU/driver
        # that doesn't advertise the profiles we need.
        cmd += [
            "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
            "-filter_hw_device", "va",
        ]
        if hw_decode:
            cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                    "-hwaccel_device", "va"]
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if mode == "vod":
        # VOD isn't paced for a shared live edge — read at VOD_READRATE×
        # realtime so the buffer runs ahead of the viewer without flat-out
        # transcoding the whole file ("0" = unpaced, full speed).
        if VOD_READRATE != "0":
            cmd += ["-readrate", VOD_READRATE]
        cmd += ["-i", input_str]
    else:
        cmd += ["-re", "-i", input_str]
    # YouTube DASH path: separate audio URL feeds a 2nd input. -ss is repeated
    # so both inputs seek to the same position, keeping audio in sync after
    # admin scrubbing. -re paces both at native rate.
    if audio_input:
        if start_seconds > 0:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        cmd += ["-re", "-i", audio_input]
    # Map: video from input #0, audio from input #1 if it's a separate URL,
    # otherwise from input #0. -sn drops every subtitle track; without it
    # ffmpeg's HLS muxer auto-maps them all — Superbad's 8 PGS subs alone
    # push CPU to ~1000%.
    if audio_input:
        audio_map = "1:a:0"
    else:
        audio_map = f"0:a:{audio_idx}" if audio_idx is not None else "0:a:0?"
    cmd += [
        "-map", "0:v:0",
        "-map", audio_map,
        "-sn",
    ]
    # Burn-in path: point the libass `subtitles=` filter at a pre-extracted
    # `.srt` in the cache so the filter no longer demuxes the source to EOF
    # before frame 0. If the cache file is missing, kick off a background
    # extract and play this run WITHOUT subs — the next play picks them up.
    # SUBTITLE_BURN_IN remains the global kill switch (set USE_SUBTITLES=0 to
    # bypass the whole path) so a stale subtitle_idx in an old state.json or
    # queue entry can't reintroduce the stall.
    sub_filter = None
    if SUBTITLE_BURN_IN and subtitle_idx is not None:
        cached = _cached_sub_path(input_str, subtitle_idx)
        if cached.exists():
            # Cached single-track .srt — si=0 is the only stream in the file.
            sub_filter = _subtitles_filter(str(cached), 0)
        else:
            _start_sub_extract(input_str, subtitle_idx)
            print(
                f"sub cache miss; extracting in background for next play "
                f"({Path(input_str).name} #{subtitle_idx})",
                file=sys.stderr,
            )
    if use_nvenc:
        # NVENC encode + (when codec is supported) NVDEC decode. Filter chain:
        #   - SDR fast path (NVDEC + no subs): scale_cuda only, frames never
        #     leave the GPU.
        #   - SDR + subs: hwdownload → burn → hwupload_cuda (libass is CPU-only).
        #   - HDR: tonemap chain is CPU-side (zscale). Frames come down via
        #     hwdownload, tonemap to BT.709 SDR, optional sub burn-in, then
        #     scale and re-upload to CUDA for nvenc.
        #   - CPU-decode + NVENC encode: CPU filter chain ends with
        #     hwupload_cuda so the encoder receives CUDA frames.
        if is_hdr:
            tonemap = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            # Scale to the output height BEFORE the tonemap (and subtitle
            # burn-in). The zscale tonemap is CPU-only and dominates cost:
            # at 4K it runs ~0.5x realtime and stalls the live stream; at
            # 1080p it's ~1.9x. For NVDEC input the downscale happens on the
            # GPU (scale_cuda) so only the smaller frames get pulled to system
            # memory; for CPU decode (e.g. Dolby Vision, which NVDEC can't
            # handle) the `scale` filter downsizes first. Subs burn in after
            # tonemap onto the SDR 1080p frames.
            if nv_decode:
                pre = (
                    f"scale_cuda=-2:{out_h}:format=p010le,"
                    f"hwdownload,format=p010le,{tonemap}"
                )
            else:
                pre = f"scale=-2:{out_h},{tonemap}"
            post = "format=nv12,hwupload_cuda"
            vf = (
                f"{pre},{sub_filter},{post}"
                if sub_filter else f"{pre},{post}"
            )
        elif nv_decode:
            if sub_filter:
                # hwdownload pix_fmt must match the GPU surface — 8-bit sources
                # arrive as nv12, 10-bit (HEVC Main10, AV1 10-bit) as p010le.
                # The trailing format=nv12 forces 8-bit before nvenc's main profile.
                vf = (
                    f"hwdownload,format=nv12|p010le,{sub_filter},"
                    f"scale=-2:{out_h},format=nv12,hwupload_cuda"
                )
            else:
                vf = f"scale_cuda=-2:{out_h}:format=nv12"
        else:
            post = f"scale=-2:{out_h},format=nv12,hwupload_cuda"
            vf = f"{sub_filter},{post}" if sub_filter else post
        # Compute GOP size for NVENC. h264_nvenc silently ignores
        # -force_key_frames, so without an explicit -g it picks its own
        # huge GOP (~250 frames default), the HLS muxer never sees a
        # keyframe at the segment boundary, and no segments get finalized
        # until ffmpeg exits. Multiply the source's fps by the desired
        # segment duration to land an IDR on each segment. Fallback to 30
        # when fps is unparseable.
        gop = max(1, int(round((info.get("fps") or 30) * float(HLS_SEG_TIME))))
        cmd += [
            "-vf", vf,
            "-c:v", "h264_nvenc",
            # p1..p7 quality/speed dial. p6 (NVIDIA's documented low-latency
            # preset) over the old p4: 1080p NVENC runs many× realtime here so
            # the encoder has ample headroom — spend it on quality-per-bitrate
            # at the same 5M. `ll` tune keeps b-frames off + per-segment IDR so
            # HLS segmentation and the Firefox fmp4 demuxer stay happy. cbr
            # keeps segment sizes predictable; bufsize stays generous (10M ≈ 2s
            # VBV) since the watch-party lag budget is ~2.5s, not sub-second.
            "-preset", "p6",
            "-tune", "ll",
            "-rc", "cbr",
            "-b:v", video_bitrate,
            "-maxrate", video_bitrate,
            "-bufsize", video_bufsize,
            "-profile:v", "main",
            "-g", str(gop),
            "-forced-idr", "1",
            "-no-scenecut", "1",
            # Insert Access Unit Delimiters between every encoded frame.
            # Firefox's fmp4 demuxer uses AUDs to find frame boundaries
            # inside segments; h264_nvenc sometimes omits them, forcing a
            # slower/less-reliable scan that surfaces as periodic micro-
            # skips even on healthy 1 s segments. The old version of this
            # BSF also set colour_primaries/transfer/matrix and corrupted
            # Chrome's avcC — `aud=insert` alone leaves the SPS/PPS
            # untouched and works in both browsers.
            "-bsf:v", "h264_metadata=aud=insert",
        ]
    elif use_vaapi:
        # When hw_decode is on, frames are already in vaapi format on the GPU,
        # so scale_vaapi runs entirely on the GPU. Otherwise hwupload moves
        # CPU-decoded frames onto the GPU before encode.
        # This iGPU lacks VAEntrypointVideoProc, so scale_vaapi can't run. The
        # hwdownload variant keeps decode on the GPU (the expensive part for
        # 4K HEVC), pulls frames to system memory just for the cheaper scale,
        # then re-uploads for the GPU encoder. format=nv12|p010le accepts
        # 8-bit and Main10 sources; the trailing format=nv12 forces 8-bit
        # output for h264_vaapi (Main profile).
        # HDR path uses zscale to tonemap PQ/HLG → BT.709 SDR before encode
        # — h264_vaapi outputs SDR, and naive p010le→nv12 colorspace truncation
        # produces "deepfried" HDR-on-SDR output (clipped highlights, oversaturated).
        # zscale is CPU-only, so HDR sources are decoded on CPU (hw_decode is
        # forced off above for HDR).
        # Subtitle burn-in: the `subtitles=` filter is CPU-only and renders
        # via libass. It must run on SDR frames in CPU memory:
        #   - HDR branch: AFTER tonemap (so subs render onto SDR pixels) and
        #     BEFORE the final scale + hwupload.
        #   - HW-decode branch: AFTER hwdownload (CPU frames) and BEFORE
        #     hwupload — render then push back to GPU.
        #   - CPU-decode branch: BEFORE format=nv12,hwupload.
        if is_hdr:
            # Reference: ffmpeg HDR-to-SDR best-practice chain. npl=100 targets
            # SDR-display peak luminance (100 nits); mobius is the fastest
            # tonemap operator for the typical PQ-mastered movie source.
            tonemap_pre = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            tonemap_post = f"scale=-2:{out_h},format=nv12,hwupload"
            vf = (
                f"{tonemap_pre},{sub_filter},{tonemap_post}"
                if sub_filter else f"{tonemap_pre},{tonemap_post}"
            )
        elif hw_decode:
            pre = f"hwdownload,format=nv12|p010le"
            post = f"scale=-2:{out_h},format=nv12,hwupload"
            vf = (
                f"{pre},{sub_filter},{post}"
                if sub_filter else f"{pre},{post}"
            )
        else:
            post = f"scale=-2:{out_h},format=nv12,hwupload"
            vf = f"{sub_filter},{post}" if sub_filter else post
        cmd += [
            "-vf", vf,
            "-c:v", "h264_vaapi",
            "-low_power", "1",
            "-rc_mode", "CQP",
            "-qp", VIDEO_QP,
            "-profile:v", "main",
            # Force IDR frames on exact HLS segment boundaries. Without this,
            # h264_vaapi picks its own GOP cadence and segments can begin
            # mid-GOP — Chrome's decoder is forgiving, Firefox's fmp4 demuxer
            # either drops the segment or shows blocky/frozen frames until the
            # next IDR, visible as stutter at every segment boundary.
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_TIME})",
            # Tag the output as BT.709 limited-range (the modern HD standard).
            # VAAPI encoders frequently omit colour_description_present_flag
            # in the SPS; Firefox's decoder then guesses inconsistently across
            # versions — tinted output, occasional decode rejection. (HDR
            # sources still need a tonemap step before encode — ROADMAP item.)
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            # Insert Access Unit Delimiters between every encoded frame.
            # Firefox's MP4 demuxer uses AUDs to find frame boundaries inside
            # fmp4 segments; h264_vaapi sometimes omits them, forcing a
            # slower/less-reliable scan path.
            "-bsf:v", "h264_metadata=aud=insert",
        ]
    else:
        # Pure CPU path. Same HDR tonemap chain as the VAAPI branch when needed,
        # else just a plain scale. Subs burn in BEFORE scale (cheaper to
        # render once at source resolution than per-line at output) and AFTER
        # tonemap when HDR.
        if is_hdr:
            tonemap_pre = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            scale = f"scale=-2:{out_h}"
            vf = (
                f"{tonemap_pre},{sub_filter},{scale}"
                if sub_filter else f"{tonemap_pre},{scale}"
            )
        else:
            scale = f"scale=-2:{out_h}"
            vf = f"{sub_filter},{scale}" if sub_filter else scale
        cmd += [
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
        ]
        # zerolatency exists for the live stream's lag budget; VOD has no
        # shared live edge, so keep b-frames/lookahead for better quality.
        if mode != "vod":
            cmd += ["-tune", "zerolatency"]
        cmd += [
            "-b:v", video_bitrate,
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_TIME})",
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
        ]
    cmd += [
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ac", "2",
        "-f", "hls",
        "-hls_time", HLS_SEG_TIME,
    ]
    if mode == "vod":
        cmd += [
            # Full playlist, nothing deleted: segments live on the disk-backed
            # /hls/vod volume, so back-seek within the transcoded range is free
            # and client-side. No omit_endlist — when the transcode completes,
            # ffmpeg writes #EXT-X-ENDLIST and the player fires a real `ended`
            # instead of hanging at the last segment waiting for a live edge.
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
        ]
    else:
        cmd += [
            "-hls_list_size", HLS_LIST_SIZE,
            # +program_date_time tags every segment with the server's wall clock.
            # Clients use that as a universal reference to converge on the same
            # playhead (vs. each holding their own per-buffer "live edge").
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
        ]
    cmd += [
        # fmp4 segments (.m4s) instead of mpegts (.ts). Firefox's MSE doesn't
        # accept raw mpegts, so with .ts segments Hls.js transmuxes each one
        # to fmp4 in JavaScript before appending — a known FF stutter source
        # (chronic decoder starvation despite segments arriving on time).
        # Emitting fmp4 directly skips that JS transmux step. Native iOS
        # Safari and Hls.js on Chrome both handle fmp4 natively.
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        # Segment + init paths are absolute on disk but ffmpeg writes BASENAMES
        # into idx.m3u8, so the per-run playlist references them as relative
        # paths from its own directory. The composer prefixes "run/<id>/" when
        # building the global master.
        "-hls_segment_filename", str(run_dir / "seg_%05d.m4s"),
        str(run_dir / "idx.m3u8"),
    ]
    return cmd


def _build_ffmpeg_abr_cmd(
    input_path: Path | str,
    run_dir: Path,
    start_seconds: float = 0.0,
    audio_idx: int | None = None,
    subtitle_idx: int | None = None,
    audio_input: str | None = None,
) -> list[str]:
    """ABR sibling of `_build_ffmpeg_cmd`: ONE decode → N NVENC/CPU/VAAPI
    encodes in a single ffmpeg, emitting one HLS variant per ladder rung.

    Output layout (per run):
        run/<id>/v0/{init.mp4,seg_*.m4s,idx.m3u8}   # highest rung
        run/<id>/v1/{...}                            # next rung down
        ...
        run/<id>/master.m3u8                         # ffmpeg's per-run master
    The composer ignores ffmpeg's per-run master and stitches the GLOBAL
    master + per-variant media playlists itself (see `_composer_tick_abr`).

    Time alignment across variants is guaranteed by giving every encoder the
    SAME GOP / forced-IDR cadence keyed off presentation time, fed from the
    same decoded frames — so seg_NNNNN.m4s covers the identical wall-clock
    window in every rung and a viewer can switch rungs without a reseek.

    Covers NVENC (primary), libx264 (CPU) and VAAPI. HDR tonemap and burned-in
    subtitles run ONCE on the shared pre-split chain, then the result is
    `split` into the ladder and each branch is scaled to its rung height.
    """
    input_str = str(input_path)
    info = _probe_video_info(input_str) or {}
    src_codec = info.get("codec")
    src_height = info.get("height")
    is_hdr = bool(info.get("is_hdr"))

    ladder = _abr_ladder()
    nv = len(ladder)
    # Per-rung output height — never upscale beyond the source. A 720p source
    # with a 1080p top rung just emits 720p for that rung (no quality gain to
    # be had, and upscaling wastes encoder cycles).
    heights = [
        (min(src_height, int(r["height"])) if src_height else int(r["height"]))
        for r in ladder
    ]
    # The shared pre-split chain (tonemap / sub burn-in) runs at the TALLEST
    # rung — cheaper than running it at source 4K, and the rungs below scale
    # down from there.
    top_h = max(heights)

    # Pre-create the per-variant output subdirs; ffmpeg's HLS muxer won't mkdir
    # them itself.
    for i in range(nv):
        (run_dir / f"v{i}").mkdir(parents=True, exist_ok=True)

    hw_decode = (
        USE_VAAPI and USE_VAAPI_DECODE
        and src_codec in HWACCEL_DECODE_CODECS
        and not is_hdr
    )
    nv_decode = (
        USE_NVENC and src_codec in NVDEC_DECODE_CODECS
        and not info.get("is_dovi")
    )

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if USE_NVENC:
        if nv_decode:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif USE_VAAPI:
        cmd += [
            "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
            "-filter_hw_device", "va",
        ]
        if hw_decode:
            cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                    "-hwaccel_device", "va"]
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += ["-re", "-i", input_str]
    if audio_input:
        if start_seconds > 0:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        cmd += ["-re", "-i", audio_input]
    if audio_input:
        audio_map = "1:a:0"
    else:
        audio_map = f"0:a:{audio_idx}" if audio_idx is not None else "0:a:0?"

    # Subtitle burn-in (same cache contract as the single-rendition builder).
    sub_filter = None
    if SUBTITLE_BURN_IN and subtitle_idx is not None:
        cached = _cached_sub_path(input_str, subtitle_idx)
        if cached.exists():
            sub_filter = _subtitles_filter(str(cached), 0)
        else:
            _start_sub_extract(input_str, subtitle_idx)
            print(
                f"sub cache miss; extracting in background for next play "
                f"({Path(input_str).name} #{subtitle_idx})",
                file=sys.stderr,
            )

    # Build the shared pre-split filter `base` and a per-rung `scale(h)` that
    # turns the post-split frames into encoder-ready frames.
    #   - base: tonemap + sub burn-in, run once, leaving frames on the surface
    #     the per-rung scaler expects.
    #   - scale(h): downscale to the rung height + put frames where the encoder
    #     wants them (CUDA for nvenc, system nv12 for x264, GPU for vaapi).
    base = ""
    if USE_NVENC:
        if is_hdr:
            tonemap = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            if nv_decode:
                base = (
                    f"scale_cuda=-2:{top_h}:format=p010le,"
                    f"hwdownload,format=p010le,{tonemap}"
                )
            else:
                base = f"scale=-2:{top_h},{tonemap}"
            if sub_filter:
                base += f",{sub_filter}"
            base += ",format=nv12,hwupload_cuda"
            scale = lambda h: f"scale_cuda=-2:{h}:format=nv12"
        elif nv_decode:
            if sub_filter:
                # libass is CPU-only: pull tallest-rung frames down, burn, push
                # back to the GPU; rungs scale down from there on the GPU.
                base = (
                    f"scale_cuda=-2:{top_h}:format=nv12,"
                    f"hwdownload,format=nv12,{sub_filter},format=nv12,hwupload_cuda"
                )
                scale = lambda h: f"scale_cuda=-2:{h}:format=nv12"
            else:
                # All-GPU fast path: frames never leave the card. Each rung is a
                # cheap scale_cuda off the shared decoded surface.
                base = ""
                scale = lambda h: f"scale_cuda=-2:{h}:format=nv12"
        else:
            # CPU decode → NVENC encode. Shared chain stays in system memory;
            # each rung scales (CPU) then uploads to CUDA for its encoder.
            base = sub_filter or ""
            scale = lambda h: f"scale=-2:{h},format=nv12,hwupload_cuda"
        venc = lambda i, vb, buf: [
            f"-c:v:{i}", "h264_nvenc",
            f"-preset:v:{i}", "p4",
            f"-tune:v:{i}", "ll",
            f"-rc:v:{i}", "cbr",
            f"-b:v:{i}", vb,
            f"-maxrate:v:{i}", vb,
            f"-bufsize:v:{i}", buf,
            f"-profile:v:{i}", "main",
        ]
        # GOP/IDR + AUD are global so EVERY rung shares the cadence — this is
        # what keeps the rungs segment-aligned.
        gop = max(1, int(round((info.get("fps") or 30) * float(HLS_SEG_TIME))))
        global_v = [
            "-g", str(gop),
            "-forced-idr", "1",
            "-no-scenecut", "1",
            "-bsf:v", "h264_metadata=aud=insert",
        ]
    elif USE_VAAPI:
        if is_hdr:
            tonemap = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            base = f"scale=-2:{top_h},{tonemap}"
            if sub_filter:
                base += f",{sub_filter}"
        elif hw_decode:
            # iGPU lacks VAEntrypointVideoProc: download for the CPU scale, burn
            # subs on CPU frames, re-upload per rung.
            base = "hwdownload,format=nv12|p010le"
            if sub_filter:
                base += f",{sub_filter}"
        else:
            base = sub_filter or ""
        scale = lambda h: f"scale=-2:{h},format=nv12,hwupload"
        venc = lambda i, vb, buf: [
            f"-c:v:{i}", "h264_vaapi",
            f"-low_power:v:{i}", "1",
            f"-rc_mode:v:{i}", "CQP",
            f"-qp:v:{i}", VIDEO_QP,
            f"-profile:v:{i}", "main",
        ]
        global_v = [
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_TIME})",
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-bsf:v", "h264_metadata=aud=insert",
        ]
    else:
        # Pure CPU (libx264). Tonemap + subs once on the shared chain; rungs
        # are plain CPU downscales.
        if is_hdr:
            tonemap = (
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
            base = f"scale=-2:{top_h},{tonemap}"
            if sub_filter:
                base += f",{sub_filter}"
        else:
            base = sub_filter or ""
        scale = lambda h: f"scale=-2:{h}"
        venc = lambda i, vb, buf: [
            f"-c:v:{i}", "libx264",
            f"-preset:v:{i}", "veryfast",
            f"-tune:v:{i}", "zerolatency",
            f"-b:v:{i}", vb,
        ]
        global_v = [
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEG_TIME})",
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
        ]

    # filter_complex: shared base (optional), then split into N, then per-rung
    # scale. Labels: [v0]..[v{N-1}] feed the encoders in ladder order.
    split_labels = "".join(f"[t{i}]" for i in range(nv))
    head = f"[0:v]{base + ',' if base else ''}split={nv}{split_labels}"
    parts = [head]
    for i, h in enumerate(heights):
        parts.append(f"[t{i}]{scale(h)}[v{i}]")
    filter_complex = ";".join(parts)
    cmd += ["-filter_complex", filter_complex, "-sn"]

    # Per-rung video maps + encoder args, in ladder order.
    for i, r in enumerate(ladder):
        cmd += ["-map", f"[v{i}]"]
        cmd += venc(i, str(r["video_bitrate"]), str(r["bufsize"]))
    cmd += global_v
    # Audio: one encoded copy per rung so each variant playlist is self-
    # contained (video+audio muxed in the same fmp4 segment, matching the
    # legacy single-rendition layout the composer already understands).
    for _ in range(nv):
        cmd += ["-map", audio_map]
    cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2"]

    var_stream_map = " ".join(f"v:{i},a:{i}" for i in range(nv))
    cmd += [
        "-f", "hls",
        "-hls_time", HLS_SEG_TIME,
        "-hls_list_size", HLS_LIST_SIZE,
        "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(run_dir / "v%v" / "seg_%05d.m4s"),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", var_stream_map,
        str(run_dir / "v%v" / "idx.m3u8"),
    ]
    return cmd


_library_cache_lock = threading.Lock()
_library_cache: dict = {"sig": None, "files": None}


def _library_signature() -> int | None:
    """Cheap directory-tree fingerprint for the library cache. POSIX bumps a
    directory's mtime when an entry is added or removed in it, so hashing the
    mtime of every directory under MEDIA_ROOT captures any add/remove
    anywhere in the tree — including a new episode dropped into an existing
    `tv/Show/Season N/` folder, which the previous top-level-only check
    missed. Files are NOT stat'd here (would defeat the cache); typical
    100-dir library is ~1-3 ms vs the full file scan's ~13 ms."""
    try:
        sig = hash(MEDIA_ROOT.stat().st_mtime_ns)
    except OSError:
        return None
    try:
        for root, dirs, _files in os.walk(MEDIA_ROOT):
            for d in dirs:
                if d.startswith("."):
                    continue
                try:
                    # XOR-mix so order doesn't matter (os.walk visits in
                    # arbitrary order); name+mtime so a rename also fires.
                    sig ^= hash((d, Path(root, d).stat().st_mtime_ns))
                except OSError:
                    pass
    except OSError:
        return None
    return sig


def _scan_library() -> list[Path]:
    """Walk MEDIA_ROOT, returning every playable file. Result is cached and
    reused while the library's directory tree signature is unchanged — any
    file or folder add/remove at any depth bumps a containing directory's
    mtime, which the signature hashes (see _library_signature). Typical
    library: ~1-3 ms signature check, ~13 ms full scan on miss."""
    sig = _library_signature()
    with _library_cache_lock:
        if sig is not None and _library_cache["sig"] == sig and _library_cache["files"] is not None:
            return list(_library_cache["files"])
    files: list[Path] = []
    try:
        for root, _dirs, names in os.walk(MEDIA_ROOT):
            for name in names:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in VIDEO_EXTS:
                    files.append(Path(root) / name)
    except Exception as e:
        print(f"library scan failed: {e}", file=sys.stderr)
        return []
    with _library_cache_lock:
        _library_cache["sig"] = sig
        _library_cache["files"] = files
    return list(files)


def _pick_random_from_library() -> dict | None:
    """Return a random playable file as a source dict, or None if the library
    has no playable videos.

    Auto-pick has no UI for choosing a subtitle track, so subtitle_idx is
    set to None — _start_stream sees the key present and skips its legacy
    auto-pick fallback. (If you want subs on auto-fill picks, change this.)"""
    candidates = _scan_library()
    if not candidates:
        return None
    pick = random.choice(candidates)
    return {
        "type": "file",
        "ref": str(pick.relative_to(MEDIA_ROOT)),
        "title": pick.name,
        "duration": None,
        "is_live": False,
        "subtitle_idx": None,
    }


def _parse_run_idx(run_dir: Path, subdir: str = "") -> tuple[str | None, list[dict]]:
    """Read a run's idx.m3u8 and return (init_uri, [segment dicts]). Each
    segment dict has 'filename', 'duration', 'pdt'. Skips runs that don't
    have an EXT-X-MAP yet (ffmpeg starting up) or whose idx.m3u8 is missing.

    `subdir` (e.g. "v0") reads a per-variant playlist under the run dir in ABR
    mode; "" reads the legacy single-rendition idx.m3u8 at the run root."""
    idx = (run_dir / subdir / "idx.m3u8") if subdir else (run_dir / "idx.m3u8")
    init_uri: str | None = None
    segments: list[dict] = []
    pending_pdt: str | None = None
    pending_duration: float | None = None
    try:
        text = idx.read_text()
    except (FileNotFoundError, OSError):
        return None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            # Format: #EXT-X-MAP:URI="init.mp4"[,...]
            for part in line[len("#EXT-X-MAP:"):].split(","):
                if part.startswith("URI="):
                    init_uri = part[4:].strip().strip('"')
                    break
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = line[len("#EXT-X-PROGRAM-DATE-TIME:"):]
        elif line.startswith("#EXTINF:"):
            d = line[len("#EXTINF:"):].rstrip(",").rstrip()
            try:
                pending_duration = float(d)
            except ValueError:
                pending_duration = None
        elif not line.startswith("#"):
            if pending_duration is None:
                # Rare: segment URI without a preceding EXTINF (shouldn't
                # happen with ffmpeg's HLS muxer). Skip.
                continue
            segments.append({
                "filename": line,
                "duration": pending_duration,
                "pdt": pending_pdt,
            })
            pending_pdt = None
            pending_duration = None
    return init_uri, segments


def _list_run_ids() -> list[int]:
    if not RUN_DIR_BASE.exists():
        return []
    out = []
    for entry in RUN_DIR_BASE.iterdir():
        if entry.is_dir():
            try:
                out.append(int(entry.name))
            except ValueError:
                pass
    return sorted(out)  # run_id is monotonic so sort = chronological


def _composer_assign_seqs(run_id: int, segments: list[dict]):
    """Assign a stable media-sequence to each new segment and a stable
    discontinuity-sequence to each new run. Idempotent — re-seeing a known
    segment keeps its existing seq."""
    with _composer_state_lock:
        if run_id not in _composer_state["run_to_disc"]:
            if not _composer_state["first_run_seen"]:
                _composer_state["run_to_disc"][run_id] = 0
                _composer_state["first_run_seen"] = True
            else:
                _composer_state["run_to_disc"][run_id] = _composer_state["next_disc"]
                _composer_state["next_disc"] += 1
        for seg in segments:
            key = (run_id, seg["filename"])
            if key not in _composer_state["seg_to_seq"]:
                _composer_state["seg_to_seq"][key] = _composer_state["next_seq"]
                _composer_state["next_seq"] += 1


def _atomic_write(path: Path, text: str) -> bool:
    """Write `text` to `path` atomically (write-tmp + rename). POSIX rename is
    atomic so a reader never sees a half-written playlist. Returns success."""
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"composer: write {path.name} failed: {e}", file=sys.stderr)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False


def _stitch_media_playlist(runs, prefix) -> tuple[str | None, set[int]]:
    """Build ONE live media playlist (text) from `runs` — a list of
    (run_id, init_uri, [segment dicts]) tuples in chronological order. `prefix`
    is a callable run_id -> URI-prefix that locates that run's segments + init
    relative to /hls (e.g. "run/7/" single, or "run/7/v0/" for ABR rung 0).

    Returns (playlist_text_or_None, run_ids_still_in_window). Caller assigns
    seqs (via `_composer_assign_seqs`) BEFORE calling. In ABR mode this is
    invoked once per rung; because seqs are keyed by (run_id, filename) and the
    rungs share identical segment filenames + timing, every rung gets matching
    MEDIA-SEQUENCE numbering and a player can switch rungs without a reseek."""
    if not runs:
        return None, set()
    # Truncate the global window from the FRONT to the most recent
    # HLS_LIST_SIZE segments. Older segments roll off (files may linger until
    # ffmpeg's delete_segments or finished-run cleanup catches up).
    global_window = int(HLS_LIST_SIZE)
    total = sum(len(segs) for _, _, segs in runs)
    drop = max(0, total - global_window)
    kept: list[tuple[int, str, list[dict]]] = []
    for rid, init_uri, segs in runs:
        if drop >= len(segs):
            drop -= len(segs)
            continue
        if drop > 0:
            segs = segs[drop:]
            drop = 0
        kept.append((rid, init_uri, segs))
    if not kept:
        return None, set()

    first_run_id, _, first_segs = kept[0]
    first_seg_key = (first_run_id, first_segs[0]["filename"])
    with _composer_state_lock:
        media_seq = _composer_state["seg_to_seq"][first_seg_key]
        disc_seq = _composer_state["run_to_disc"][first_run_id]
    target_dur = max(
        int(round(seg["duration"])) for _, _, segs in kept for seg in segs
        if seg.get("duration")
    ) if any(seg.get("duration") for _, _, segs in kept for seg in segs) else int(float(HLS_SEG_TIME))
    # HLS spec: TARGETDURATION must be >= the longest segment, rounded up.
    target_dur = max(target_dur, int(float(HLS_SEG_TIME)) + 1)

    lines: list[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",   # 7 is the floor for fmp4 + EXT-X-MAP
        f"#EXT-X-TARGETDURATION:{target_dur}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for run_idx, (rid, init_uri, segs) in enumerate(kept):
        # Discontinuity marker between runs (not before the very first run in
        # the playlist — that boundary is implicit, and DISCONTINUITY-SEQUENCE
        # already accounts for runs that rolled off).
        if run_idx > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        # EXT-X-MAP MUST appear before the segments it applies to. Each run has
        # its own init.mp4, so we emit a fresh MAP at every run boundary.
        lines.append(f'#EXT-X-MAP:URI="{prefix(rid)}{init_uri}"')
        for seg in segs:
            if seg["pdt"]:
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg['pdt']}")
            duration = seg["duration"] if seg["duration"] is not None else float(HLS_SEG_TIME)
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(f"{prefix(rid)}{seg['filename']}")
    # No EXT-X-ENDLIST — this is a live playlist, the player must keep polling.
    body = "\n".join(lines) + "\n"
    return body, {rid for rid, _, _ in kept}


def _composer_cleanup_finished(kept_run_ids: set[int]) -> None:
    """Remove finished runs whose segments have all rolled out of the live
    window. The active run is never touched here (only _stop_locked / a fresh
    _start_stream retire it). Shared by the single + ABR composer paths."""
    with state_lock:
        finished_snapshot = set(finished_run_ids)
        currently_active = active_run_id
    for rid in finished_snapshot:
        if rid == currently_active or rid in kept_run_ids:
            continue
        try:
            shutil.rmtree(_run_dir(rid))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"composer: cleanup of run {rid} failed: {e}", file=sys.stderr)
            continue
        with state_lock:
            finished_run_ids.discard(rid)
        # Memory hygiene: drop the assigned seqs for this run.
        with _composer_state_lock:
            for key in [k for k in _composer_state["seg_to_seq"] if k[0] == rid]:
                del _composer_state["seg_to_seq"][key]
            _composer_state["run_to_disc"].pop(rid, None)


def _composer_tick():
    """Stitch /hls/stream.m3u8 from all run dirs. In ABR mode (ABR_LADDER=1)
    this delegates to `_composer_tick_abr` which publishes a multi-variant
    master + per-rung media playlists. Otherwise it builds the single
    live media playlist. Deletes the master when nothing is playable (idle
    signal for /api/status's playlist_ready check)."""
    if ABR_LADDER:
        _composer_tick_abr()
        return
    run_ids = _list_run_ids()
    runs: list[tuple[int, str, list[dict]]] = []  # (run_id, init_uri, segments)
    for rid in run_ids:
        init_uri, segs = _parse_run_idx(_run_dir(rid))
        if init_uri is None or not segs:
            continue
        _composer_assign_seqs(rid, segs)
        runs.append((rid, init_uri, segs))

    master = HLS_DIR / "stream.m3u8"
    text, kept_run_ids = _stitch_media_playlist(runs, lambda rid: f"run/{rid}/")
    if text is None:
        try:
            master.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(master, text)
    _composer_cleanup_finished(kept_run_ids)


def _composer_tick_abr() -> None:
    """ABR composer: publish /hls/stream.m3u8 as a multi-variant MASTER and one
    /hls/v<k>.m3u8 live media playlist per ladder rung.

    Each run's ffmpeg writes run/<id>/v<k>/{init.mp4,seg_*.m4s,idx.m3u8}. We
    stitch each rung's runs into its own media playlist (reusing the single-
    rendition stitcher) and emit a master that points at them with
    BANDWIDTH/RESOLUTION so hls.js can auto-select/downshift.

    Sequence numbering is assigned from the UNION of segments across rungs per
    run, so even if one rung is briefly a segment ahead at the live edge, every
    rung that DOES have a given segment numbers it identically — keeping the
    rungs time-aligned for seamless switching."""
    ladder = _abr_ladder()
    nv = len(ladder)
    run_ids = _list_run_ids()
    per_variant: list[list[tuple[int, str, list[dict]]]] = [[] for _ in range(nv)]
    for rid in run_ids:
        union: dict[str, dict] = {}
        for k in range(nv):
            init_uri, segs = _parse_run_idx(_run_dir(rid), f"v{k}")
            if init_uri is None or not segs:
                continue
            for s in segs:
                union.setdefault(s["filename"], s)
            per_variant[k].append((rid, init_uri, segs))
        # Assign seqs once per run from the union, in filename order, so seqs
        # advance monotonically with time across all rungs.
        if union:
            _composer_assign_seqs(rid, [union[fn] for fn in sorted(union)])

    master = HLS_DIR / "stream.m3u8"
    written: list[int] = []
    all_kept: set[int] = set()
    for k in range(nv):
        text, kept_ids = _stitch_media_playlist(
            per_variant[k], lambda rid, kk=k: f"run/{rid}/v{kk}/"
        )
        vpath = HLS_DIR / f"v{k}.m3u8"
        if text is None:
            try:
                vpath.unlink()
            except FileNotFoundError:
                pass
            continue
        if _atomic_write(vpath, text):
            written.append(k)
            all_kept |= kept_ids

    if not written:
        for p in [master] + [HLS_DIR / f"v{k}.m3u8" for k in range(nv)]:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        return

    audio_bps = _bitrate_to_bps(AUDIO_BITRATE)
    mlines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for k in written:
        r = ladder[k]
        bw = _bitrate_to_bps(r["video_bitrate"]) + audio_bps
        h = int(r["height"])
        # Nominal 16:9 width for RESOLUTION — the real frame width tracks source
        # aspect ratio (we scale height-locked with -2), so this is advisory for
        # the player's capLevelToPlayerSize heuristic, not exact.
        w = (round(h * 16 / 9) // 2) * 2
        mlines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},"
            f'CODECS="avc1.4d401f,mp4a.40.2"'
        )
        mlines.append(f"v{k}.m3u8")
    _atomic_write(master, "\n".join(mlines) + "\n")
    _composer_cleanup_finished(all_kept)


def _composer_thread():
    while True:
        try:
            _composer_tick()
        except Exception as e:
            print(f"composer: tick failed: {e}", file=sys.stderr)
        time.sleep(COMPOSER_TICK_S)


def _watcher():
    """Keep something playing: advance the queue, then auto-fill from the library
    if enabled. Triggered by ffmpeg exiting and also when fully idle (e.g. fresh
    startup or after admin stop)."""
    last_state_save = 0.0
    while True:
        time.sleep(1)
        try:
            with state_lock:
                proc = current_proc
                paused = current_paused
                # Build a snapshot of live globals while holding the lock; do the
                # actual file write below, after we've dropped it.
                snapshot = _state_snapshot_locked()
            # Still streaming — periodically persist position so a crash recovers near where we were.
            if proc is not None and proc.poll() is None:
                now = time.time()
                if snapshot is not None and now - last_state_save >= STATE_SAVE_INTERVAL:
                    _save_state(snapshot)
                    last_state_save = now
                # Try to kick off the next item's ffmpeg ahead of the current
                # source's EOF. _start_preroll_locked is a cheap no-op when
                # already pre-rolled or not yet near the end; the work only
                # fires inside the last PREROLL_LEAD_SECS of duration.
                with state_lock:
                    _start_preroll_locked()
                continue
            # Holding a paused position — leave it alone.
            if paused:
                continue
            # If a process exited, make sure user action hasn't superseded us,
            # and check for a pre-rolled successor: if the watcher set one up
            # in the lead-up to EOF, promoting it skips the full ffprobe +
            # spawn dance and the player sees an unbroken segment stream.
            if proc is not None:
                with state_lock:
                    if current_proc is not proc:
                        continue
                    if _promote_preroll_locked():
                        # Snapshot the new current source for state persistence.
                        promote_snap = _state_snapshot_locked()
                    else:
                        promote_snap = None
                if promote_snap is not None:
                    _save_state(promote_snap)
                    continue
            # Pick what to play next: queue first, then random library fallback.
            with playlist_lock:
                next_item = playlist.pop(0) if playlist else None
                if next_item is not None:
                    _save_playlist()
            if next_item is None:
                with settings_lock:
                    auto_fill_on = settings.get("auto_fill", True)
                if auto_fill_on:
                    next_item = _pick_random_from_library()
            if next_item is not None:
                try:
                    _start_stream(next_item)
                    continue
                except Exception as e:
                    print(f"watcher: failed to start {next_item.get('title')!r}: {e}",
                          file=sys.stderr)
                    # Back off so a persistently-broken pick can't pin a CPU.
                    time.sleep(4)
                    continue
            # Nothing to play — finalize idle if a process just exited.
            if proc is not None:
                cleared = False
                with state_lock:
                    if current_proc is proc:
                        _stop_locked()
                        cleared = True
                if cleared:
                    # _cleanup_hls acquires its own locks — outside state_lock.
                    _cleanup_hls()
                    _clear_state()
        except Exception as e:
            print(f"watcher: {e}", file=sys.stderr)


def _start_stream(source: dict, start_seconds: float = 0.0):
    """Start streaming the given source. May re-resolve URLs to refresh signed links.

    Spawns ffmpeg into a fresh /hls/run/<run_id>/ subdir; the previous run (if
    any) is marked finished and the composer keeps stitching its still-on-disk
    segments into the master playlist with an EXT-X-DISCONTINUITY between them
    and the new run's first segment. Viewers see one continuous stream.m3u8."""
    global current_proc, current_source, current_start_offset, current_start_time
    global current_paused, paused_position
    audio_input: str | None = None  # set on the URL DASH path; passed as a 2nd ffmpeg -i
    if source["type"] == "file":
        full_path = _safe_resolve(source["ref"], must_be_file=True)
        ffmpeg_input = str(full_path)
        # Always re-probe in case the file changed; cheap.
        if source.get("duration") is None:
            source = {**source, "duration": _probe_duration(full_path)}
    elif source["type"] == "url":
        resolved = _resolve_url(source["ref"])
        ffmpeg_input = resolved["stream_url"]
        audio_input = resolved.get("audio_url")
        source = {
            **source,
            "title": resolved["title"],
            "duration": resolved["duration"],
            "is_live": resolved["is_live"],
        }
    else:
        raise ValueError(f"unknown source type: {source.get('type')!r}")
    duration = source.get("duration")
    if duration is not None and start_seconds >= duration:
        start_seconds = max(0.0, duration - 1.0)
    audio_idx = _probe_english_audio(ffmpeg_input)
    # Subtitle burn-in: auto-pick whenever the source dict doesn't carry a
    # usable index. "Usable" = an int — both missing keys AND explicit `null`
    # re-trigger the picker. Without the null-re-pick, a state.json or queue
    # entry that was added while USE_SUBTITLES was off (subtitle_idx → null)
    # would stay sub-less forever once burn-in was flipped on. URLs don't
    # carry sub indices and are skipped.
    if source["type"] == "file" and source.get("subtitle_idx") is None:
        source = {**source, "subtitle_idx": _pick_default_subtitle(ffmpeg_input)}
    subtitle_idx = source.get("subtitle_idx") if source["type"] == "file" else None
    old_source = None
    old_pos = None
    with state_lock:
        global active_run_id, next_run_id
        # Retire the old run (its segments stay on disk + in the master until
        # they roll out of the global window — that's what makes the
        # transition seamless).
        if _source_key(current_source) != _source_key(source):
            old_source = current_source
            old_pos = _current_position()
            if old_pos is None and current_paused:
                old_pos = paused_position
        _terminate_proc_locked()
        if active_run_id is not None:
            finished_run_ids.add(active_run_id)
        run_id = next_run_id
        next_run_id += 1
        run_dir = _run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        builder = _build_ffmpeg_abr_cmd if ABR_LADDER else _build_ffmpeg_cmd
        cmd = builder(
            ffmpeg_input, run_dir, start_seconds, audio_idx, subtitle_idx,
            audio_input=audio_input,
        )
        current_proc = _spawn_ffmpeg(cmd, run_dir / "ffmpeg.log")
        active_run_id = run_id
        current_source = source
        current_start_offset = start_seconds
        current_start_time = time.time()
        current_paused = False
        paused_position = 0.0
        snapshot = _state_snapshot_locked()
    # New item playing — wipe any skip votes from the last one.
    with skip_votes_lock:
        skip_votes.clear()
    _recent_record(old_source, old_pos)
    _save_state(snapshot)


def _restore_state_on_startup():
    """If we shut down with something playing or paused, resume exactly where
    we left off. Falls back to idle on any failure (the watcher will then
    auto-pick the next queue item / random fallback)."""
    global current_source, current_start_offset, current_start_time
    global current_paused, paused_position
    saved = _load_state()
    if saved is None:
        return
    source = saved["source"]
    position = float(saved.get("position_seconds") or 0.0)
    was_paused = bool(saved.get("paused"))
    if was_paused:
        # No ffmpeg, no run dir — just remember the source + position so the
        # watcher leaves things alone and the next admin Resume picks up here.
        with state_lock:
            current_source = source
            current_start_offset = 0.0
            current_start_time = 0.0
            current_paused = True
            paused_position = position
        return
    try:
        _start_stream(source, start_seconds=position)
    except Exception as e:
        print(f"state: restore failed for {source.get('title')!r}: {e}", file=sys.stderr)
        _clear_state()


# Wipe any leftover /hls/ state from a previous run before we spawn anything —
# orphaned run dirs + composer state would otherwise show up in the master.
_cleanup_hls()
_restore_state_on_startup()
threading.Thread(target=_composer_thread, daemon=True, name="jetstream-composer").start()
threading.Thread(target=_watcher, daemon=True, name="jetstream-watcher").start()
threading.Thread(target=_arr_refresh_loop, daemon=True, name="arr-refresh").start()


@app.before_request
def _gate_viewer_routes():
    p = request.path
    if p.startswith("/admin"):
        return None  # Traefik handles admin auth
    if p == "/api/_authcheck":
        return None  # nginx subrequest endpoint — has its own logic below
    if p == "/api/now-playing":
        return None  # title-only external display feed — deliberately tokenless
    # v2 login surface — reachable logged-out by definition. The theme CSS is
    # exempt too (login.html links it); nothing else static is opened up —
    # the /build/ bundles stay token-gated so a private instance doesn't hand
    # its full client-side route map to anonymous scanners.
    if p in ("/login", "/api/auth/login", "/jetstream-theme.css",
             # The home screen's two doors, both necessarily pre-auth:
             # redeem a friend code, or register with one.
             "/api/invite/redeem", "/api/auth/register"):
        return None
    if p == "/api/_authcheck_vod":
        return None  # nginx subrequest endpoint — has its own logic below
    # v2 user area: the library browser + VOD control APIs require a logged-in
    # user session (never an invite token — invite links stay watch/control
    # only). Pages redirect to login; APIs get JSON 401s.
    if (p in ("/library", "/home") or p.startswith("/api/user/")
            or p.startswith("/api/vod/")
            or p in ("/api/auth/logout", "/api/auth/me")):
        if _session_user():
            return None
        if p in ("/library", "/home"):
            return redirect("/login?next=" + urllib.parse.quote(p))
        return jsonify({"error": "auth_required"}), 401
    # Control surface (the /controls page + /api/control/* endpoints) requires
    # a friend-or-admin token regardless of public mode — viewers and the
    # anonymous public can watch but never drive playback. Check this before
    # the public-mode short-circuit so public mode can't leak controls.
    if p == "/controls" or p.startswith("/api/control/"):
        # Honor a ?t= invite on the control page the same way viewer routes do,
        # so a friend link lands straight on /controls with the cookie set.
        qs_t = request.args.get("t")
        if p == "/controls" and qs_t and _token_level(qs_t) in CONTROL_LEVELS:
            _mark_token_seen(qs_t)
            resp = redirect(p)
            resp.set_cookie(
                TOKEN_COOKIE, qs_t,
                max_age=TOKEN_COOKIE_MAX_AGE,
                httponly=True, secure=True, samesite="Lax",
            )
            return resp
        if _caller_can_control():
            _mark_token_seen(request.cookies.get(TOKEN_COOKIE))
            return None
        if p == "/controls":
            return send_from_directory("static", "home.html"), 401
        return ("", 403)
    cookie_t = request.cookies.get(TOKEN_COOKIE)
    if _valid_token(cookie_t):
        _mark_token_seen(cookie_t)
        return None
    # A logged-in user is at least a viewer: accounts can watch the live
    # stream without also needing an invite token.
    if _session_user():
        return None
    qs_t = request.args.get("t")
    if _valid_token(qs_t):
        _mark_token_seen(qs_t)
        resp = redirect(p)
        resp.set_cookie(
            TOKEN_COOKIE, qs_t,
            max_age=TOKEN_COOKIE_MAX_AGE,
            httponly=True, secure=True, samesite="Lax",
        )
        return resp
    with settings_lock:
        if settings.get("viewer_public"):
            return None  # public mode — anyone can watch
    if p == "/":
        # The front door: redeem a friend code, or sign in. Replaces the old
        # dead-end "you need an invite link" page — that page had no way in
        # for someone holding credentials, and no way to TYPE a code at all
        # (codes only ever arrived as a ?t= URL).
        return send_from_directory("static", "home.html"), 401
    return ("", 401)


@app.route("/api/_authcheck")
def api_authcheck():
    """nginx auth_request subrequest target. nginx forwards the original
    request's Cookie + X-Forwarded-For + User-Agent here; we return 204 if
    the viewer is allowed to fetch /hls/* (public mode OR valid token cookie),
    401 otherwise. nginx then either serves the static segment or rejects
    with 401 itself. Excluded from `_gate_viewer_routes` so the gate doesn't
    return its HTML "no token" page here — nginx wants a body-less response.

    Doubles as the viewer-tracking hook: each /hls/* fetch triggers an
    auth_request, which is now the only Flask touchpoint per segment fetch.
    Without _track_viewer here the active viewers list and history go silent."""
    with settings_lock:
        public = settings.get("viewer_public")
    if (not public and not _valid_token(request.cookies.get(TOKEN_COOKIE))
            and not _session_user()):
        return ("", 401)
    # Continuous-watch cap: a session past the limit gets its segments cut off.
    # Checked before _track_viewer so an expired session stops refreshing its
    # last-seen and ages out of the viewer list, letting a reload start fresh.
    if _viewer_session_expired(_client_ip()):
        return ("", 403)
    # Authorized — log this viewer activity. Idempotent: updates the timestamp
    # for known IPs and only emits a connect-log on the very first sighting.
    _track_viewer()
    return ("", 204)


# ==== v2: auth routes + VOD authcheck ======================================

@app.route("/api/_authcheck_vod")
def api_authcheck_vod():
    """nginx auth_request target for /hls/vod/*. Unlike the live stream, a
    VOD session belongs to exactly one user: 204 only when the caller's
    `js_user` session owns the <sid> embedded in the original URI (forwarded
    by nginx as X-Original-URI). Doubles as the idle-reaper heartbeat — each
    segment fetch bumps last_access, the same trick _authcheck plays with
    _track_viewer."""
    u = _session_user()
    if not u:
        return ("", 401)
    m = re.match(r"^/hls/vod/([A-Za-z0-9_-]+)/", request.headers.get("X-Original-URI", ""))
    if not m:
        return ("", 403)
    with vod_lock:
        sess = vod_sessions.get(m.group(1))
        if not sess or sess.get("user_id") != u["id"]:
            return ("", 403)
        sess["last_access"] = time.time()
    return ("", 204)


@app.route("/login")
def login_page():
    return send_from_directory("static", "login.html")


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    # Rate-limit on the TRUSTED ip: keying on _client_ip (leftmost XFF) lets
    # an attacker rotate forged XFF headers and never trip the limiter.
    if not _ip_rate_check(login_rate, login_rate_lock, _trusted_client_ip(),
                          LOGIN_RATE_WINDOW, LOGIN_RATE_MAX):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    with users_lock:
        user = next((u for u in users if u["username"] == username), None)
        scrypt_rec = dict(user.get("scrypt") or {}) if user else None
        disabled = bool(user.get("disabled")) if user else True
    # scrypt is deliberately slow (~16 MiB / tens of ms) — verify OUTSIDE
    # users_lock so a login burst can't serialize every other user-store
    # reader behind the KDF. (No dummy-verify on unknown usernames: the
    # username set is admin-curated, not enumerable-sensitive.)
    if disabled or not scrypt_rec or not _verify_password(password, scrypt_rec):
        return jsonify({"error": "bad_credentials"}), 401
    with users_lock:
        user = next((u for u in users if u["username"] == username), None)
        if not user or user.get("disabled"):
            return jsonify({"error": "bad_credentials"}), 401
        user["last_login"] = time.time()
        _save_users()
        uid = user["id"]
    sid = _create_user_session(uid)
    resp = jsonify({"ok": True})
    resp.set_cookie(
        USER_SESSION_COOKIE, sid,
        max_age=USER_SESSION_TTL,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


@app.route("/api/invite/redeem", methods=["POST"])
def api_invite_redeem():
    """Type-in twin of the ?t=<code> invite link, for the home screen.
    Validates a code and sets the same `lt` cookie the link flow sets.
    Reports whether the code also unlocks registration so the page can offer
    the account upgrade."""
    if not _ip_rate_check(invite_rate, invite_rate_lock, _trusted_client_ip(),
                          INVITE_RATE_WINDOW, INVITE_RATE_MAX):
        return jsonify({"error": "rate_limited"}), 429
    code = str((request.get_json(silent=True) or {}).get("code") or "").strip()
    level = _token_level(code)
    # The admin pseudo-token is not a redeemable invite — it's minted behind
    # Traefik basicauth and must never be enterable as a code.
    if not level or code == ADMIN_TOKEN_ID:
        return jsonify({"error": "bad_code"}), 401
    _mark_token_seen(code)
    resp = jsonify({
        "ok": True,
        "level": level,
        "can_register": level in REGISTER_LEVELS,
    })
    resp.set_cookie(
        TOKEN_COOKIE, code,
        max_age=TOKEN_COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    """Invite-gated self-registration: a FRIEND-level code (REGISTER_LEVELS)
    lets its holder create an account. There is no open registration — the
    code is re-validated here rather than trusted from the redeem step, so a
    client can't skip straight to this endpoint.

    Codes are reusable by design (one friend link, several housemates). If a
    link leaks, the remedy is deleting the token and the accounts from
    /admin, not a per-code counter."""
    ip = _trusted_client_ip()
    # Peek, don't charge: budget is spent on accounts actually CREATED (see
    # the record=True call below), not on a mistyped password — otherwise two
    # typos lock a whole household (one NAT IP) out for the hour.
    if not _ip_rate_check(register_rate, register_rate_lock, ip,
                          REGISTER_RATE_WINDOW, REGISTER_RATE_MAX,
                          record=False):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip()
    level = _token_level(code)
    if not level or code == ADMIN_TOKEN_ID:
        # A bad code here is code-guessing, the same attack /api/invite/redeem
        # faces — charge it to the shared invite budget so guessing stays
        # bounded even though validation errors are free.
        _ip_rate_check(invite_rate, invite_rate_lock, ip,
                       INVITE_RATE_WINDOW, INVITE_RATE_MAX)
        return jsonify({"error": "bad_code"}), 401
    if level not in REGISTER_LEVELS:
        return jsonify({"error": "code_not_friend"}), 403
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    if not USERNAME_RE.match(username):
        return jsonify({"error": "bad_username",
                        "message": "3-32 chars, a-z 0-9 _ . -"}), 400
    if len(password) < 8:
        return jsonify({"error": "bad_password", "message": "min 8 chars"}), 400
    scrypt_rec = _hash_password(password)  # slow KDF — do it outside the lock
    with users_lock:
        if any(u["username"] == username for u in users):
            return jsonify({"error": "duplicate"}), 409
        rec = {
            "id": secrets.token_urlsafe(12),
            "username": username,
            "scrypt": scrypt_rec,
            "created": time.time(),
            "last_login": time.time(),
            "disabled": False,
            # Provenance: which invite minted this account. Lets the host see
            # who vouched for whom, and find every account from a leaked code.
            "invited_by": _token_label(code),
            "invite_token": code,
        }
        users.append(rec)
        _save_users()
    # Charge the budget now that an account actually exists.
    _ip_rate_check(register_rate, register_rate_lock, ip,
                   REGISTER_RATE_WINDOW, REGISTER_RATE_MAX)
    print(f"[users] register {username!r} via invite "
          f"{_token_label(code)!r}", file=sys.stderr, flush=True)
    sid = _create_user_session(rec["id"])
    resp = jsonify({"ok": True, "username": username})
    # Log them straight in, and keep the invite cookie so the live stream
    # keeps working in the same session.
    resp.set_cookie(
        USER_SESSION_COOKIE, sid,
        max_age=USER_SESSION_TTL,
        httponly=True, secure=True, samesite="Lax",
    )
    resp.set_cookie(
        TOKEN_COOKIE, code,
        max_age=TOKEN_COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


@app.route("/home")
def hub_page():
    # Gate redirects logged-out browsers to /login before this runs.
    return send_from_directory("static", "hub.html")


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    sid = request.cookies.get(USER_SESSION_COOKIE)
    if sid:
        with user_sessions_lock:
            if sid in user_sessions:
                del user_sessions[sid]
                _save_user_sessions()
    resp = jsonify({"ok": True})
    resp.delete_cookie(USER_SESSION_COOKIE)
    return resp


@app.route("/api/auth/me")
def api_auth_me():
    u = _session_user()  # gate guarantees a session, but stay defensive
    if not u:
        return jsonify({"error": "auth_required"}), 401
    return jsonify({"id": u["id"], "username": u["username"]})


@app.route("/admin/api/users", methods=["GET", "POST"])
def api_admin_users():
    if request.method == "GET":
        with vod_lock:
            active_vod_uids = {s.get("user_id") for s in vod_sessions.values()}
        with users_lock:
            return jsonify({"users": [
                {
                    "id": u["id"], "username": u["username"],
                    "created": u.get("created"), "last_login": u.get("last_login"),
                    "disabled": bool(u.get("disabled")),
                    "active_vod": u["id"] in active_vod_uids,
                } for u in users
            ]})
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    if not USERNAME_RE.match(username):
        return jsonify({"error": "bad_username",
                        "message": "3-32 chars, a-z 0-9 _ . -"}), 400
    if len(password) < 8:
        return jsonify({"error": "bad_password", "message": "min 8 chars"}), 400
    with users_lock:
        if any(u["username"] == username for u in users):
            return jsonify({"error": "duplicate"}), 409
        rec = {
            "id": secrets.token_urlsafe(12),
            "username": username,
            "scrypt": _hash_password(password),
            "created": time.time(),
            "last_login": None,
            "disabled": False,
        }
        users.append(rec)
        _save_users()
    return jsonify({"id": rec["id"]})


@app.route("/admin/api/users/<uid>", methods=["DELETE"])
def api_admin_user_delete(uid):
    with users_lock:
        before = len(users)
        users[:] = [u for u in users if u["id"] != uid]
        if len(users) == before:
            return jsonify({"error": "not_found"}), 404
        _save_users()
    _revoke_user_sessions(uid)
    _vod_kill_sessions_for_user(uid)
    # Deleting the account takes its watch history with it — otherwise the
    # rows linger forever keyed to an id nothing can log in as.
    with progress_lock:
        if watch_progress.pop(uid, None) is not None:
            _save_progress_locked(force=True)
    return jsonify({"ok": True})


@app.route("/admin/api/users/<uid>/password", methods=["POST"])
def api_admin_user_password(uid):
    password = str((request.get_json(silent=True) or {}).get("password") or "")
    if len(password) < 8:
        return jsonify({"error": "bad_password", "message": "min 8 chars"}), 400
    with users_lock:
        user = next((u for u in users if u["id"] == uid), None)
        if not user:
            return jsonify({"error": "not_found"}), 404
        user["scrypt"] = _hash_password(password)
        _save_users()
    _revoke_user_sessions(uid)  # a reset means "lock out whoever had it"
    return jsonify({"ok": True})


@app.route("/admin/api/users/<uid>/disabled", methods=["POST"])
def api_admin_user_disabled(uid):
    disabled = bool((request.get_json(silent=True) or {}).get("disabled"))
    with users_lock:
        user = next((u for u in users if u["id"] == uid), None)
        if not user:
            return jsonify({"error": "not_found"}), 404
        user["disabled"] = disabled
        _save_users()
    if disabled:
        _revoke_user_sessions(uid)
        _vod_kill_sessions_for_user(uid)
    return jsonify({"ok": True})


@app.route("/api/session/continue", methods=["POST"])
def api_session_continue():
    """Reset the caller's continuous-watch clock — the "keep watching" action
    behind the session-cap overlay. The cap exists to reclaim abandoned tabs;
    a viewer actively asking to continue is the opposite of abandoned, so we
    honor it and start their 8h over. Viewer-gated by the route gate."""
    with viewers_lock:
        viewer_session_start[_client_ip()] = time.time()
    return jsonify({"ok": True})


def _chat_rate_check(ip: str) -> bool:
    """True if this IP can post one more message within the rolling window.
    Mutates `chat_rate` to record the timestamp on success. Pruned lazily —
    fully-stale entries get dropped here so a one-shot scanner IP doesn't
    leave a permanent `{ip: []}` row in the dict."""
    now = time.time()
    cutoff = now - CHAT_RATE_WINDOW
    with chat_lock:
        timestamps = [t for t in chat_rate.get(ip, []) if t > cutoff]
        if len(timestamps) >= CHAT_RATE_MAX:
            chat_rate[ip] = timestamps
            return False
        timestamps.append(now)
        chat_rate[ip] = timestamps
        return True


@app.route("/chat/recent")
def api_chat_recent():
    """Long-poll-friendly catch-up endpoint. `?since=N` returns every message
    with id > N (ring-buffer-bounded — late joiners get up to CHAT_BUFFER_SIZE
    of history). Also returns `deleted_ids` so clients that already painted a
    message hide it on next poll when the admin removed it. Both viewer and
    admin pages poll this."""
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    with chat_lock:
        messages = [m for m in chat_messages if m["id"] > since]
        # Trim deleted-ids to ids still potentially visible to any client:
        # the live ring's id range. Anything older has been evicted and no
        # poller will ever ask about it.
        if chat_messages:
            oldest = chat_messages[0]["id"]
            chat_deleted_ids.intersection_update(
                {i for i in chat_deleted_ids if i >= oldest}
            )
        deleted = sorted(chat_deleted_ids)
        # Piggyback the chat_rate prune on the poll path: viewers hit this
        # every 2.5 s while the page is open, so the dict stays bounded to
        # IPs that chatted in the last window without a separate sweep timer.
        cutoff = time.time() - CHAT_RATE_WINDOW
        for ip in [k for k, ts in chat_rate.items() if not any(t > cutoff for t in ts)]:
            del chat_rate[ip]
    max_id = messages[-1]["id"] if messages else since
    return jsonify({"messages": messages, "max_id": max_id, "deleted_ids": deleted})


def _clean_chat_name(raw: str | None) -> str:
    """Squeeze a user-provided display name down to printable ASCII-ish text,
    cap to CHAT_NAME_MAX_LEN, fall back to 'anonymous' when nothing's left.
    Strips control chars and zero-widths so a label can't hide its size or
    inject formatting tricks into the chat list."""
    if not raw:
        return "anonymous"
    cleaned = "".join(c for c in raw if c.isprintable() and c not in "​‌‍﻿")
    cleaned = " ".join(cleaned.split())  # collapse whitespace runs
    cleaned = cleaned[:CHAT_NAME_MAX_LEN].strip()
    return cleaned or "anonymous"


@app.route("/chat/send", methods=["POST"])
def api_chat_send():
    """Append a message to the in-memory ring. `sid` is opaque — generated
    client-side, used only to color-dot the message. `name` is a free-text
    display name (also client-driven — no validation against tokens). Per-IP
    rate limit: CHAT_RATE_MAX messages per CHAT_RATE_WINDOW seconds. No
    persistence — a server restart wipes the chat (intentional, per roadmap)."""
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if not text:
        return jsonify({"error": "empty message"}), 400
    if len(text) > CHAT_MSG_MAX_LEN:
        return jsonify({"error": f"message exceeds {CHAT_MSG_MAX_LEN} chars"}), 413
    sid = (data.get("sid") or "").strip()[:32] or "anon"
    name = _clean_chat_name(data.get("name"))
    # Mute check before rate-limit so the muted sid doesn't burn its IP's
    # rate budget on rejected sends.
    now = time.time()
    with chat_lock:
        mute_until = chat_mutes.get(sid)
        if mute_until is not None:
            if mute_until > now:
                return jsonify({
                    "error": "muted",
                    "until": mute_until,
                    "remaining": int(mute_until - now),
                }), 403
            del chat_mutes[sid]
    if not _chat_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    global chat_next_id
    with chat_lock:
        msg = {
            "id": chat_next_id,
            "ts": time.time(),
            "sid": sid,
            "name": name,
            "text": text,
        }
        chat_next_id += 1
        chat_messages.append(msg)
    return jsonify({"ok": True, "id": msg["id"]})


# Chat moderation — host-only (Traefik basicauth gates /admin, so no extra
# token check needed here). Deletes use the message id; mutes target the
# opaque client sid (one browser tab = one sid). Both are in-memory and
# don't survive a restart, matching the chat ring itself.
@app.route("/admin/api/chat/<int:msg_id>", methods=["DELETE"])
def api_chat_delete(msg_id: int):
    """Drop a single message from the ring and remember its id so clients
    that already painted it hide it on next /chat/recent poll."""
    with chat_lock:
        # deque has no .remove(predicate); rebuild without the target. Cheap
        # at maxlen=200. Preserves the maxlen on the new deque.
        before = len(chat_messages)
        kept = [m for m in chat_messages if m["id"] != msg_id]
        if len(kept) == before:
            return jsonify({"error": "message not found"}), 404
        chat_messages.clear()
        chat_messages.extend(kept)
        chat_deleted_ids.add(msg_id)
    return jsonify({"ok": True})


@app.route("/admin/api/chat/mute", methods=["POST"])
def api_chat_mute():
    """Mute a sid for N seconds. Body: {sid, seconds}. Seconds <=0 unmutes.
    sid is what the offender's tab sends to /chat/send — copy it out of the
    message dict in the admin chat panel."""
    data = request.get_json(silent=True) or {}
    sid = (data.get("sid") or "").strip()[:32]
    if not sid:
        return jsonify({"error": "sid required"}), 400
    try:
        seconds = int(data.get("seconds", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "seconds must be an int"}), 400
    with chat_lock:
        if seconds <= 0:
            chat_mutes.pop(sid, None)
            return jsonify({"ok": True, "muted": False})
        # Cap at a week — anything longer should be a token revoke instead.
        seconds = min(seconds, 7 * 24 * 60 * 60)
        until = time.time() + seconds
        chat_mutes[sid] = until
    return jsonify({"ok": True, "muted": True, "until": until, "seconds": seconds})


# ---- Emoji reactions --------------------------------------------------------
# Codepoint ranges that count as emoji (or emoji modifiers / joiners). Generous
# on purpose: the goal is to reject arbitrary text/markup and oversized
# payloads, not to perfectly enumerate Unicode. A stray symbol slipping through
# is harmless — the client renders reactions via textContent, never innerHTML.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # the big pictograph / emoji / supplemental blocks
    (0x2600, 0x27BF),    # misc symbols + dingbats
    (0x2B00, 0x2BFF),    # stars, extra arrows
    (0x2190, 0x21FF),    # arrows
    (0x2300, 0x23FF),    # misc technical (⌚ ⏳ ⏩ …)
    (0x2460, 0x24FF),    # enclosed alphanumerics (Ⓜ …)
    (0x25A0, 0x25FF),    # geometric shapes (▶ ● …)
    (0x200D, 0x200D),    # zero-width joiner (ZWJ sequences)
    (0xFE00, 0xFE0F),    # variation selectors
    (0x20D0, 0x20FF),    # combining marks for symbols (keycap 20E3)
    (0x00A9, 0x00AE),    # © ®
    (0x2122, 0x2122),    # ™
    (0x2139, 0x2139),    # ℹ
)


def _is_emoji(s: str) -> bool:
    """True if `s` is a short emoji (single emoji or a ZWJ/modifier sequence).
    Bounds length and restricts codepoints to the emoji-ish ranges above so a
    direct caller can't push arbitrary text into everyone's reaction overlay."""
    if not s or len(s) > 12:
        return False
    saw_pictograph = False
    for ch in s:
        cp = ord(ch)
        if not any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
            return False
        if cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF \
                or 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF \
                or 0x25A0 <= cp <= 0x25FF or 0x2460 <= cp <= 0x24FF:
            saw_pictograph = True
    # Require at least one real pictograph so a lone ZWJ / variation selector
    # doesn't register as a reaction.
    return saw_pictograph


def _reaction_rate_check(ip: str) -> bool:
    """Per-IP token-bucket-ish check, same shape as _chat_rate_check but with
    its own (more permissive) limits. True if allowed; records the timestamp."""
    now = time.time()
    cutoff = now - REACTION_RATE_WINDOW
    with reactions_lock:
        ts = [t for t in reaction_rate.get(ip, []) if t > cutoff]
        if len(ts) >= REACTION_RATE_MAX:
            reaction_rate[ip] = ts
            return False
        ts.append(now)
        reaction_rate[ip] = ts
        return True


def _custom_reactions_public(caller_sid: str | None = None, is_admin: bool = False) -> list[dict]:
    """The viewer-facing view of the custom set: id + image url + label, plus a
    per-caller `can_delete` flag. An admin can delete any; a friend can delete
    only ones they uploaded (matched on the opaque client `sid` tag). Viewers
    never match, so they get can_delete=False for everything."""
    with custom_reactions_lock:
        return [
            {
                "id": r["id"],
                "url": f"/reactions/img/{r['id']}",
                "label": r.get("label"),
                "can_delete": bool(is_admin or (caller_sid and r.get("sid") == caller_sid)),
            }
            for r in custom_reactions
        ]


@app.route("/reactions/send", methods=["POST"])
def api_reaction_send():
    """Append a reaction to the ephemeral ring. The reaction is either an
    `emoji` (ANY emoji — not limited to the quick set — gated by _is_emoji so a
    client can't inject arbitrary text/markup) or a `custom_id` referencing a
    validated custom reaction. `sid` is the same opaque client tag chat uses so
    a client can skip re-animating its own reaction. Per-IP rate-limited."""
    data = request.get_json(silent=True) or {}
    emoji = (data.get("emoji") or "").strip()
    custom_id = data.get("custom_id")
    rec = {"id": None, "ts": time.time(), "sid": (data.get("sid") or "").strip()[:32] or "anon"}
    if custom_id is not None:
        with custom_reactions_lock:
            cr = next((r for r in custom_reactions if r["id"] == custom_id), None)
        if cr is None:
            return jsonify({"error": "unknown reaction"}), 400
        rec["custom_id"] = cr["id"]
        rec["custom_url"] = f"/reactions/img/{cr['id']}"
    elif _is_emoji(emoji):
        rec["emoji"] = emoji
    else:
        return jsonify({"error": "not an emoji"}), 400
    if not _reaction_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    global reaction_next_id
    with reactions_lock:
        rec["id"] = reaction_next_id
        reaction_next_id += 1
        reactions.append(rec)
    return jsonify({"ok": True, "id": rec["id"]})


@app.route("/reactions/recent")
def api_reaction_recent():
    """Reactions with id > `since` AND newer than REACTION_RECENT_WINDOW. The
    recency filter keeps this an ephemeral feed — clients animate each reaction
    once and late joiners don't get a backlog. Also returns the built-in emoji
    set and the current custom set so the client can (re)build the bar."""
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    fresh = time.time() - REACTION_RECENT_WINDOW
    with reactions_lock:
        out = [r for r in reactions if r["id"] > since and r["ts"] >= fresh]
        max_id = reactions[-1]["id"] if reactions else since
        # Same dead-entry sweep as /chat/recent — keeps reaction_rate bounded
        # to IPs that actually reacted within the last window.
        cutoff = time.time() - REACTION_RATE_WINDOW
        for ip in [k for k, ts in reaction_rate.items() if not any(t > cutoff for t in ts)]:
            del reaction_rate[ip]
    caller_sid = request.args.get("sid")
    is_admin = _token_level(request.cookies.get(TOKEN_COOKIE)) == "admin"
    return jsonify({
        "reactions": out,
        "max_id": max_id,
        "emojis": list(REACTION_EMOJIS),
        "custom": _custom_reactions_public(caller_sid, is_admin),
    })


@app.route("/api/reactions/upload", methods=["POST"])
def api_reaction_upload():
    """A viewer imports their own reaction image. Multipart form: `image` file
    (+ optional `label`). Strictly validated — magic-byte image sniff, size
    cap, bounded total set, per-IP rate limit. The stored filename is derived
    from the content hash (deduped) so two viewers uploading the same image
    share one file. Returns the new reaction's id + url.

    Friend/admin only: viewers can tap existing custom reactions but can't add
    new ones to the shared set. The viewer page hides the import UI for them,
    but enforce it here too since the UI gate is bypassable."""
    if not _caller_can_control():
        return jsonify({"error": "friends only"}), 403
    ip = _client_ip()
    now = time.time()
    cutoff = now - CUSTOM_REACTION_UPLOAD_WINDOW
    with custom_reactions_lock:
        ts = [t for t in custom_reaction_upload_rate.get(ip, []) if t > cutoff]
        if len(ts) >= CUSTOM_REACTION_UPLOAD_MAX:
            custom_reaction_upload_rate[ip] = ts
            return jsonify({"error": "rate limited"}), 429
        if len(custom_reactions) >= CUSTOM_REACTION_MAX:
            return jsonify({"error": "reaction set is full"}), 409
    f = request.files.get("image")
    if f is None:
        return jsonify({"error": "image required"}), 400
    data = f.read(CUSTOM_REACTION_MAX_BYTES + 1)
    if len(data) > CUSTOM_REACTION_MAX_BYTES:
        return jsonify({"error": "image too large"}), 413
    if not data:
        return jsonify({"error": "empty image"}), 400
    sniff = _sniff_image(data)
    if sniff is None:
        return jsonify({"error": "unsupported image type"}), 415
    ext, mime = sniff
    digest = hashlib.sha256(data).hexdigest()[:24]
    fname = f"{digest}.{ext}"
    label = (request.form.get("label") or "").strip()[:24] or None
    REACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    fpath = REACTIONS_DIR / fname
    if not fpath.exists():
        fpath.write_bytes(data)
    global custom_reaction_next_id
    with custom_reactions_lock:
        # Record the upload timestamp now that it's accepted.
        ts.append(now)
        custom_reaction_upload_rate[ip] = ts
        # Dedup: if this exact image is already in the set, reuse it instead of
        # adding a second identical button.
        existing = next((r for r in custom_reactions if r["file"] == fname), None)
        if existing is not None:
            return jsonify({"ok": True, "id": existing["id"], "url": f"/reactions/img/{existing['id']}", "duplicate": True})
        entry = {
            "id": custom_reaction_next_id,
            "file": fname,
            "mime": mime,
            "label": label,
            "sid": (request.form.get("sid") or "").strip()[:32] or "anon",
            "ip": ip,
            "ts": now,
        }
        custom_reaction_next_id += 1
        custom_reactions.append(entry)
        _save_custom_reactions()
    return jsonify({"ok": True, "id": entry["id"], "url": f"/reactions/img/{entry['id']}"})


@app.route("/reactions/img/<int:cid>")
def api_reaction_image(cid: int):
    """Serve a custom reaction's image. Cached — the bytes never change for a
    given id (the file is content-hashed)."""
    with custom_reactions_lock:
        cr = next((r for r in custom_reactions if r["id"] == cid), None)
    if cr is None:
        abort(404)
    resp = send_from_directory(REACTIONS_DIR, cr["file"], mimetype=cr.get("mime"))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/admin/api/reactions", methods=["GET"])
def api_reactions_admin_list():
    """Full custom-reaction set with uploader context, for host moderation."""
    with custom_reactions_lock:
        return jsonify(list(custom_reactions))


@app.route("/admin/api/reactions/<int:cid>", methods=["DELETE"])
def api_reaction_delete(cid: int):
    """Remove a custom reaction. Drops the metadata entry and the backing file
    if no other entry references it (content-hash dedup can share a file)."""
    with custom_reactions_lock:
        cr = next((r for r in custom_reactions if r["id"] == cid), None)
        if cr is None:
            return jsonify({"error": "reaction not found"}), 404
        custom_reactions.remove(cr)
        still_used = any(r["file"] == cr["file"] for r in custom_reactions)
        _save_custom_reactions()
    if not still_used:
        try:
            (REACTIONS_DIR / cr["file"]).unlink(missing_ok=True)
        except Exception as e:
            print(f"reactions: failed to unlink {cr['file']}: {e}", file=sys.stderr)
    return jsonify({"ok": True})


@app.route("/api/reactions/<int:cid>", methods=["DELETE"])
def api_reaction_delete_own(cid: int):
    """Viewer-page delete: a friend prunes a custom reaction they uploaded (sid
    match), an admin prunes any. Lets the friend group manage the shared set
    without the admin surface — e.g. to free a slot when it hits the cap.
    Viewers (no control tier) can't delete. Same file-unlink dedup as the admin
    endpoint above."""
    level = _token_level(request.cookies.get(TOKEN_COOKIE))
    if level not in CONTROL_LEVELS:
        return jsonify({"error": "friends only"}), 403
    caller_sid = (request.args.get("sid") or "").strip()
    with custom_reactions_lock:
        cr = next((r for r in custom_reactions if r["id"] == cid), None)
        if cr is None:
            return jsonify({"error": "reaction not found"}), 404
        if level != "admin" and cr.get("sid") != caller_sid:
            return jsonify({"error": "not yours"}), 403
        custom_reactions.remove(cr)
        still_used = any(r["file"] == cr["file"] for r in custom_reactions)
        _save_custom_reactions()
    if not still_used:
        try:
            (REACTIONS_DIR / cr["file"]).unlink(missing_ok=True)
        except Exception as e:
            print(f"reactions: failed to unlink {cr['file']}: {e}", file=sys.stderr)
    return jsonify({"ok": True})


def _request_rate_check(ip: str) -> bool:
    """Per-IP token-bucket-ish check, same shape as _chat_rate_check. True if
    this IP can file one more request within the rolling window; records the
    timestamp on success. Pruned lazily."""
    now = time.time()
    cutoff = now - REQUEST_RATE_WINDOW
    with requests_lock:
        ts = [t for t in request_rate.get(ip, []) if t > cutoff]
        if len(ts) >= REQUEST_RATE_MAX:
            request_rate[ip] = ts
            return False
        ts.append(now)
        request_rate[ip] = ts
        return True


def _voter_key(data: dict | None = None) -> str:
    """Stable per-viewer identity for request votes: the client `sid` tag when
    supplied (survives reloads, distinguishes tabs), else the IP. Auth-free —
    requests are a low-stakes viewer voice, not a security boundary."""
    data = data or {}
    sid = (data.get("sid") or request.args.get("sid") or "").strip()[:32]
    return ("sid:" + sid) if sid else ("ip:" + _client_ip())


def _request_votes(r: dict) -> list[str]:
    """Voter list for a request, tolerating legacy entries saved before votes
    existed (seed them with the original requester's IP so they count as 1)."""
    v = r.get("voters")
    if v is None:
        v = ["ip:" + r["ip"]] if r.get("ip") else []
        r["voters"] = v
    return v


def _public_request(r: dict, key: str) -> dict:
    """Viewer-facing view of a request: no IP/path leakage, just what the list
    needs — title, who asked, the tally, and whether *this* caller voted."""
    voters = _request_votes(r)
    return {
        "id": r["id"],
        "title": r.get("title") or Path(r["path"]).name,
        "requester": r.get("requester"),
        "votes": len(voters),
        "voted": key in voters,
        "ts": r.get("ts", 0),
    }


def _do_approve_request(rid: int):
    """Pop a pending request and append it to the playlist as a real file item.
    Shared by the admin and friend-control approve routes."""
    with requests_lock:
        req = next((r for r in media_requests if r["id"] == rid), None)
        if req is None:
            return jsonify({"error": "request not found"}), 404
        media_requests.remove(req)
        _save_requests()
    path = req["path"]
    try:
        full = _safe_resolve(path, must_be_file=True)
    except Exception:
        # The file vanished between request and approval — the request is gone
        # either way, so report it rather than leaving a dangling entry.
        return jsonify({"error": "file not found"}), 400
    item = {
        "type": "file", "ref": path, "title": Path(path).name,
        "duration": _probe_duration(full), "is_live": False,
        "subtitle_idx": _pick_default_subtitle(str(full)),
    }
    with playlist_lock:
        playlist.append(item)
        _save_playlist()
        length = len(playlist)
    return jsonify({"ok": True, "queue_length": length})


def _do_deny_request(rid: int):
    """Drop a pending request without touching the playlist. Shared by the
    admin and friend-control deny routes."""
    with requests_lock:
        req = next((r for r in media_requests if r["id"] == rid), None)
        if req is None:
            return jsonify({"error": "request not found"}), 404
        media_requests.remove(req)
        _save_requests()
    return jsonify({"ok": True})


@app.route("/api/requests")
def api_requests_public():
    """Viewer-facing request list with vote tallies, most-wanted first. Anyone
    with viewer access sees it; `can_manage` tells the client whether to show
    the friend/admin approve+deny affordances."""
    key = _voter_key()
    with requests_lock:
        _expire_old_requests()
        items = [_public_request(r, key) for r in media_requests]
    items.sort(key=lambda x: (-x["votes"], x["ts"]))
    return jsonify({"requests": items, "can_manage": _caller_can_control()})


@app.route("/api/request/<int:rid>/vote", methods=["POST"])
def api_request_vote(rid: int):
    """Toggle the caller's vote on a pending request. Viewer-accessible — this
    is the viewers' lever; acting on the result (approve) stays friend/admin."""
    data = request.get_json(silent=True) or {}
    if not _request_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    key = _voter_key(data)
    with requests_lock:
        req = next((r for r in media_requests if r["id"] == rid), None)
        if req is None:
            return jsonify({"error": "request not found"}), 404
        voters = _request_votes(req)
        if key in voters:
            voters.remove(key)
        else:
            voters.append(key)
        _save_requests()
        votes, voted = len(voters), key in voters
    return jsonify({"ok": True, "votes": votes, "voted": voted})


@app.route("/api/control/requests/<int:rid>/approve", methods=["POST"])
def api_request_approve_friend(rid: int):
    """Friend/admin approves a request → queues it. Gated to control tiers by
    the /api/control/ branch of _gate_viewer_routes."""
    return _do_approve_request(rid)


@app.route("/api/control/requests/<int:rid>", methods=["DELETE"])
def api_request_deny_friend(rid: int):
    """Friend/admin denies a request. Control-tier gated like approve."""
    return _do_deny_request(rid)


@app.route("/api/library/browse")
def api_library_browse():
    """Viewer-facing library browse — same payload as the control-gated
    /admin/api/browse, but reachable by a plain viewer token (the viewer gate
    applies since this is neither /admin nor /api/control). Read-only: viewers
    use it to find something to request, not to play."""
    return jsonify(_viewer_list_dir(request.args.get("path", "")))


@app.route("/api/library/search")
def api_library_search():
    """Viewer-facing recursive search, same shape as /admin/api/search."""
    items, truncated = _search_viewer_library(request.args.get("q", "").strip())
    return jsonify({"results": items, "truncated": truncated})


@app.route("/api/request", methods=["POST"])
def api_request_add():
    """A viewer asks the host to queue a file. Validates the path, rate-limits
    per IP, and appends to the pending list — it does NOT touch the playlist
    (the host approves via /admin/api/requests/<id>/approve). Duplicate pending
    requests for the same path are folded into a no-op so a double-tap doesn't
    stack the list."""
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path required"}), 400
    if not _viewer_file_allowed(path):
        return jsonify({"error": "file not found"}), 400
    try:
        _safe_resolve(path, must_be_file=True)
    except Exception:
        return jsonify({"error": "file not found"}), 400
    if not _request_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    requester = _clean_chat_name(data.get("name"))
    key = _voter_key(data)
    global request_next_id
    with requests_lock:
        # Sweep ghosts before dedup so an expired request for the same path
        # doesn't look like a live duplicate.
        _expire_old_requests()
        existing = next((r for r in media_requests if r["path"] == path), None)
        if existing:
            # Re-requesting an already-pending title is an upvote, not a no-op —
            # that's how a second viewer's interest registers.
            voters = _request_votes(existing)
            if key not in voters:
                voters.append(key)
                _save_requests()
            return jsonify({"ok": True, "duplicate": True, "request": _public_request(existing, key)})
        req = {
            "id": request_next_id,
            "ts": time.time(),
            "path": path,
            "title": Path(path).name,
            "requester": requester,
            "ip": _client_ip(),
            "voters": [key],  # the requester is the first vote
        }
        request_next_id += 1
        media_requests.append(req)
        _save_requests()
    return jsonify({"ok": True, "request": _public_request(req, key)})


def _report_rate_check(ip: str) -> bool:
    """Per-IP token-bucket-ish check, same shape as _request_rate_check but on
    its own (tighter) window. True if allowed; records the timestamp."""
    now = time.time()
    cutoff = now - REPORT_RATE_WINDOW
    with reports_lock:
        ts = [t for t in report_rate.get(ip, []) if t > cutoff]
        if len(ts) >= REPORT_RATE_MAX:
            report_rate[ip] = ts
            return False
        ts.append(now)
        report_rate[ip] = ts
        return True


@app.route("/api/report", methods=["POST"])
def api_report_add():
    """A viewer files an issue report. Rate-limited per IP. The server captures
    authoritative context (timestamp, IP, viewer token label, stream-health
    snapshot) and folds in the client-supplied fields the browser knows but the
    server can't see (short message, what the player thought it was showing,
    playback position, user-agent, playback mode, mute/fullscreen). Persisted
    to /data/reports.json in an agent-readable shape — `client` (what the
    viewer saw) is kept separate from `stream_health` (server truth) so a
    triage agent can spot drift between them. Never touches playback."""
    if not _report_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()[:1000]
    client = {
        "title": (str(data.get("title") or "").strip() or None),
        "path": (str(data.get("path") or "").strip() or None),
        "position_seconds": _coerce_float(data.get("position_seconds")),
        "playback_mode": (str(data.get("mode") or "").strip()[:40] or None),
        "muted": bool(data["muted"]) if "muted" in data else None,
        "fullscreen": bool(data["fullscreen"]) if "fullscreen" in data else None,
    }
    token = request.cookies.get(TOKEN_COOKIE)
    report = {
        "id": None,  # assigned under the lock
        "ts": time.time(),
        "ts_iso": _iso_now(),
        "status": "new",
        "message": msg or None,
        "viewer_label": _token_label(token),
        "user_agent": request.headers.get("User-Agent", "")[:400],
        "ip": _client_ip(),
        "client": client,
        "stream_health": _stream_health_snapshot(),
        # Reserved for a watcher agent to write triage notes back into.
        "triage": None,
    }
    global report_next_id
    with reports_lock:
        report["id"] = report_next_id
        report_next_id += 1
        reports.append(report)
        if len(reports) > REPORT_MAX:
            del reports[: len(reports) - REPORT_MAX]
        _save_reports()
    return jsonify({"ok": True, "id": report["id"]})


@app.route("/api/queue")
def api_queue_public():
    """Read-only queue for viewers: just enough to show "up next" (title,
    source type, duration). No control — adding/removing/reordering stays on
    the admin/friend control surface. Token-gated by the viewer route gate.
    `ref` is included for FILE items so the viewer UI can render the same
    Sonarr/Radarr poster thumbs the admin and friend surfaces show — URL /
    yt-dlp items don't have a useful ref for cover art and pass None."""
    with playlist_lock:
        items = [
            {
                "title": it.get("title"),
                "type": it.get("type"),
                "duration": it.get("duration"),
                "is_live": it.get("is_live", False),
                "ref": it.get("ref") if it.get("type") == "file" else None,
            }
            for it in playlist
        ]
    return jsonify({"queue": items})


@app.route("/")
def viewer_page():
    return send_from_directory("static", "viewer.html")


@app.route("/controls")
def controls_page():
    # Friend control surface. Serves the same admin.html as /admin but the
    # page detects it was loaded here (not /admin), points its API calls at
    # /api/control/* instead of /admin/api/*, and hides the host-only panels
    # (invites, settings, viewer list). Access is gated to friend+admin tokens
    # by _gate_viewer_routes; reaching here means the cookie already checked out.
    return send_from_directory("static", "admin.html")


@app.route("/admin")
@app.route("/admin/")
def admin_page():
    resp = send_from_directory("static", "admin.html")
    # Mint+attach a viewer cookie on every admin page load. The admin path is
    # gated by Traefik basicauth, so reaching here is trusted; without this the
    # admin had to also juggle a separate viewer invite token (and any browser-
    # side cookie loss after the basicauth round-trip locked them out of /).
    with tokens_lock:
        if not any(t.get("id") == ADMIN_TOKEN_ID for t in tokens):
            tokens.append({
                "id": ADMIN_TOKEN_ID,
                "label": "admin",
                "created": time.time(),
            })
            _save_tokens()
    resp.set_cookie(
        TOKEN_COOKIE, ADMIN_TOKEN_ID,
        max_age=TOKEN_COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


@app.route("/admin/api/browse")
@app.route("/api/control/browse")
def api_browse():
    return jsonify(_list_dir(request.args.get("path", "")))


@app.route("/admin/api/search")
@app.route("/api/control/search")
def api_search():
    """Recursive filename search across the whole library (cached scan).
    Returns file entries in the same shape as /browse so the UI renders them
    with the existing list renderer."""
    items, truncated = _search_library(request.args.get("q", "").strip())
    return jsonify({"results": items, "truncated": truncated})


@app.route("/admin/api/play", methods=["POST"])
@app.route("/api/control/play", methods=["POST"])
def api_play():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path required"}), 400
    start = float(data.get("start_seconds") or 0)
    # Subtitle handling: missing key → auto-pick English; explicit JSON
    # `null` → no subs (passes through state intact). Anything else gets
    # coerced to int.
    if "subtitle_idx" in data:
        raw = data["subtitle_idx"]
        sub_idx = None if raw is None else int(raw)
    else:
        full = _safe_resolve(path, must_be_file=True)
        sub_idx = _pick_default_subtitle(str(full))
    source = {
        "type": "file",
        "ref": path,
        "title": Path(path).name,
        "duration": None,
        "is_live": False,
        "subtitle_idx": sub_idx,
    }
    _start_stream(source, start_seconds=start)
    return jsonify({
        "ok": True, "path": path, "start_seconds": start,
        "subtitle_idx": sub_idx,
    })


@app.route("/admin/api/play_url", methods=["POST"])
@app.route("/api/control/play_url", methods=["POST"])
def api_play_url():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        items = _expand_url(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    first, rest = items[0], items[1:]
    if rest:
        # Playlist — prepend the rest of the entries onto the queue so they auto-advance.
        with playlist_lock:
            playlist[0:0] = rest
            _save_playlist()
    _start_stream(first, start_seconds=0.0)
    # Re-read updated source state (might have refreshed title/duration/is_live).
    src = current_source or first
    return jsonify({
        "ok": True,
        "title": src.get("title"),
        "is_live": src.get("is_live"),
        "duration": src.get("duration"),
        "queued": len(rest),
    })


@app.route("/admin/api/tokens", methods=["GET"])
def api_list_tokens():
    with tokens_lock:
        return jsonify([
            {
                "id": t["id"], "label": t["label"], "created": t.get("created"),
                "level": t.get("level", "viewer"),
                "last_seen": t.get("last_seen"),
            }
            for t in tokens
            if t.get("id") != ADMIN_TOKEN_ID
        ])


@app.route("/admin/api/tokens", methods=["POST"])
def api_create_token():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label required"}), 400
    level = (data.get("level") or "viewer").strip()
    if level not in TOKEN_LEVELS:
        return jsonify({"error": f"level must be one of {TOKEN_LEVELS}"}), 400
    new = {
        "id": secrets.token_urlsafe(12), "label": label,
        "created": time.time(), "level": level,
    }
    with tokens_lock:
        tokens.append(new)
        _save_tokens()
    return jsonify(new)


@app.route("/admin/api/tokens/<tid>", methods=["DELETE"])
def api_delete_token(tid: str):
    with tokens_lock:
        before = len(tokens)
        tokens[:] = [t for t in tokens if t["id"] != tid]
        if len(tokens) < before:
            _save_tokens()
    return jsonify({"ok": True})


@app.route("/admin/api/viewers", methods=["GET"])
def api_viewers():
    """Live list of currently-active viewers, with cached geo + token label.
    Admin-only by virtue of the /admin path prefix being basicauth-gated by Traefik."""
    cutoff = time.time() - VIEWER_TIMEOUT
    out = []
    with viewers_lock:
        active = [(ip, ts, viewer_labels.get(ip)) for ip, ts in viewers.items() if ts >= cutoff]
    # Build geo lookup outside viewers_lock to avoid nesting locks on the hot path.
    for ip, ts, label in active:
        with _geo_cache_lock:
            loc = _geo_cache.get(ip, "?")
        out.append({"ip": ip, "loc": loc, "last_seen": ts, "who": label})
    out.sort(key=lambda v: -v["last_seen"])
    return jsonify(out)


@app.route("/admin/api/perf", methods=["GET"])
def api_perf():
    """Cheap observability hook: ffmpeg PID + uptime, segment count on disk,
    composer/run state, viewer count. No external probes — everything here is
    O(small) so a polling admin dashboard won't add latency to the hot path."""
    with state_lock:
        proc = current_proc
        run_id = active_run_id
        finished = sorted(finished_run_ids)
        source = current_source
        start_time = current_start_time
        paused = current_paused
        pre_proc = preroll_proc
        pre_run_id = preroll_run_id
        pre_title = (preroll_source or {}).get("title")
    pid = proc.pid if proc else None
    alive = bool(proc and proc.poll() is None)
    uptime = (time.time() - start_time) if (alive and start_time > 0) else None
    pre_pid = pre_proc.pid if pre_proc else None
    pre_alive = bool(pre_proc and pre_proc.poll() is None)
    segments = 0
    runs_on_disk = 0
    try:
        for d in RUN_DIR_BASE.iterdir():
            if not d.is_dir():
                continue
            runs_on_disk += 1
            for f in d.iterdir():
                if f.suffix == ".m4s":
                    segments += 1
    except FileNotFoundError:
        pass
    return jsonify({
        "ffmpeg_pid": pid,
        "ffmpeg_alive": alive,
        "ffmpeg_uptime_seconds": uptime,
        "active_run_id": run_id,
        "finished_run_ids": finished,
        "runs_on_disk": runs_on_disk,
        "segments_on_disk": segments,
        "viewers": _viewer_count(),
        "paused": paused,
        "title": (source or {}).get("title"),
        "preroll": {
            "ffmpeg_pid": pre_pid,
            "ffmpeg_alive": pre_alive,
            "run_id": pre_run_id,
            "title": pre_title,
        },
    })


@app.route("/admin/api/viewers/history", methods=["GET"])
def api_viewers_history():
    """Return the last N viewer-log entries (JSONL on disk; survives restarts)."""
    try:
        if not VIEWER_LOG_FILE.exists():
            return jsonify([])
        with VIEWER_LOG_FILE.open() as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    out = []
    # Tail the last 200 lines — enough for a session of browsing without
    # hitting browser-side rendering limits if the log gets long.
    for line in lines[-200:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return jsonify(out)


@app.route("/admin/api/queue", methods=["GET"])
@app.route("/api/control/queue", methods=["GET"])
def api_queue_list():
    with playlist_lock:
        return jsonify(list(playlist))


@app.route("/admin/api/recent", methods=["GET"])
@app.route("/api/control/recent", methods=["GET"])
def api_recent_list():
    with recent_lock:
        return jsonify(list(recent_items))


@app.route("/admin/api/recent/<int:idx>/queue", methods=["POST"])
@app.route("/api/control/recent/<int:idx>/queue", methods=["POST"])
def api_recent_requeue(idx: int):
    with recent_lock:
        if not (0 <= idx < len(recent_items)):
            return jsonify({"error": "index out of range"}), 400
        source = dict(recent_items[idx].get("source") or {})
    if source.get("type") == "file":
        ref = source.get("ref")
        if not ref:
            return jsonify({"error": "missing file ref"}), 400
        try:
            full = _safe_resolve(ref, must_be_file=True)
        except Exception:
            return jsonify({"error": "file not found"}), 400
        item = {
            "type": "file",
            "ref": ref,
            "title": source.get("title") or Path(ref).name,
            "duration": source.get("duration") or _probe_duration(full),
            "is_live": False,
        }
        if "subtitle_idx" in source:
            item["subtitle_idx"] = source.get("subtitle_idx")
        else:
            item["subtitle_idx"] = _pick_default_subtitle(str(full))
        items = [item]
    elif source.get("type") == "url":
        ref = (source.get("ref") or "").strip()
        if not ref:
            return jsonify({"error": "missing URL"}), 400
        # Keep requeue fast: store the original URL source back into the
        # playlist and let _start_stream resolve fresh signed stream URLs when
        # playback reaches it.
        items = [{
            "type": "url",
            "ref": ref,
            "title": source.get("title") or ref,
            "duration": source.get("duration"),
            "is_live": bool(source.get("is_live")),
        }]
    else:
        return jsonify({"error": "unsupported recent source"}), 400
    with playlist_lock:
        playlist.extend(items)
        _save_playlist()
        length = len(playlist)
    return jsonify({
        "ok": True,
        "added": len(items),
        "first_title": items[0].get("title"),
        "queue_length": length,
    })


@app.route("/admin/api/queue", methods=["POST"])
@app.route("/api/control/queue", methods=["POST"])
def api_queue_add():
    data = request.get_json(silent=True) or {}
    t = data.get("type")
    if t == "file":
        path = data.get("path")
        if not path:
            return jsonify({"error": "path required"}), 400
        try:
            full = _safe_resolve(path, must_be_file=True)
        except Exception:
            return jsonify({"error": "file not found"}), 400
        # Subtitle pick at enqueue time, same convention as /admin/api/play:
        # omitted → auto-pick English; explicit JSON null → no subs.
        if "subtitle_idx" in data:
            raw = data["subtitle_idx"]
            sub_idx = None if raw is None else int(raw)
        else:
            sub_idx = _pick_default_subtitle(str(full))
        # Probe duration at enqueue time (cheap — ffprobe ~10-50ms per file)
        # so the queue UI can show a duration badge without round-tripping
        # later. None on probe failure stays harmless.
        items = [{
            "type": "file", "ref": path, "title": Path(path).name,
            "duration": _probe_duration(full), "is_live": False,
            "subtitle_idx": sub_idx,
        }]
    elif t == "url":
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url required"}), 400
        try:
            items = _expand_url(url)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    else:
        return jsonify({"error": "type must be 'file' or 'url'"}), 400
    with playlist_lock:
        playlist.extend(items)
        _save_playlist()
        length = len(playlist)
    return jsonify({
        "ok": True,
        "added": len(items),
        "first_title": items[0]["title"],
        "queue_length": length,
    })


@app.route("/admin/api/requests", methods=["GET"])
def api_requests_list():
    """Pending viewer requests, oldest first (insertion order). Host-only —
    only the /admin path serves it (no /api/control alias), so friends on
    /controls don't see or act on the queue-request backlog. Sweeps
    expired entries lazily on each view (REQUEST_TTL_SECS)."""
    with requests_lock:
        _expire_old_requests()
        return jsonify(list(media_requests))


@app.route("/admin/api/requests", methods=["DELETE"])
def api_requests_clear():
    """Clear all pending requests in one shot. The per-entry deny endpoint
    still exists; this is the "I'm not adopting any of these" admin button."""
    with requests_lock:
        count = len(media_requests)
        media_requests.clear()
        _save_requests()
    return jsonify({"ok": True, "cleared": count})


@app.route("/admin/api/requests/<int:rid>/approve", methods=["POST"])
def api_request_approve(rid: int):
    """Approve a pending request → queue it. Shares _do_approve_request with the
    friend-control route; idempotent against a missing id (404)."""
    return _do_approve_request(rid)


@app.route("/admin/api/requests/<int:rid>", methods=["DELETE"])
def api_request_deny(rid: int):
    """Deny a pending request: drop it without touching the playlist."""
    return _do_deny_request(rid)


@app.route("/admin/api/reports", methods=["GET"])
def api_reports_list():
    """Viewer bug reports, newest first. Host-only (no /api/control alias) — the
    report queue is an admin/host triage surface, not something friends act on.
    This is also the endpoint a Jetstream watcher agent polls."""
    with reports_lock:
        return jsonify(list(reversed(reports)))


@app.route("/admin/api/reports", methods=["DELETE"])
def api_reports_clear():
    """Clear the whole report pile — the "all triaged, wipe it" button."""
    with reports_lock:
        count = len(reports)
        reports.clear()
        _save_reports()
    return jsonify({"ok": True, "cleared": count})


@app.route("/admin/api/reports/<int:rid>/handled", methods=["POST"])
def api_report_handled(rid: int):
    """Flip a report's status. Defaults to "handled"; pass {"unhandle": true}
    to reopen one. 404 on a missing id."""
    data = request.get_json(silent=True) or {}
    new_status = "new" if data.get("unhandle") else "handled"
    with reports_lock:
        rep = next((r for r in reports if r["id"] == rid), None)
        if rep is None:
            return jsonify({"error": "report not found"}), 404
        rep["status"] = new_status
        _save_reports()
    return jsonify({"ok": True, "status": new_status})


@app.route("/admin/api/reports/<int:rid>/triage", methods=["POST"])
def api_report_triage(rid: int):
    """Write a triage verdict into a report's reserved `triage` field — the safe
    write-back path for the Jetstream watcher agent. The watcher must go through
    Flask (under reports_lock) rather than editing reports.json directly:
    Flask keeps `reports` in memory and rewrites the whole file on every change,
    so a direct file edit would be clobbered on the next save. Body:
    {"triage": <value>}; pass null to clear. Optionally also flips status to
    "handled" with {"handled": true}. 404 on a missing id."""
    data = request.get_json(silent=True) or {}
    if "triage" not in data:
        return jsonify({"error": "triage field required"}), 400
    with reports_lock:
        rep = next((r for r in reports if r["id"] == rid), None)
        if rep is None:
            return jsonify({"error": "report not found"}), 404
        rep["triage"] = data["triage"]
        if data.get("handled"):
            rep["status"] = "handled"
        _save_reports()
        status = rep["status"]
    return jsonify({"ok": True, "status": status})


@app.route("/admin/api/reports/<int:rid>", methods=["DELETE"])
def api_report_delete(rid: int):
    """Drop a single report."""
    with reports_lock:
        rep = next((r for r in reports if r["id"] == rid), None)
        if rep is None:
            return jsonify({"error": "report not found"}), 404
        reports.remove(rep)
        _save_reports()
    return jsonify({"ok": True})


@app.route("/admin/api/queue/<int:idx>", methods=["DELETE"])
@app.route("/api/control/queue/<int:idx>", methods=["DELETE"])
def api_queue_remove(idx: int):
    with playlist_lock:
        if not (0 <= idx < len(playlist)):
            return jsonify({"error": "index out of range"}), 400
        playlist.pop(idx)
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/queue/clear", methods=["POST"])
@app.route("/api/control/queue/clear", methods=["POST"])
def api_queue_clear():
    with playlist_lock:
        playlist.clear()
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/queue/shuffle", methods=["POST"])
@app.route("/api/control/queue/shuffle", methods=["POST"])
def api_queue_shuffle():
    with playlist_lock:
        random.shuffle(playlist)
        _save_playlist()
        length = len(playlist)
    return jsonify({"ok": True, "queue_length": length})


@app.route("/admin/api/settings", methods=["GET"])
def api_settings_get():
    with settings_lock:
        out = dict(settings)
    if out.get("stream_quality") not in STREAM_QUALITY_PRESETS:
        out["stream_quality"] = "default"
    out["stream_quality_options"] = [
        {"key": key, **preset}
        for key, preset in STREAM_QUALITY_PRESETS.items()
    ]
    return jsonify(out)


BOOL_SETTINGS = {"viewer_public", "auto_fill"}
SETTABLE_SETTINGS = BOOL_SETTINGS | {"stream_quality"}


@app.route("/admin/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(silent=True) or {}
    keys = SETTABLE_SETTINGS & data.keys()
    if not keys:
        return jsonify({"error": f"one of {sorted(SETTABLE_SETTINGS)} required"}), 400
    with settings_lock:
        for k in keys:
            if k in BOOL_SETTINGS:
                settings[k] = bool(data[k])
            elif k == "stream_quality":
                quality = str(data[k])
                if quality not in STREAM_QUALITY_PRESETS:
                    return jsonify({"error": "invalid stream_quality"}), 400
                settings[k] = quality
        _save_settings()
        out = dict(settings)
    out["stream_quality_options"] = [
        {"key": key, **preset}
        for key, preset in STREAM_QUALITY_PRESETS.items()
    ]
    return jsonify(out)


@app.route("/admin/api/queue/<int:idx>/move", methods=["POST"])
@app.route("/api/control/queue/<int:idx>/move", methods=["POST"])
def api_queue_move(idx: int):
    """Move a queue item. Supports legacy {direction: "up"|"down"} for the
    arrow buttons and {to: N} for drag-drop reordering (where N is the new
    index, with the item removed first). Out-of-range targets clamp."""
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    to = data.get("to")
    with playlist_lock:
        n = len(playlist)
        if not (0 <= idx < n):
            return jsonify({"error": "index out of range"}), 400
        if direction == "up" and idx > 0:
            playlist[idx - 1], playlist[idx] = playlist[idx], playlist[idx - 1]
        elif direction == "down" and idx < n - 1:
            playlist[idx + 1], playlist[idx] = playlist[idx], playlist[idx + 1]
        elif to is not None:
            try:
                to_i = int(to)
            except (TypeError, ValueError):
                return jsonify({"error": "to must be an integer"}), 400
            # Clamp to a valid post-removal index. After removing `idx`, the
            # list has n-1 slots, so the destination is in [0, n-1].
            to_i = max(0, min(n - 1, to_i))
            if to_i == idx:
                return jsonify({"ok": True})
            item = playlist.pop(idx)
            playlist.insert(to_i, item)
        else:
            return jsonify({"ok": True})
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/seek", methods=["POST"])
@app.route("/api/control/seek", methods=["POST"])
def api_seek():
    data = request.get_json(silent=True) or {}
    if current_source is None:
        return jsonify({"error": "no stream running"}), 400
    if current_source.get("is_live"):
        return jsonify({"error": "can't seek a live stream"}), 400
    if "to_seconds" in data:
        target = float(data["to_seconds"])
    elif "delta_seconds" in data:
        pos = _current_position() or 0
        target = pos + float(data["delta_seconds"])
    else:
        return jsonify({"error": "to_seconds or delta_seconds required"}), 400
    target = max(0.0, target)
    _start_stream(current_source, start_seconds=target)
    return jsonify({"ok": True, "start_seconds": target})


@app.route("/admin/api/stop", methods=["POST"])
@app.route("/api/control/stop", methods=["POST"])
def api_stop():
    with state_lock:
        _stop_locked()
    # _cleanup_hls acquires its own locks — must call outside state_lock.
    _cleanup_hls()
    _clear_state()
    return jsonify({"ok": True})


def _skip_locked():
    """Terminate ffmpeg + retire the run so the watcher advances to the next
    item (queue first, then auto_fill). Caller must hold state_lock."""
    global current_paused, active_run_id
    _terminate_proc_locked()
    # Retire the run so the next _start_stream draws an EXT-X-DISCONTINUITY
    # boundary in the master.
    if active_run_id is not None:
        finished_run_ids.add(active_run_id)
        active_run_id = None
    # Un-pause so the watcher's `if paused: continue` doesn't block advance.
    current_paused = False


@app.route("/admin/api/skip", methods=["POST"])
@app.route("/api/control/skip", methods=["POST"])
def api_skip():
    """Skip to the next item: terminate ffmpeg without clearing source state and
    let the watcher pick the next thing (queue first, then auto_fill if on).
    Acquires state_lock and delegates the teardown to _skip_locked."""
    with state_lock:
        if current_source is None:
            return jsonify({"error": "nothing playing"}), 400
        _skip_locked()
    return jsonify({"ok": True})


def _skip_threshold(active: int) -> int:
    """Votes needed to skip: a strict majority of active viewers (IP-based,
    matching _viewer_count). 1 viewer → 1, 2 → 2, 3 → 2, 4 → 3, 5 → 3."""
    return active // 2 + 1


@app.route("/api/vote_skip", methods=["POST"])
def api_vote_skip():
    """Toggle the calling viewer's skip vote for the current item. When votes
    reach a majority of active viewers, the item is skipped and votes reset.
    Keyed by client IP (same basis as the viewer count)."""
    with state_lock:
        playing = current_source is not None
    if not playing:
        return jsonify({"error": "nothing playing"}), 400
    ip = _client_ip()
    active = _viewer_count()
    needed = _skip_threshold(active)
    with skip_votes_lock:
        if ip in skip_votes:
            skip_votes.discard(ip)
            voted = False
        else:
            skip_votes.add(ip)
            voted = True
        votes = len(skip_votes)
        passed = voted and votes >= needed
        if passed:
            skip_votes.clear()
    if passed:
        with state_lock:
            if current_source is not None:
                _skip_locked()
    return jsonify({"votes": 0 if passed else votes, "needed": needed,
                    "voted": voted, "skipped": passed})


@app.route("/admin/api/pause", methods=["POST"])
@app.route("/api/control/pause", methods=["POST"])
def api_pause():
    global current_paused, paused_position, active_run_id
    with state_lock:
        if current_source is None:
            return jsonify({"error": "no stream loaded"}), 400
        if current_source.get("is_live"):
            return jsonify({"error": "can't pause a live stream"}), 400
        if current_paused:
            return jsonify({"ok": True, "paused": True, "position_seconds": paused_position})
        pos = current_start_offset + max(0.0, time.time() - current_start_time)
        duration = current_source.get("duration")
        if duration:
            pos = min(pos, duration)
        paused_position = pos
        # Kill ffmpeg outright. SIGSTOP doesn't work cleanly with -re — wall-clock advances
        # while the process is suspended, then on SIGCONT ffmpeg burst-encodes to "catch up"
        # and rapidly burns through the rest of the file.
        _terminate_proc_locked()
        # Retire the run. Its segments stay in /hls/run/<id>/ and the composer
        # keeps them in the master playlist, so viewers' players can sit on
        # the existing live edge instead of seeing a 404'd manifest. On resume
        # _start_stream creates a new run with an EXT-X-DISCONTINUITY between.
        if active_run_id is not None:
            finished_run_ids.add(active_run_id)
            active_run_id = None
        current_paused = True
        snapshot = _state_snapshot_locked()
    _save_state(snapshot)
    return jsonify({"ok": True, "paused": True, "position_seconds": paused_position})


@app.route("/admin/api/resume", methods=["POST"])
@app.route("/api/control/resume", methods=["POST"])
def api_resume():
    with state_lock:
        if current_source is None or not current_paused:
            return jsonify({"error": "not paused"}), 400
        source_to_resume = current_source
        position = paused_position
    # Release lock before _start_stream — it does its own locking and may run yt-dlp for URLs.
    _start_stream(source_to_resume, start_seconds=position)
    return jsonify({"ok": True, "paused": False, "start_seconds": position})


@app.route("/api/status")
def api_status():
    # Count unhandled reports under its own lock first, so we don't nest it
    # inside state_lock. Bounded by REPORT_MAX, so the scan is trivial.
    with reports_lock:
        reports_new = sum(1 for r in reports if r.get("status") == "new")
    with state_lock:
        running = current_proc is not None and current_proc.poll() is None
        active = current_source is not None  # a source is loaded (running OR paused)
        playlist_ready = (HLS_DIR / "stream.m3u8").exists()
        if running:
            position = _current_position(running=True)
        elif current_paused:
            position = paused_position
        else:
            position = None
        src = current_source if active else None
        # Compute these once — viewer count is an O(viewers) scan + lock and
        # we'd otherwise call it twice (once for the field, once inside
        # _skip_threshold); token level is an O(tokens) scan and was being
        # walked twice (once via _caller_can_control, once for the `level`
        # field).
        viewer_count = _viewer_count()
        caller_level = _token_level(request.cookies.get(TOKEN_COOKIE))
        caller_user = _session_user()
        return jsonify({
            "playing": active,
            "paused": current_paused,
            "ready": active and playlist_ready,
            "path": (src or {}).get("ref"),
            "title": (src or {}).get("title"),
            "source_type": (src or {}).get("type"),
            "is_live": (src or {}).get("is_live", False),
            "position_seconds": position,
            "duration_seconds": (src or {}).get("duration"),
            "viewers": viewer_count,
            # For client-side sync: lets clients correct for clock skew so
            # the "play whatever PDT == server_now − 2s" target lands at the
            # same moment on every device.
            "server_unix": time.time(),
            # Permission hints for the viewer page: show a "Controls" link
            # when this token can drive playback. is_admin distinguishes the
            # host (full control surface) from a friend.
            "can_control": caller_level in CONTROL_LEVELS,
            "level": caller_level,
            # v2 account tier: username when a js_user session is present,
            # else None. Drives the header's Library / Sign-in link — without
            # it an account holder watching via an invite link has no way to
            # discover /library.
            "user": (caller_user or {}).get("username"),
            # Vote-to-skip tally for the current item (viewer-facing button).
            "skip_votes": len(skip_votes),
            "skip_needed": _skip_threshold(viewer_count),
            # Pending viewer requests — lets the admin UI badge the count
            # without polling /admin/api/requests when nothing's waiting.
            "requests_pending": len(media_requests),
            # Unhandled bug reports — same idea, badges the admin reports panel.
            "reports_new": reports_new,
            # True once this viewer has hit the continuous-watch cap; the page
            # surfaces a "refresh to keep watching" notice instead of a silent
            # stall. Matches the same check the /hls auth gate enforces.
            "session_expired": _viewer_session_expired(_client_ip()),
        })


# Scene-release tokens that mark where a human-facing title ends. Matched
# case-insensitively against whitespace-split tokens; the title is everything
# BEFORE the first such token. Kept deliberately small — leaving a little cruft
# is better than trimming a real word out of a title.
_TITLE_STOP = re.compile(
    r"^(?:\d{3,4}p|4k|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|"
    r"web[\-]?dl|web[\-]?rip|webrip|bluray|bdrip|brrip|dvdrip|hdrip|hdtv|"
    r"remux|amzn|nf|hulu|dsnp|atvp|hmax|"
    r"ddp?\d?|dd5|aac\d?|ac3|eac3|dts|truehd|atmos|flac|"
    r"repack|proper|internal|limited|extended|unrated|remastered|"
    r"complete|multi|dual|hdr|hdr10|dv|sdr|imax)$",
    re.IGNORECASE,
)


def _display_title(name: str) -> str:
    """Best-effort clean of a media filename into a human title for external
    displays. Strips the extension, normalizes dot/underscore separators to
    spaces, and cuts the string at the first scene-release token (resolution /
    source / codec / audio / tag). Purely cosmetic and used ONLY by
    /api/now-playing — the app's own UI keeps the raw filename. Falls back to
    the extension-stripped name if cleaning would empty it out."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name).strip()
    norm = re.sub(r"[._]+", " ", stem)
    kept = []
    for t in norm.split():
        if _TITLE_STOP.match(t.strip("()[]")):
            break
        kept.append(t)
    return " ".join(kept).strip(" -") or stem


@app.route("/api/now-playing")
def api_now_playing():
    """Unauthenticated feed for external displays (e.g. the living-room hub /
    dashboard). Returns the currently-playing, filename-derived title so a
    dumb client can show "The Matrix" instead of a generic "Jetstream
    livestream" label, plus playback position/duration and a server timestamp
    so a display can render "0:42 / 1:38" and detect a stale/cached reply.
    Deliberately tokenless — no paths, viewers, or tokens leak, just the title
    every viewer already sees plus non-sensitive timing. `title` is "",
    `playing` is false, and the timing fields are null when nothing is loaded,
    so the client can fall back to its own default. Same title + position
    source as /api/status."""
    with state_lock:
        src = current_source
        playing = src is not None
        raw = (src or {}).get("title") or ""
        stype = (src or {}).get("type")
        ref = (src or {}).get("ref", "")
        duration = (src or {}).get("duration")
        is_live = (src or {}).get("is_live", False)
        # Position: live encoder → computed elapsed; paused → frozen offset;
        # otherwise unknown. Mirrors /api/status exactly.
        if current_proc is not None and current_proc.poll() is None:
            position = _current_position(running=True)
        elif current_paused:
            position = paused_position
        else:
            position = None
    if src is None:
        title = ""
    elif stype == "file" or not raw:
        # File sources carry a raw scene-release filename (Path(path).name);
        # prettify it. URL/live sources already have a human title from yt-dlp,
        # so only prettify them when the title is missing (fall back to ref).
        title = _display_title(raw or os.path.basename(ref))
    else:
        title = raw
    resp = jsonify({
        "title": title,
        "playing": playing,
        "is_live": is_live,
        "position_seconds": position,
        "duration_seconds": duration,
        # When this reply was generated — lets a polling display spot a stale
        # (cached/proxied) response despite the 2 s Cache-Control below.
        "server_unix": time.time(),
    })
    # Short cache so a polling display doesn't hammer Flask, but still tracks
    # source changes within a couple seconds.
    resp.headers["Cache-Control"] = "public, max-age=2"
    return resp


@app.route("/hls/<path:filename>")
def hls(filename):
    _track_viewer()
    resp = send_from_directory(HLS_DIR, filename, conditional=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/poster")
def poster():
    """Proxy + cache cover art from Sonarr/Radarr for any viewer-known media
    path. `?path=` is the same relative path the UI uses everywhere (e.g.
    `movies/Title (Year)/file.mkv`). We walk the path's components looking
    for a basename match in the arr inventory cover map; on hit we fetch
    arr's MediaCover URL once, cache the jpeg under POSTERS_DIR keyed by
    sha256 of the raw path, and serve it with a long-lived Cache-Control so
    the browser stops re-asking. Misses (path → no match) return 204 so the
    UI can hide the `<img>` cleanly with `onerror`."""
    raw = request.args.get("path", "")
    if not raw:
        return ("", 400)
    info = _arr_cover_for_path(raw)
    if not info:
        # Cache the "no match" verdict client-side too — the path-to-arr
        # mapping only changes when the background refresher repolls (every
        # 30 min), so re-asking on every render is wasted Flask round-trips.
        resp = app.response_class("", status=204)
        resp.headers["Cache-Control"] = "public, max-age=600"
        return resp
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    cached = POSTERS_DIR / f"{h}.jpg"
    if not cached.exists():
        base, url, key = info
        full = url if url.startswith("http") else f"{base}{url}"
        # Pass the API key only when fetching arr's local mirror (which would
        # also need basic auth in most homelab configs — see remoteUrl-first
        # logic in _arr_fetch_inventory). The public TVDB/TMDB CDN ignores
        # the header. 5 s timeout caps how long a single Flask worker is
        # tied up on the cache-miss path — TVDB usually first-bytes within
        # ~1 s, but the long tail used to be a 10 s ceiling that could lock
        # up multiple workers if a viewer loaded a grid full of fresh shows.
        headers = {"X-Api-Key": key} if url.startswith(base) else {}
        try:
            req = urllib.request.Request(full, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                data = r.read()
            POSTERS_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        except Exception as e:
            print(f"poster fetch failed for {raw}: {e}", file=sys.stderr)
            return ("", 502)
    resp = send_from_directory(POSTERS_DIR, cached.name, mimetype="image/jpeg")
    # Poster art doesn't change for the life of an arr entry; let the browser
    # cache for an hour so re-rendering a grid doesn't re-hit Flask.
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ==== v2: vod engine =======================================================
# Per-user on-demand transcode sessions. Each session is one ffmpeg (built by
# _build_ffmpeg_cmd with mode="vod") writing an fmp4 HLS ladder-of-one into
# /hls/vod/<sid>/, served by nginx behind /api/_authcheck_vod (which also
# bumps last_access per segment fetch — the reaper's idle signal). Seeks are
# kill+restart into the same sid with a bumped `generation` so the client can
# cache-bust the playlist URL. No playlist splicing, ever.

VOD_PLAYLIST_NAME = "idx.m3u8"  # what _build_ffmpeg_cmd names its playlist


def _vod_playlist_url(sid: str, generation: int) -> str:
    url = f"/hls/vod/{sid}/{VOD_PLAYLIST_NAME}"
    return f"{url}?g={generation}" if generation else url


def _vod_build_cmd(sess_dir: Path, rel_path: str, start: float,
                   audio_idx: int | None, subtitle_idx: int | None) -> list[str]:
    full = _safe_resolve(rel_path, must_be_file=True)
    return _build_ffmpeg_cmd(
        full, sess_dir, start, audio_idx, subtitle_idx, mode="vod")


def _vod_start(user_id: str, rel_path: str, start: float = 0.0,
               subtitle_idx: int | None = None) -> dict | tuple:
    """Start a VOD session. Returns the registry record (plus "session_id")
    on success, or an ("error-code",) tuple: ("not_found",) for a bad path,
    ("capacity",) when all VOD_MAX_SESSIONS slots are taken by OTHER users
    (starting a new film replaces your own session first — one per user)."""
    rel = (rel_path or "").strip("/")
    if not _viewer_file_allowed(rel):
        return ("not_found",)
    try:
        full = _safe_resolve(rel, must_be_file=True)
    except Exception:
        return ("not_found",)
    # Probes (ffprobe subprocesses) run before taking vod_lock so a slow disk
    # can't stall the per-segment _authcheck_vod heartbeat.
    duration = _probe_duration(full)
    start = max(0.0, float(start or 0.0))
    if duration is not None and start >= duration:
        start = max(0.0, duration - 1.0)
    audio_idx = _probe_english_audio(str(full))
    sid = secrets.token_urlsafe(16)
    sess_dir = _vod_session_dir(sid)
    now = time.time()
    # Placeholder-first: reserve the registry slot BEFORE the slow command
    # build (it ffprobes the source) and the spawn. This closes two races a
    # probe-then-lock shape had: (a) capacity TOCTOU — two starts could both
    # pass the cap check they measured pre-lock; (b) rapid same-user
    # double-click — the later start detaches this placeholder, and we notice
    # at install time instead of leaving the registry pointing at film A
    # while the client plays film B.
    rec = {
        "user_id": user_id, "rel_path": rel, "title": full.name,
        "proc": None, "start_offset": start, "started_at": now,
        "last_access": now, "duration": duration, "status": "starting",
        "generation": 0,
        # kept so _vod_seek rebuilds the exact same track selection
        "audio_idx": audio_idx, "subtitle_idx": subtitle_idx,
    }
    with vod_lock:
        old = [(s, _vod_detach_locked(s))
               for s in [s for s, r in vod_sessions.items()
                         if r.get("user_id") == user_id]]
        over_cap = len(vod_sessions) >= VOD_MAX_SESSIONS
        if not over_cap:
            vod_sessions[sid] = rec
    for old_sid, old_sess in old:  # one-per-user: replaced session dies
        _vod_dispose(old_sid, old_sess)
    if over_cap:
        return ("capacity",)
    try:
        cmd = _build_ffmpeg_cmd(
            full, sess_dir, start, audio_idx, subtitle_idx, mode="vod")
        sess_dir.mkdir(parents=True, exist_ok=True)
        proc = _spawn_ffmpeg(cmd, sess_dir / "ffmpeg.log")
    except Exception:
        with vod_lock:
            _vod_detach_locked(sid)
        shutil.rmtree(sess_dir, ignore_errors=True)
        return ("not_found",)
    with vod_lock:
        if vod_sessions.get(sid) is rec:
            rec["proc"] = proc
            rec["status"] = "running"
            rec["last_access"] = time.time()
            return {**rec, "session_id": sid}
    # Superseded while we were spawning (a newer start by the same user, or
    # an admin kill) — dispose our encoder rather than leaking it.
    _vod_dispose(sid, {"proc": proc})
    return ("conflict",)


def _vod_seek(sid: str, position: float) -> dict | None:
    """Restart a session's ffmpeg at `position` — same sid, wiped dir,
    generation+1 (the playlist URL carries ?g=<gen> as a cache-buster).
    Returns the updated record, or None for an unknown sid.

    Three phases so vod_lock is never held across slow work (proc.wait,
    rmtree, the ffprobe inside the command builder) — the per-segment
    _authcheck_vod heartbeat contends on this lock, so a seek that held it
    for 5+ s would stall every VOD viewer's segment fetches."""
    with vod_lock:  # phase 1: take exclusive hold of the proc handle
        sess = vod_sessions.get(sid)
        if not sess or sess.get("status") == "seeking":
            return None  # unknown, or a concurrent seek already in flight
        old_proc = sess.get("proc")
        sess["proc"] = None
        sess["status"] = "seeking"
        sess["last_access"] = time.time()  # don't get idle-reaped mid-seek
        rel_path = sess["rel_path"]
        audio_idx = sess.get("audio_idx")
        subtitle_idx = sess.get("subtitle_idx")
        duration = sess.get("duration")
    # phase 2 (unlocked): all the slow work
    _vod_dispose(sid, {"proc": old_proc})  # terminate + wait + rmtree
    sess_dir = _vod_session_dir(sid)
    sess_dir.mkdir(parents=True, exist_ok=True)
    position = max(0.0, float(position or 0.0))
    if duration is not None and position >= duration:
        position = max(0.0, duration - 1.0)
    try:
        cmd = _vod_build_cmd(sess_dir, rel_path, position,
                             audio_idx, subtitle_idx)
    except Exception:
        # Source vanished mid-session (e.g. library cleanup) — drop it.
        _vod_kill_session(sid)
        return None
    proc = _spawn_ffmpeg(cmd, sess_dir / "ffmpeg.log")
    with vod_lock:  # phase 3: reinstall, unless killed while we worked
        if vod_sessions.get(sid) is sess:
            sess["proc"] = proc
            sess["start_offset"] = position
            sess["generation"] = int(sess.get("generation") or 0) + 1
            sess["last_access"] = time.time()
            sess["status"] = "running"
            return dict(sess)
    # Session was killed (admin/stop/reaper) during phase 2 — don't leak the
    # freshly spawned encoder.
    _vod_dispose(sid, {"proc": proc})
    return None


def _vod_stop(sid: str) -> bool:
    return _vod_kill_session(sid)


def _vod_reaper():
    """Reap idle and dead VOD sessions every 5s. Idle = no segment fetch
    (last_access via _authcheck_vod) for VOD_IDLE_TIMEOUT_S. Dead ffmpeg is
    only reaped once last_access is >30s old — grace so a just-started proc
    that crashed instantly still leaves its ffmpeg.log inspectable and a
    session isn't reaped between /api/vod/start and the first fetch.
    Detach under the lock, dispose (proc.wait/rmtree) outside it."""
    while True:
        time.sleep(5)
        try:
            now = time.time()
            doomed = []
            with vod_lock:
                for sid, sess in list(vod_sessions.items()):
                    idle = now - float(sess.get("last_access") or 0)
                    proc = sess.get("proc")
                    dead = proc is not None and proc.poll() is not None
                    if idle > VOD_IDLE_TIMEOUT_S or (dead and idle > 30):
                        doomed.append((sid, _vod_detach_locked(sid), dead, idle))
            for sid, sess, dead, idle in doomed:
                print(f"vod: reaping session {sid} "
                      f"({'dead ffmpeg' if dead else 'idle'}, "
                      f"idle {idle:.0f}s)", file=sys.stderr)
                _vod_dispose(sid, sess)
        except Exception as e:
            print(f"vod reaper error: {e}", file=sys.stderr)


# Orphan sweep before the reaper starts: vod_sessions is memory-only, so any
# dirs left by a previous process (crash / deploy restart) have no registry
# entry and would otherwise sit on the VOD volume forever (_cleanup_hls
# deliberately skips this tree). Single gunicorn worker (-w 1), so there's no
# sibling process whose live sessions this could wipe.
if VOD_DIR_BASE.exists():
    for _orphan in VOD_DIR_BASE.iterdir():
        shutil.rmtree(_orphan, ignore_errors=True)

threading.Thread(target=_vod_reaper, daemon=True, name="vod-reaper").start()


@app.route("/library")
def library_page():
    # Gate redirects logged-out browsers to /login before this runs.
    return send_from_directory("static", "library.html")


@app.route("/api/user/library/browse")
def api_user_library_browse():
    """User-tier twin of /api/library/browse — same helper, same payload.
    Gated to logged-in users by _gate_viewer_routes (/api/user/ prefix)."""
    return jsonify(_viewer_list_dir(request.args.get("path", "")))


@app.route("/api/user/library/search")
def api_user_library_search():
    items, truncated = _search_viewer_library(request.args.get("q", "").strip())
    return jsonify({"results": items, "truncated": truncated})


def _vod_owned_session(sid: str) -> dict | None:
    """Registry record for `sid` IF the calling user owns it, else None.
    Not-owned and non-existent are deliberately the same answer (404) so
    session ids can't be probed."""
    u = _session_user()
    if not u or not sid:
        return None
    with vod_lock:
        sess = vod_sessions.get(sid)
        if not sess or sess.get("user_id") != u["id"]:
            return None
        return dict(sess)


@app.route("/api/vod/start", methods=["POST"])
def api_vod_start():
    u = _session_user()
    data = request.get_json(silent=True) or {}
    rel_path = str(data.get("path") or "")
    # Auto-resume: an ABSENT `start` means "put me where I left off"; an
    # explicit start (including 0) is the caller overriding that — which is
    # how "Start over" works. `or 0.0` would collapse those two cases, so
    # test for the key's presence, not its truthiness.
    resumed_from = None
    if "start" in data and data.get("start") is not None:
        try:
            start = float(data.get("start"))
        except (TypeError, ValueError):
            start = 0.0
    else:
        start = _progress_lookup(u["id"], rel_path.strip("/")) or 0.0
        if start > 0:
            resumed_from = start
    sub_raw = data.get("subtitle_idx")
    try:
        subtitle_idx = int(sub_raw) if sub_raw is not None else None
    except (TypeError, ValueError):
        subtitle_idx = None
    res = _vod_start(u["id"], rel_path, start, subtitle_idx)
    if isinstance(res, tuple):
        if res[0] == "capacity":
            return jsonify({
                "error": "vod_capacity",
                "message": f"All {VOD_MAX_SESSIONS} streams in use — "
                           "try again later",
            }), 409
        if res[0] == "conflict":
            # A newer start by the same user superseded this one mid-spawn.
            return jsonify({"error": "vod_conflict"}), 409
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "session_id": res["session_id"],
        "playlist_url": _vod_playlist_url(res["session_id"], 0),
        "duration": res["duration"],
        "start_offset": res["start_offset"],
        # Non-null when we put the viewer back mid-film, so the UI can say
        # so and offer "Start over".
        "resumed_from": resumed_from,
    })


@app.route("/api/vod/progress", methods=["POST"])
def api_vod_progress():
    """Client heartbeat carrying the absolute playhead. The server can't know
    it — it only knows the session's start_offset, not where the player is
    inside the transcoded range — so the page reports it on its existing 5 s
    status poll and on teardown."""
    data = request.get_json(silent=True) or {}
    sess = _vod_owned_session(str(data.get("session_id") or ""))
    if not sess:
        return jsonify({"error": "not_found"}), 404
    try:
        position = float(data.get("position"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad_position"}), 400
    _progress_record(sess["user_id"], sess["rel_path"], position,
                     sess.get("duration"), sess.get("title"))
    return jsonify({"ok": True})


@app.route("/api/user/continue", methods=["GET", "DELETE"])
def api_user_continue():
    """The Continue-watching row: in-progress films, newest first."""
    u = _session_user()
    if request.method == "DELETE":
        path = str((request.get_json(silent=True) or {}).get("path") or "")
        if not _progress_forget(u["id"], path.strip("/")):
            return jsonify({"error": "not_found"}), 404
        return jsonify({"ok": True})
    with progress_lock:
        mine = dict(watch_progress.get(u["id"]) or {})
    items = []
    for rel_path, rec in mine.items():
        # Skip anything the library can no longer serve (file deleted, or a
        # root removed from VIEWER_LIBRARY_ROOTS) — a card that 404s on click
        # is worse than no card.
        if not _viewer_file_allowed(rel_path):
            continue
        duration = rec.get("duration")
        position = float(rec.get("position") or 0)
        items.append({
            "path": rel_path,
            "title": rec.get("title") or Path(rel_path).name,
            "position": position,
            "duration": duration,
            "updated": rec.get("updated"),
            "percent": (min(100.0, position / duration * 100)
                        if duration else 0.0),
        })
    items.sort(key=lambda x: x.get("updated") or 0, reverse=True)
    return jsonify({"items": items})


@app.route("/api/vod/seek", methods=["POST"])
def api_vod_seek():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("session_id") or "")
    if not _vod_owned_session(sid):
        return jsonify({"error": "not_found"}), 404
    try:
        position = float(data.get("position") or 0.0)
    except (TypeError, ValueError):
        position = 0.0
    sess = _vod_seek(sid, position)
    if not sess:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "playlist_url": _vod_playlist_url(sid, sess["generation"]),
        "start_offset": sess["start_offset"],
    })


@app.route("/api/vod/stop", methods=["POST"])
def api_vod_stop():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("session_id") or "")
    if not _vod_owned_session(sid):
        return jsonify({"error": "not_found"}), 404
    _vod_stop(sid)
    return jsonify({"ok": True})


@app.route("/api/vod/status")
def api_vod_status():
    sid = request.args.get("session_id", "")
    sess = _vod_owned_session(sid)
    if not sess:
        return jsonify({"error": "not_found"}), 404
    # Buffer depth: segments on disk × segment duration (nothing is deleted
    # for VOD, so this is the full transcoded range from start_offset).
    try:
        n_segs = sum(1 for p in _vod_session_dir(sid).glob("seg_*.m4s"))
        transcoded_ahead_s = n_segs * float(HLS_SEG_TIME)
    except Exception:
        transcoded_ahead_s = None
    status = sess.get("status", "running")
    proc = sess.get("proc")
    if proc is not None and proc.poll() is not None:
        # poll() at read time is fresh enough — but a clean exit (rc 0,
        # transcode complete) and a mid-file crash are different answers:
        # the client treats "ended" as "the whole remainder is on disk" and
        # stops polling, which would leave a crash looking fully buffered.
        status = "ended" if proc.returncode == 0 else "error"
    return jsonify({
        "status": status,
        "start_offset": sess["start_offset"],
        "duration": sess["duration"],
        "transcoded_ahead_s": transcoded_ahead_s,
    })


@app.route("/admin/api/vod/sessions")
def api_admin_vod_sessions():
    with vod_lock:
        snap = [(sid, dict(sess)) for sid, sess in vod_sessions.items()]
    with users_lock:
        names = {u["id"]: u["username"] for u in users}
    return jsonify({
        "sessions": [{
            "session_id": sid,
            "username": names.get(sess.get("user_id"), "?"),
            "title": sess.get("title"),
            "start_offset": sess.get("start_offset"),
            "started_at": sess.get("started_at"),
            "last_access": sess.get("last_access"),
        } for sid, sess in snap],
        "cap": VOD_MAX_SESSIONS,
    })


@app.route("/admin/api/vod/sessions/<sid>", methods=["DELETE"])
def api_admin_vod_session_delete(sid):
    if not _vod_stop(sid):
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
