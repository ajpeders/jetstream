FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      vainfo \
      intel-media-va-driver \
      i965-va-driver \
      libva2 \
      libva-drm2 \
 && rm -rf /var/lib/apt/lists/*

ENV LIBVA_DRIVER_NAME=iHD

RUN pip install --no-cache-dir flask==3.0.3 yt-dlp gunicorn==23.0.0

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

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
