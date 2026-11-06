FROM python:3.12-slim

# Archive listers (nothing is ever extracted): unrar (official RarLab tool, Debian non-free) for .rar incl.
# multi-part sets, 7-Zip (+ its RAR plugin) for .7z/.zip and split sets, unzip, GNU tar with xz/bzip2 for tarballs.
# (Alpine's 7-Zip is built without RAR support, so the image is Debian based.)
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends unrar 7zip 7zip-rar unzip xz-utils bzip2 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && unrar | head -2 && 7z i | grep -qi rar && unzip -v | head -1 && tar --version | head -1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Set by the publish workflow (GitHub Actions run number, commit, time); shown in the web UI
ARG BUILD_NUMBER=dev
ARG BUILD_SHA=
ARG BUILD_DATE=
ENV BUILD_NUMBER=$BUILD_NUMBER BUILD_SHA=$BUILD_SHA BUILD_DATE=$BUILD_DATE

ENV DATA_DIR=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)" || exit 1

CMD ["sh", "-c", "exec waitress-serve --listen=0.0.0.0:${PORT} --threads=8 main:app"]
