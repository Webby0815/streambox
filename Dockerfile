FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN printf 'deb http://deb.debian.org/debian bookworm main\n\
deb http://deb.debian.org/debian bookworm-updates main\n\
deb http://deb.debian.org/debian-security bookworm-security main\n' > /etc/apt/sources.list \
    && apt-get clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        ca-certificates \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/streambox

COPY requirements.txt .

RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir -U "yt-dlp[default,curl-cffi]" \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p \
    /srv/streambox/data/downloads \
    /srv/streambox/data/music \
    /srv/streambox/data/thumbnails \
    /srv/streambox/data/posters \
    /srv/streambox/data/subtitles \
    /srv/streambox/data/trash \
    /srv/streambox/data/backups \
    /srv/streambox/cookies

EXPOSE 8080
CMD ["python", "-m", "app.app"]
