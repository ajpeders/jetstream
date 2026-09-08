FROM node:26-slim AS ui-build

WORKDIR /ui
COPY package.json package-lock.json vite.config.js angular.json tsconfig.json tsconfig.app.json tsconfig.react.json vite.public.config.js /ui/
COPY src /ui/src
RUN npm ci
# `static/` must be present before the build: src/jetstream-theme.css does
# `@import "../static/jetstream-custom.css"`, so Tailwind cannot resolve its
# own entrypoint without it. CI never caught this because CI runs the same
# `npm run build` against a full checkout, where static/ is simply there —
# only the container build, which copies just `src`, was missing it.
# Placed AFTER `npm ci` so editing a stylesheet doesn't invalidate that layer.
COPY static /ui/static
RUN npm run build

FROM python:3.12-slim

# `git` is here for the `pip install git+https://…/companion.git` step below:
# pip shells out to the git binary for VCS URLs and python:3.12-slim does not
# ship one, so without it the image build dies with "Cannot find command
# 'git'" — which is exactly what blocked every deploy from 2026-08-04 onward.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      vainfo \
      intel-media-va-driver \
      i965-va-driver \
      libva2 \
      libva-drm2 \
      git \
 && rm -rf /var/lib/apt/lists/*

ENV LIBVA_DRIVER_NAME=iHD

# Everything pinned (werkzeug explicitly — it floats behind the flask pin
# otherwise) so image rebuilds are reproducible; a dep bump is a reviewable
# diff here, not whatever upstream shipped that day.
RUN pip install --no-cache-dir flask==3.0.3 werkzeug==3.1.8 yt-dlp==2026.7.4 gunicorn==23.0.0

# LLM provider seam for the auto_fill content gate. Separate layer from the
# pinned core deps so a companion change rebuilds only this step, and because
# it is the one dependency fetched over the network from our own Forgejo —
# keeping it last means a Forgejo outage can't invalidate the layers above.
#
# app.py imports this behind a try/except: if the install is ever removed or
# fails at runtime-import, the content gate degrades to "never judges" (and,
# being fail-closed, auto_fill just stays quiet) instead of the container
# crashing on boot.
ARG COMPANION_REF=main
# Parameterised because CI cannot use the public hostname: Forgejo Actions jobs
# run on the `ci-jobs` network, which has no hairpin route back to
# git.thelunadog.com (verified — it times out, while http://forgejo:3000
# answers). The default is unchanged, so host builds behave exactly as before;
# the CI image-build job overrides it with the internal URL.
ARG COMPANION_URL=https://git.thelunadog.com/alex/companion.git
RUN pip install --no-cache-dir "git+${COMPANION_URL}@${COMPANION_REF}"

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
COPY --from=ui-build /ui/static/build /app/static/build
COPY --from=ui-build /ui/static/build-admin /app/static/build-admin
COPY --from=ui-build /ui/static/build-public /app/static/build-public

EXPOSE 8080
# gunicorn replaces the dev-server Werkzeug. One worker (the app's background
# threads — watcher, composer — must share the same in-process state); a
# pool of gthread workers handles the concurrent /hls/* segment fetches that
# Werkzeug was struggling with at scale. --timeout 120 covers slow /admin/api
# operations (yt-dlp resolution can run 30-60s for some sites).
CMD ["gunicorn", "-b", "0.0.0.0:8080", \
     "-w", "1", \
     "-k", "gthread", \
     "--threads", "16", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--access-logformat", "%(h)s - %(t)s \"%(r)s\" %(s)s %(b)s %(L)ss", \
     "app:app"]
