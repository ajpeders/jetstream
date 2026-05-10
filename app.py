import ipaddress
import json
import os
import random
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
PLAYLIST_FILE = Path(os.environ.get("PLAYLIST_FILE", "/data/playlist.json"))
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/data/settings.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
VIEWER_LOG_FILE = Path(os.environ.get("VIEWER_LOG_FILE", "/data/viewer_log.jsonl"))

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
HLS_DIR = Path(os.environ.get("HLS_DIR", "/hls")).resolve()
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
USE_VAAPI = os.environ.get("USE_VAAPI", "1") == "1"
USE_VAAPI_DECODE = os.environ.get("USE_VAAPI_DECODE", "1") == "1"
# ffmpeg codec_name values supported by VAAPI VLD on this GPU (verified via vainfo).
HWACCEL_DECODE_CODECS = {"h264", "hevc", "vp8", "vp9", "mpeg2video"}
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "5M")
VIDEO_QP = os.environ.get("VIDEO_QP", "23")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "160k")
HLS_SEG_TIME = os.environ.get("HLS_SEG_TIME", "4")
# 450 segments × 4s = 30 minutes of scroll-back buffer for admin.
HLS_LIST_SIZE = os.environ.get("HLS_LIST_SIZE", "450")

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi"}

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

tokens_lock = threading.Lock()
tokens: list[dict] = []

playlist_lock = threading.Lock()
playlist: list[dict] = []


def _load_tokens():
    global tokens
    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            return
        except Exception:
            pass
    tokens = []


def _save_tokens():
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def _valid_token(t: str | None) -> bool:
    if not t:
        return False
    with tokens_lock:
        return any(x["id"] == t for x in tokens)


def _load_playlist():
    global playlist
    if PLAYLIST_FILE.exists():
        try:
            playlist = json.loads(PLAYLIST_FILE.read_text())
            return
        except Exception:
            pass
    playlist = []


def _save_playlist():
    PLAYLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYLIST_FILE.write_text(json.dumps(playlist, indent=2))


settings_lock = threading.Lock()
settings: dict = {"viewer_public": False, "auto_fill": True}


def _load_settings():
    global settings
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text())
            if isinstance(loaded, dict):
                settings.update(loaded)
        except Exception:
            pass


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
_load_settings()


NO_TOKEN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>livestream — invite required</title>
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
  <h1>LIVESTREAM</h1>
  <p>You need an invite link to watch.</p>
  <p class="small">Ask the host for one — it'll look like<br><code>live.thelunadog.com/?t=…</code></p>
</div></body></html>"""


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


_geo_cache: dict[str, str] = {}
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
    cached strings on subsequent calls. Best-effort; returns '?' on any failure."""
    with _geo_cache_lock:
        cached = _geo_cache.get(ip)
        if cached is not None:
            return cached
    if _is_private_ip(ip):
        loc = "LAN"
    else:
        try:
            req = urllib.request.Request(
                f"https://ipinfo.io/{ip}/json",
                headers={"User-Agent": "homelab-livestream/1"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read())
            parts = [data.get("city"), data.get("region"), data.get("country")]
            loc = ", ".join(p for p in parts if p) or "?"
        except Exception:
            loc = "?"
    with _geo_cache_lock:
        _geo_cache[ip] = loc
    return loc


def _track_viewer():
    ip = _client_ip()
    is_new = False
    with viewers_lock:
        if ip not in viewers:
            is_new = True
        viewers[ip] = time.time()
    if is_new:
        # Resolve token → label so the log says "who" instead of just an IP.
        token = request.cookies.get(TOKEN_COOKIE) or request.args.get("t")
        label = None
        if token:
            with tokens_lock:
                for t in tokens:
                    if t.get("id") == token:
                        label = t.get("label")
                        break
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


def _terminate_proc_locked():
    """Terminate the running ffmpeg (if any), leaving source/position state alone.
    Caller must hold state_lock."""
    global current_proc
    if current_proc and current_proc.poll() is None:
        try:
            current_proc.send_signal(signal.SIGTERM)
            current_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            current_proc.kill()
            current_proc.wait(timeout=2)
    current_proc = None


def _stop_locked():
    """Full stop: terminate ffmpeg AND clear all source/playback state. Also
    retires the active run (the composer's tick will then drop /hls/stream.m3u8
    once no playable segments remain anywhere)."""
    global current_source, current_start_offset, current_start_time
    global current_paused, paused_position, active_run_id
    _terminate_proc_locked()
    if active_run_id is not None:
        finished_run_ids.add(active_run_id)
        active_run_id = None
    current_source = None
    current_start_offset = 0.0
    current_start_time = 0.0
    current_paused = False
    paused_position = 0.0


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
    """Full-resolve a single video URL to a direct stream URL via yt-dlp."""
    try:
        out = subprocess.check_output(
            [
                "yt-dlp", "-J", "--no-playlist", "--no-warnings",
                "-f", "best[height<=1080][ext=mp4]/best[height<=1080]/best",
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
    stream_url = info.get("url")
    if not stream_url:
        formats = info.get("requested_formats") or []
        if formats:
            stream_url = formats[0].get("url")
    if not stream_url:
        raise ValueError("could not extract a direct stream URL")
    return {
        "stream_url": stream_url,
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
            })
        if not items:
            raise ValueError("playlist has no playable entries")
        return items
    return [{
        "type": "url", "ref": url,
        "title": info.get("title") or url,
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
    }]


def _probe_video_codec(input_path: str) -> str | None:
    """Return ffmpeg's codec_name for the first video stream, or None if probe fails."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        ).decode().strip().lower()
        return out or None
    except Exception:
        return None


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


def _build_ffmpeg_cmd(
    input_path: Path | str,
    run_dir: Path,
    start_seconds: float = 0.0,
    audio_idx: int | None = None,
) -> list[str]:
    """Build the ffmpeg HLS command. Output goes into `run_dir/`:
    `init.mp4` (fmp4 init segment), `seg_NNNNN.m4s` (media segments), and
    `idx.m3u8` (the per-run media playlist that the composer reads).

    `audio_idx` (the position of an English-tagged audio stream from
    `_probe_english_audio`, or None) drives English-audio preference for
    MULTi rips."""
    input_str = str(input_path)
    # Decide whether the GPU can decode this source; falls back to CPU decode
    # for codecs the iHD VLD engine doesn't support (e.g. AV1) or if ffprobe fails.
    hw_decode = (
        USE_VAAPI and USE_VAAPI_DECODE
        and _probe_video_codec(input_str) in HWACCEL_DECODE_CODECS
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if USE_VAAPI:
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
    # Map: video, English-preferred audio. -sn drops every subtitle track;
    # without it ffmpeg's HLS muxer auto-maps them all — Superbad's 8 PGS
    # subs alone push CPU to ~1000%.
    audio_map = f"0:a:{audio_idx}" if audio_idx is not None else "0:a:0?"
    cmd += [
        "-map", "0:v:0",
        "-map", audio_map,
        "-sn",
    ]
    if USE_VAAPI:
        # When hw_decode is on, frames are already in vaapi format on the GPU,
        # so scale_vaapi runs entirely on the GPU. Otherwise hwupload moves
        # CPU-decoded frames onto the GPU before encode.
        # This iGPU lacks VAEntrypointVideoProc, so scale_vaapi can't run. The
        # hwdownload variant keeps decode on the GPU (the expensive part for
        # 4K HEVC), pulls frames to system memory just for the cheaper scale,
        # then re-uploads for the GPU encoder. format=nv12|p010le accepts
        # 8-bit and Main10 sources; the trailing format=nv12 forces 8-bit
        # output for h264_vaapi (Main profile).
        vf = (
            "hwdownload,format=nv12|p010le,scale=-2:1080,format=nv12,hwupload"
            if hw_decode else
            "scale=-2:1080,format=nv12,hwupload"
        )
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
        cmd += [
            "-vf", "scale=-2:1080",
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


def _pick_random_from_library() -> dict | None:
    """Walk MEDIA_ROOT and return a random playable file as a source dict, or None
    if the library has no playable videos."""
    candidates: list[Path] = []
    try:
        for root, _dirs, files in os.walk(MEDIA_ROOT):
            for name in files:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in VIDEO_EXTS:
                    candidates.append(Path(root) / name)
    except Exception as e:
        print(f"library scan failed: {e}", file=sys.stderr)
        return None
    if not candidates:
        return None
    pick = random.choice(candidates)
    return {
        "type": "file",
        "ref": str(pick.relative_to(MEDIA_ROOT)),
        "title": pick.name,
        "duration": None,
        "is_live": False,
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
                continue
            # Holding a paused position — leave it alone.
            if paused:
                continue
            # If a process exited, make sure user action hasn't superseded us.
            if proc is not None:
                with state_lock:
                    if current_proc is not proc:
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
    if source["type"] == "file":
        full_path = _safe_resolve(source["ref"], must_be_file=True)
        ffmpeg_input = str(full_path)
        # Always re-probe in case the file changed; cheap.
        if source.get("duration") is None:
            source = {**source, "duration": _probe_duration(full_path)}
    elif source["type"] == "url":
        resolved = _resolve_url(source["ref"])
        ffmpeg_input = resolved["stream_url"]
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
    with state_lock:
        global active_run_id, next_run_id
        # Retire the old run (its segments stay on disk + in the master until
        # they roll out of the global window — that's what makes the
        # transition seamless).
        _terminate_proc_locked()
        if active_run_id is not None:
            finished_run_ids.add(active_run_id)
        run_id = next_run_id
        next_run_id += 1
        run_dir = _run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = _build_ffmpeg_cmd(ffmpeg_input, run_dir, start_seconds, audio_idx)
        current_proc = subprocess.Popen(cmd)
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
threading.Thread(target=_composer_thread, daemon=True, name="livestream-composer").start()
threading.Thread(target=_watcher, daemon=True, name="livestream-watcher").start()


@app.before_request
def _gate_viewer_routes():
    p = request.path
    if p.startswith("/admin"):
        return None  # Traefik handles admin auth
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


@app.route("/")
def viewer_page():
    return send_from_directory("static", "viewer.html")


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
def api_browse():
    return jsonify(_list_dir(request.args.get("path", "")))


@app.route("/admin/api/play", methods=["POST"])
def api_play():
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path required"}), 400
    start = float(data.get("start_seconds") or 0)
    source = {
        "type": "file",
        "ref": path,
        "title": Path(path).name,
        "duration": None,
        "is_live": False,
    }
    _start_stream(source, start_seconds=start)
    return jsonify({"ok": True, "path": path, "start_seconds": start})


@app.route("/admin/api/play_url", methods=["POST"])
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
            {"id": t["id"], "label": t["label"], "created": t.get("created")}
            for t in tokens
            if t.get("id") != ADMIN_TOKEN_ID
        ])


@app.route("/admin/api/tokens", methods=["POST"])
def api_create_token():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label required"}), 400
    new = {"id": secrets.token_urlsafe(12), "label": label, "created": time.time()}
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
        active = [(ip, ts) for ip, ts in viewers.items() if ts >= cutoff]
    # Build labels outside viewers_lock to avoid nesting locks on the hot path.
    for ip, ts in active:
        with _geo_cache_lock:
            loc = _geo_cache.get(ip, "?")
        out.append({"ip": ip, "loc": loc, "last_seen": ts})
    out.sort(key=lambda v: -v["last_seen"])
    return jsonify(out)


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
def api_queue_list():
    with playlist_lock:
        return jsonify(list(playlist))


@app.route("/admin/api/queue", methods=["POST"])
def api_queue_add():
    data = request.get_json(silent=True) or {}
    t = data.get("type")
    if t == "file":
        path = data.get("path")
        if not path:
            return jsonify({"error": "path required"}), 400
        try:
            _safe_resolve(path, must_be_file=True)
        except Exception:
            return jsonify({"error": "file not found"}), 400
        items = [{
            "type": "file", "ref": path, "title": Path(path).name,
            "duration": None, "is_live": False,
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


@app.route("/admin/api/queue/<int:idx>", methods=["DELETE"])
def api_queue_remove(idx: int):
    with playlist_lock:
        if not (0 <= idx < len(playlist)):
            return jsonify({"error": "index out of range"}), 400
        playlist.pop(idx)
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/queue/clear", methods=["POST"])
def api_queue_clear():
    with playlist_lock:
        playlist.clear()
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/queue/shuffle", methods=["POST"])
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
def api_queue_move(idx: int):
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")  # "up" or "down"
    with playlist_lock:
        n = len(playlist)
        if not (0 <= idx < n):
            return jsonify({"error": "index out of range"}), 400
        if direction == "up" and idx > 0:
            playlist[idx - 1], playlist[idx] = playlist[idx], playlist[idx - 1]
        elif direction == "down" and idx < n - 1:
            playlist[idx + 1], playlist[idx] = playlist[idx], playlist[idx + 1]
        else:
            return jsonify({"ok": True})
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/admin/api/seek", methods=["POST"])
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
def api_stop():
    with state_lock:
        _stop_locked()
    # _cleanup_hls acquires its own locks — must call outside state_lock.
    _cleanup_hls()
    _clear_state()
    return jsonify({"ok": True})


@app.route("/admin/api/skip", methods=["POST"])
def api_skip():
    """Skip to the next item: terminate ffmpeg without clearing source state and
    let the watcher pick the next thing (queue first, then auto_fill if on)."""
    global current_paused, active_run_id
    with state_lock:
        if current_source is None:
            return jsonify({"error": "nothing playing"}), 400
        _terminate_proc_locked()
        # Retire the run so the next _start_stream draws an EXT-X-DISCONTINUITY
        # boundary in the master.
        if active_run_id is not None:
            finished_run_ids.add(active_run_id)
            active_run_id = None
        # Un-pause so the watcher's `if paused: continue` doesn't block advance.
        current_paused = False
    return jsonify({"ok": True})


@app.route("/admin/api/pause", methods=["POST"])
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
        })


@app.route("/hls/<path:filename>")
def hls(filename):
    _track_viewer()
    resp = send_from_directory(HLS_DIR, filename, conditional=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
