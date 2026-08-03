#!/usr/bin/env bash
#
# Set up a local jetstream dev environment — venv, frontend bundle, and a set
# of generated media fixtures — so the app can be run and clicked through
# outside Docker.
#
# Everything lands in .devenv/ (gitignored). Safe to re-run: existing fixtures
# are left alone, so a re-run costs seconds rather than re-encoding video.
#
#   bin/dev-setup.sh      then      bin/dev-server.py
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
DEV="$ROOT/.devenv"
MEDIA="$DEV/media"
VENV="$DEV/venv"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn(){ printf '    \033[33m!!\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- prereqs ---
say "Checking prerequisites"
missing=0
for tool in python3 ffmpeg ffprobe; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool ($(command -v "$tool"))"
  else
    warn "$tool NOT FOUND — required"
    missing=1
  fi
done
if command -v npm >/dev/null 2>&1; then
  ok "npm ($(npm --version))"
  HAVE_NPM=1
else
  warn "npm not found — skipping the frontend bundle. /admin's Svelte islands
       will 404, but every page still renders (they're progressive)."
  HAVE_NPM=0
fi
[ "$missing" -eq 0 ] || { echo; echo "Install the missing tools and re-run."; exit 1; }

# ------------------------------------------------------------------ venv ----
say "Python environment"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  ok "created $VENV"
else
  ok "reusing $VENV"
fi
# flask is the app's only third-party import. yt-dlp and gunicorn are prod
# runtime extras (yt-dlp is shelled out to, not imported) — not needed to
# render pages or drive the local ffmpeg pipeline.
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet flask
ok "flask $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("flask"))')"

# -------------------------------------------------------------- frontend ----
if [ "$HAVE_NPM" -eq 1 ]; then
  say "Frontend bundle"
  if [ ! -d node_modules ]; then
    npm ci --silent
    ok "npm ci"
  else
    ok "node_modules present"
  fi
  npm run build --silent >/dev/null
  ok "static/build/ written"
fi

# ------------------------------------------------------------- fixtures ----
say "Media fixtures"
mkdir -p "$MEDIA/movies" "$MEDIA/tv/Severance/Season 1" "$DEV/data" "$DEV/hls"

subs_dir="$DEV/.subs"
mkdir -p "$subs_dir"
cat > "$subs_dir/en.srt" <<'SRT'
1
00:00:01,000 --> 00:00:12,000
English subtitles are burned in by the encoder.

2
00:00:12,000 --> 00:00:30,000
Switching tracks restarts ffmpeg at this position.
SRT
cat > "$subs_dir/fr.srt" <<'SRT'
1
00:00:01,000 --> 00:00:12,000
Les sous-titres sont incrustes par l'encodeur.

2
00:00:12,000 --> 00:00:30,000
Changer de piste redemarre ffmpeg a cet endroit.
SRT

# make_clip <path> <seconds> [hdr]
# Two subtitle tracks (eng + fre) so the CC pickers on /library and /controls
# have something real to list, and so "off" can be told apart from "track 0".
make_clip() {
  local out="$1" secs="$2" hdr="${3:-}"
  if [ -s "$out" ]; then ok "$(basename "$out") (exists)"; return; fi
  local -a pix=(-pix_fmt yuv420p)
  local -a filt=()
  if [ -n "$hdr" ]; then
    # BT.2020 + PQ so _probe_media reports is_hdr and the zscale tonemap
    # branch of _build_ffmpeg_cmd is exercised.
    #
    # This MUST be done with the `setparams` filter, not the `-color_trc` /
    # `-color_primaries` output options. lavfi sources emit frames with
    # unspecified colour properties, and the encoder writes what the *frames*
    # declare — so the output options are silently dropped and ffprobe reports
    # `color_transfer=unknown`. app.py keys HDR detection on exactly that
    # field (HDR_TRANSFERS), so the fixture would look like plain SDR.
    pix=(-pix_fmt yuv420p10le)
    filt=(-vf "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc")
  fi
  ffmpeg -y -v error \
    -f lavfi -i "testsrc2=size=640x360:rate=24:duration=$secs" \
    -f lavfi -i "sine=frequency=440:duration=$secs" \
    -i "$subs_dir/en.srt" -i "$subs_dir/fr.srt" \
    -map 0:v -map 1:a -map 2 -map 3 \
    -c:v libx264 -preset ultrafast "${pix[@]}" "${filt[@]}" \
    -c:a aac -c:s srt \
    -metadata:s:s:0 language=eng -metadata:s:s:1 language=fre \
    "$out"
  if [ -n "$hdr" ]; then
    local trc
    trc=$(ffprobe -v error -select_streams v:0 \
            -show_entries stream=color_transfer -of csv=p=0 "$out")
    [ "$trc" = "smpte2084" ] \
      && ok "$(basename "$out") (${secs}s, HDR/PQ verified)" \
      || warn "$(basename "$out") wanted HDR but color_transfer=$trc"
    return
  fi
  ok "$(basename "$out") (${secs}s)"
}

# Short clip: fast end-to-end runs, and exercises the source-transition path
# (it ends quickly, so the watcher/composer hand-off happens while you watch).
make_clip "$MEDIA/movies/The Matrix (1999) 1080p BluRay x264.mkv" 30
# Long clip: long enough to click around /controls without it ending under you.
make_clip "$MEDIA/movies/Spirited Away (2001) 1080p.mkv" 600
# HDR: the VOD HDR path has never been exercised on real hardware (see ROADMAP).
make_clip "$MEDIA/movies/Arrival (2016) 2160p HDR WEB-DL.mkv" 45 hdr
make_clip "$MEDIA/tv/Severance/Season 1/Severance S01E01 1080p.mkv" 20
make_clip "$MEDIA/tv/Severance/Season 1/Severance S01E02 1080p.mkv" 20

say "Done"
cat <<EOF

  Start the dev server:

      bin/dev-server.py

  It prints the seeded invite codes and login before it serves. State lives in
  .devenv/ — delete that directory for a clean slate (fixtures included).
EOF
