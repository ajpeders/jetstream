import hashlib
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
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

VIEWER_TIMEOUT = 30  # seconds without an HLS request → viewer dropped
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
PLAYLIST_FILE = Path(os.environ.get("PLAYLIST_FILE", "/data/playlist.json"))
RECENT_FILE = Path(os.environ.get("RECENT_FILE", "/data/recent.json"))
RECENT_LIMIT = int(os.environ.get("RECENT_LIMIT", "50"))
REQUESTS_FILE = Path(os.environ.get("REQUESTS_FILE", "/data/requests.json"))
# Pending viewer "request to queue" entries auto-expire after this many seconds
# if the host hasn't approved or denied them. Without this the list grows
# forever (the only way it shrinks is explicit host action), and a friend who
# requested something weeks ago has long since lost interest. Swept lazily on
# every list view — no background timer.
REQUEST_TTL_SECS = 7 * 24 * 60 * 60  # 7 days
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/data/settings.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
VIEWER_LOG_FILE = Path(os.environ.get("VIEWER_LOG_FILE", "/data/viewer_log.jsonl"))

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
HLS_DIR = Path(os.environ.get("HLS_DIR", "/hls")).resolve()
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
HLS_SEG_TIME = os.environ.get("HLS_SEG_TIME", "4")
# 450 segments × 4s = 30 minutes of scroll-back buffer for admin.
HLS_LIST_SIZE = os.environ.get("HLS_LIST_SIZE", "450")

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
# The allowed set is fixed server-side so a client can't inject arbitrary
# (or oversized) strings into everyone's overlay.
REACTION_EMOJIS = ("😂", "❤️", "🔥", "😮", "👏", "💀")
REACTION_BUFFER_SIZE = 100
REACTION_RECENT_WINDOW = 6  # seconds of lookback in /reactions/recent
REACTION_RATE_WINDOW = 10
REACTION_RATE_MAX = 25      # per-IP reactions per window (spammy by nature)
reactions_lock = threading.Lock()
reactions = _collections.deque(maxlen=REACTION_BUFFER_SIZE)
reaction_next_id = 1
reaction_rate: dict[str, list[float]] = {}

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


def _caller_can_control() -> bool:
    """True if the request's token cookie grants playback control."""
    return _token_level(request.cookies.get(TOKEN_COOKIE)) in CONTROL_LEVELS


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


settings_lock = threading.Lock()
settings: dict = {"viewer_public": False, "auto_fill": True}


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
_load_playlist()
_load_recent()
_load_requests()
_load_settings()


NO_TOKEN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jetstream — invite required</title>
<style>
  body { background: #0a0a0a; color: #888; font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { text-align: center; max-width: 30rem; padding: 2rem; }
  h1 { color: #c00; font-size: 1.4rem; margin: 0 0 1rem; letter-spacing: 0.05em; }
  p { line-height: 1.5; margin: 0.6rem 0; }
  .small { color: #555; font-size: 0.85rem; margin-top: 1.5rem; }
</style>
</head><body>
<div class="card">
  <h1>JETSTREAM</h1>
  <p>You need an invite link to watch.</p>
  <p class="small">Ask the host for one — it'll look like<br><code>live.thelunadog.com/?t=…</code></p>
</div></body></html>"""


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
        with tokens_lock:
            for t in tokens:
                if t.get("id") == token:
                    label = t.get("label")
                    break
    is_new = False
    with viewers_lock:
        if ip not in viewers:
            is_new = True
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
    if stale:
        for ip in stale:
            print(f"[viewer] disconnect ip={ip} (idle > {VIEWER_TIMEOUT}s)",
                  file=sys.stderr, flush=True)
    with viewers_lock:
        return len(viewers)


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
    items.sort(key=lambda i: (i["type"] != "directory", i["name"].lower()))
    return items


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


def _cleanup_hls():
    """Wipe ALL HLS state — master playlist, every run dir, every segment.
    Used on full stop / idle, and on startup before any ffmpeg spawns.

    Caller MUST NOT hold state_lock — this acquires it. (Also acquires
    _composer_state_lock; lock order matches the composer's tick path so
    they can't deadlock.)"""
    for entry in HLS_DIR.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except FileNotFoundError:
            pass
    RUN_DIR_BASE.mkdir(parents=True, exist_ok=True)
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
    # Subtitle index: respect what's stored on the source dict; only auto-pick
    # when it's entirely absent (pre-subtitle-support state). Same convention
    # as _start_stream.
    next_source = dict(candidate)
    if "subtitle_idx" not in next_source:
        next_source["subtitle_idx"] = _pick_default_subtitle(str(full))
    if next_source.get("duration") is None:
        next_source["duration"] = _probe_duration(full)
    audio_idx = _probe_english_audio(str(full))
    run_id = next_run_id
    run_dir = _run_dir(run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = _build_ffmpeg_cmd(
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


def _current_position() -> float | None:
    if current_proc is None or current_proc.poll() is not None:
        return None
    if current_paused:
        return paused_position
    pos = current_start_offset + max(0.0, time.time() - current_start_time)
    duration = (current_source or {}).get("duration")
    if duration:
        pos = min(pos, duration)
    return pos


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
    get real 1080p, then merge here at encode time."""
    input_str = str(input_path)
    # Single ffprobe pass — codec for HW-decode eligibility, height for output
    # cap, transfer/primaries for HDR detection.
    info = _probe_video_info(input_str) or {}
    src_codec = info.get("codec")
    src_height = info.get("height")
    is_hdr = bool(info.get("is_hdr"))
    # Output height: clamp to TARGET_HEIGHT, but don't upscale a smaller source
    # (a 720p WEB-DL has nothing to gain from being upscaled to 1080p, just CPU).
    out_h = min(src_height, TARGET_HEIGHT) if src_height else TARGET_HEIGHT
    # Decide whether the GPU can decode this source; falls back to CPU decode
    # for codecs the iHD VLD engine doesn't support (e.g. AV1) or if ffprobe fails.
    # HDR sources go through CPU decode unconditionally — the HW path can't
    # tonemap on this iGPU (no VPP), and the zscale tonemap chain only works
    # on CPU-side frames.
    hw_decode = (
        USE_VAAPI and USE_VAAPI_DECODE
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
        USE_NVENC and src_codec in NVDEC_DECODE_CODECS
        and not info.get("is_dovi")
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if USE_NVENC:
        # Frames are kept in `cuda` hw frames format when NVDEC is used so the
        # whole pipeline (decode → optional scale_cuda → nvenc) stays on-GPU.
        if nv_decode:
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif USE_VAAPI:
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
    if USE_NVENC:
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
            # p1..p7 quality/speed dial: p4 is the balanced middle. ll tune
            # keeps latency low for live HLS (b-frames off, single-frame look-
            # ahead). cbr keeps segment sizes predictable for HLS.
            "-preset", "p4",
            "-tune", "ll",
            "-rc", "cbr",
            "-b:v", VIDEO_BITRATE,
            "-maxrate", VIDEO_BITRATE,
            "-bufsize", "10M",
            "-profile:v", "main",
            "-g", str(gop),
            "-forced-idr", "1",
            "-no-scenecut", "1",
            # NOTE: h264_metadata BSF was here to insert AUDs (Firefox) and
            # to tag BT.709 color in the SPS VUI. It corrupted the fmp4 init
            # segment's avcC box — the SPS/PPS were not extracted into the
            # MP4 container's codec configuration record (profile=unknown,
            # level=-99 in ffprobe on init.mp4 alone). Chrome's MSE needs a
            # valid `codecs="avc1.xxxxxx"` derived from avcC to create a
            # SourceBuffer, and silently fails to play the stream when that
            # string can't be built. Firefox color tagging tracked under the
            # "Firefox playback" ROADMAP item.
        ]
    elif USE_VAAPI:
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
            "-tune", "zerolatency",
            "-b:v", VIDEO_BITRATE,
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
        "-hls_list_size", HLS_LIST_SIZE,
        # +program_date_time tags every segment with the server's wall clock.
        # Clients use that as a universal reference to converge on the same
        # playhead (vs. each holding their own per-buffer "live edge").
        "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
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


def _parse_run_idx(run_dir: Path) -> tuple[str | None, list[dict]]:
    """Read a run's idx.m3u8 and return (init_uri, [segment dicts]). Each
    segment dict has 'filename', 'duration', 'pdt'. Skips runs that don't
    have an EXT-X-MAP yet (ffmpeg starting up) or whose idx.m3u8 is missing."""
    idx = run_dir / "idx.m3u8"
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


def _composer_tick():
    """Stitch /hls/stream.m3u8 from all run dirs' idx.m3u8 files. Atomic write.
    If no runs have any segments, deletes /hls/stream.m3u8 (idle signal so the
    client knows there's nothing to play)."""
    run_ids = _list_run_ids()
    runs: list[tuple[int, str, list[dict]]] = []  # (run_id, init_uri, segments)
    for rid in run_ids:
        init_uri, segs = _parse_run_idx(_run_dir(rid))
        if init_uri is None or not segs:
            continue
        _composer_assign_seqs(rid, segs)
        runs.append((rid, init_uri, segs))

    master = HLS_DIR / "stream.m3u8"
    if not runs:
        # No playable content. Drop the master so /api/status's playlist_ready
        # check (and viewer's idle path) reflects reality.
        try:
            master.unlink()
        except FileNotFoundError:
            pass
        return

    # Truncate the global window from the FRONT to the most recent
    # GLOBAL_HLS_LIST_SIZE segments. Older segments roll off — their files
    # may still be on disk (until ffmpeg's per-run delete_segments rotation
    # or our finished-run cleanup catches up), but they stop being referenced.
    global_window = int(HLS_LIST_SIZE)
    total = sum(len(segs) for _, _, segs in runs)
    drop = max(0, total - global_window)
    # Walk front-to-back dropping segments from the oldest run(s) until we've
    # dropped `drop` of them. A run that ends up with zero kept segments is
    # excluded from the master entirely.
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
        return

    # Header values: MEDIA-SEQUENCE = the assigned seq of the first kept
    # segment; DISCONTINUITY-SEQUENCE = the assigned disc of the first kept
    # run.
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
        # Discontinuity marker between runs (not before the very first run
        # in the playlist — that boundary is implicit, and DISCONTINUITY-SEQUENCE
        # already accounts for runs that rolled off).
        if run_idx > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        # EXT-X-MAP MUST appear before the segments it applies to. Each run
        # has its own init.mp4, so we emit a fresh MAP at every run boundary.
        lines.append(f'#EXT-X-MAP:URI="run/{rid}/{init_uri}"')
        for seg in segs:
            if seg["pdt"]:
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg['pdt']}")
            duration = seg["duration"] if seg["duration"] is not None else float(HLS_SEG_TIME)
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(f"run/{rid}/{seg['filename']}")
    # No EXT-X-ENDLIST — this is a live playlist, the player must keep polling.
    body = "\n".join(lines) + "\n"
    tmp = HLS_DIR / ".stream.m3u8.tmp"
    try:
        tmp.write_text(body)
        tmp.replace(master)  # atomic on POSIX — viewers never read a half-written manifest
    except Exception as e:
        print(f"composer: master write failed: {e}", file=sys.stderr)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    # Clean up finished runs whose segments have all rolled out of the kept
    # window. The active run is never cleaned up here — only _stop_locked
    # / a fresh _start_stream will retire it.
    kept_run_ids = {rid for rid, _, _ in kept}
    with state_lock:
        finished_snapshot = set(finished_run_ids)
        currently_active = active_run_id
    for rid in finished_snapshot:
        if rid == currently_active:
            continue
        if rid in kept_run_ids:
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
    tick = 0
    while True:
        time.sleep(1)
        tick += 1
        try:
            with state_lock:
                proc = current_proc
                paused = current_paused
                # Build a snapshot of live globals while holding the lock; do the
                # actual file write below, after we've dropped it.
                snapshot = None
                if current_source is not None:
                    pos = _current_position()
                    if pos is None and current_paused:
                        pos = paused_position
                    snapshot = {
                        "source": current_source,
                        "position_seconds": pos if pos is not None else 0.0,
                        "paused": current_paused,
                    }
            # Still streaming — periodically persist position so a crash recovers near where we were.
            if proc is not None and proc.poll() is None:
                if tick % 10 == 0 and snapshot is not None:
                    _save_state(snapshot)
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
                        promote_snap = {
                            "source": current_source,
                            "position_seconds": 0.0,
                            "paused": False,
                        }
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
    # Subtitle burn-in: use the value already on the source dict (set at
    # queue/play time, including explicit `null` to mean "no subs"). Only
    # auto-pick as a fallback when the key is entirely absent — i.e. this is
    # a legacy state.json or a path that pre-dates subtitle support. URLs
    # don't carry sub indices.
    if source["type"] == "file" and "subtitle_idx" not in source:
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
        cmd = _build_ffmpeg_cmd(
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
        snapshot = {
            "source": current_source,
            "position_seconds": current_start_offset,
            "paused": False,
        }
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
    # Control surface (the /controls page + /api/control/* endpoints) requires
    # a friend-or-admin token regardless of public mode — viewers and the
    # anonymous public can watch but never drive playback. Check this before
    # the public-mode short-circuit so public mode can't leak controls.
    if p == "/controls" or p.startswith("/api/control/"):
        # Honor a ?t= invite on the control page the same way viewer routes do,
        # so a friend link lands straight on /controls with the cookie set.
        qs_t = request.args.get("t")
        if p == "/controls" and qs_t and _token_level(qs_t) in CONTROL_LEVELS:
            resp = redirect(p)
            resp.set_cookie(
                TOKEN_COOKIE, qs_t,
                max_age=TOKEN_COOKIE_MAX_AGE,
                httponly=True, secure=True, samesite="Lax",
            )
            return resp
        if _caller_can_control():
            return None
        if p == "/controls":
            return NO_TOKEN_PAGE, 401
        return ("", 403)
    with settings_lock:
        if settings.get("viewer_public"):
            return None  # public mode — anyone can watch
    if _valid_token(request.cookies.get(TOKEN_COOKIE)):
        return None
    qs_t = request.args.get("t")
    if _valid_token(qs_t):
        resp = redirect(p)
        resp.set_cookie(
            TOKEN_COOKIE, qs_t,
            max_age=TOKEN_COOKIE_MAX_AGE,
            httponly=True, secure=True, samesite="Lax",
        )
        return resp
    if p == "/":
        return NO_TOKEN_PAGE, 401
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
    if not public and not _valid_token(request.cookies.get(TOKEN_COOKIE)):
        return ("", 401)
    # Authorized — log this viewer activity. Idempotent: updates the timestamp
    # for known IPs and only emits a connect-log on the very first sighting.
    _track_viewer()
    return ("", 204)


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


@app.route("/reactions/send", methods=["POST"])
def api_reaction_send():
    """Append an emoji reaction to the ephemeral ring. `emoji` must be one of
    REACTION_EMOJIS; `sid` is the same opaque client tag chat uses (so a client
    can skip re-animating its own reaction). Per-IP rate-limited."""
    data = request.get_json(silent=True) or {}
    emoji = (data.get("emoji") or "").strip()
    if emoji not in REACTION_EMOJIS:
        return jsonify({"error": "unknown emoji"}), 400
    if not _reaction_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    sid = (data.get("sid") or "").strip()[:32] or "anon"
    global reaction_next_id
    with reactions_lock:
        r = {"id": reaction_next_id, "ts": time.time(), "emoji": emoji, "sid": sid}
        reaction_next_id += 1
        reactions.append(r)
    return jsonify({"ok": True, "id": r["id"]})


@app.route("/reactions/recent")
def api_reaction_recent():
    """Reactions with id > `since` AND newer than REACTION_RECENT_WINDOW. The
    recency filter keeps this an ephemeral feed — clients animate each reaction
    once and late joiners don't get a backlog."""
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
    return jsonify({"reactions": out, "max_id": max_id, "emojis": list(REACTION_EMOJIS)})


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


@app.route("/api/library/browse")
def api_library_browse():
    """Viewer-facing library browse — same payload as the control-gated
    /admin/api/browse, but reachable by a plain viewer token (the viewer gate
    applies since this is neither /admin nor /api/control). Read-only: viewers
    use it to find something to request, not to play."""
    return jsonify(_list_dir(request.args.get("path", "")))


@app.route("/api/library/search")
def api_library_search():
    """Viewer-facing recursive search, same shape as /admin/api/search."""
    items, truncated = _search_library(request.args.get("q", "").strip())
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
    try:
        _safe_resolve(path, must_be_file=True)
    except Exception:
        return jsonify({"error": "file not found"}), 400
    if not _request_rate_check(_client_ip()):
        return jsonify({"error": "rate limited"}), 429
    requester = _clean_chat_name(data.get("name"))
    global request_next_id
    with requests_lock:
        # Sweep ghosts before dedup so an expired request for the same path
        # doesn't look like a live duplicate.
        _expire_old_requests()
        existing = next((r for r in media_requests if r["path"] == path), None)
        if existing:
            return jsonify({"ok": True, "duplicate": True, "request": existing})
        req = {
            "id": request_next_id,
            "ts": time.time(),
            "path": path,
            "title": Path(path).name,
            "requester": requester,
            "ip": _client_ip(),
        }
        request_next_id += 1
        media_requests.append(req)
        _save_requests()
    return jsonify({"ok": True, "request": req})


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
    """Approve a pending request: pop it and append a real file item to the
    playlist, mirroring api_queue_add's file branch (probe duration, subtitle
    pick — which returns None while burn-in is disabled). Idempotent against a
    missing id (404)."""
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


@app.route("/admin/api/requests/<int:rid>", methods=["DELETE"])
def api_request_deny(rid: int):
    """Deny a pending request: drop it without touching the playlist."""
    with requests_lock:
        req = next((r for r in media_requests if r["id"] == rid), None)
        if req is None:
            return jsonify({"error": "request not found"}), 404
        media_requests.remove(req)
        _save_requests()
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
        return jsonify(dict(settings))


SETTABLE_SETTINGS = {"viewer_public", "auto_fill"}


@app.route("/admin/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(silent=True) or {}
    keys = SETTABLE_SETTINGS & data.keys()
    if not keys:
        return jsonify({"error": f"one of {sorted(SETTABLE_SETTINGS)} required"}), 400
    with settings_lock:
        for k in keys:
            settings[k] = bool(data[k])
        _save_settings()
        return jsonify(dict(settings))


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


@app.route("/admin/api/skip", methods=["POST"])
@app.route("/api/control/skip", methods=["POST"])
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


def api_skip():
    """Skip to the next item: terminate ffmpeg without clearing source state and
    let the watcher pick the next thing (queue first, then auto_fill if on)."""
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
        snapshot = {
            "source": current_source,
            "position_seconds": paused_position,
            "paused": True,
        }
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
    with state_lock:
        running = current_proc is not None and current_proc.poll() is None
        active = current_source is not None  # a source is loaded (running OR paused)
        playlist_ready = (HLS_DIR / "stream.m3u8").exists()
        if running:
            position = _current_position()
        elif current_paused:
            position = paused_position
        else:
            position = None
        src = current_source if active else None
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
            "viewers": _viewer_count(),
            # For client-side sync: lets clients correct for clock skew so
            # the "play whatever PDT == server_now − 2s" target lands at the
            # same moment on every device.
            "server_unix": time.time(),
            # Permission hints for the viewer page: show a "Controls" link
            # when this token can drive playback. is_admin distinguishes the
            # host (full control surface) from a friend.
            "can_control": _caller_can_control(),
            "level": _token_level(request.cookies.get(TOKEN_COOKIE)),
            # Vote-to-skip tally for the current item (viewer-facing button).
            "skip_votes": len(skip_votes),
            "skip_needed": _skip_threshold(_viewer_count()),
            # Pending viewer requests — lets the admin UI badge the count
            # without polling /admin/api/requests when nothing's waiting.
            "requests_pending": len(media_requests),
        })


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
        # the header. Bumped timeout to 10 s because TVDB occasionally takes
        # a beat to first-byte.
        headers = {"X-Api-Key": key} if url.startswith(base) else {}
        try:
            req = urllib.request.Request(full, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
