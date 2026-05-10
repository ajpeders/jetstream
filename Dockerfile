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

RUN pip install --no-cache-dir flask==3.0.3 yt-dlp

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
