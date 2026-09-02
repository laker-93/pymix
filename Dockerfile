FROM python:3.11-slim

# ---------- Env ----------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8002 \
    PYTHONPATH=/app

# ---------- Install Docker CLI + Compose plugin ----------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/debian \
      $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
    && apt-get update && \
    apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
        libtag1-dev \
        vim \
    && rm -rf /var/lib/apt/lists/*
# ---------- App dir ----------
WORKDIR /app

# ---------- Install deps first (cache-friendly) ----------
COPY ./requirements.txt ./requirements.txt
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ---------- Copy app ----------
COPY ./alembic.ini ./alembic.ini
COPY ./pymix ./pymix

# ---------- Non-root user ----------
# The local dev compose overrides `user:` to the host UID (e.g. 501:20) so
# bind-mounted volumes come out host-owned -- that UID has no /etc/passwd
# entry in the container, so $HOME defaults to "/". Any code that touches
# beets' global config (e.g. rekordbox_xml_controller._tag_duplicate_paths's
# in-process `Item.write()`) makes confuse try to `os.makedirs('/.config')`
# and crashes with PermissionError, silently failing a whole watch-dir/
# rekordbox import the moment it contains even one duplicate track (laker-93/pymix#147).
# BEETSDIR pins beets' config dir to a location that's pre-created and
# world-writable, so this resolves cleanly under *any* runtime UID.
RUN mkdir -p /tmp/pymix-beetsdir && chmod 1777 /tmp/pymix-beetsdir
ENV BEETSDIR=/tmp/pymix-beetsdir
RUN useradd -u 1000 -m deploy
USER deploy

# ---------- Run ----------
# Exec form so python is PID 1. With the shell form, `sh` is PID 1 and python a
# child: a kernel OOM kill of python left sh to print "Killed" and exit 0, so
# Docker recorded OOMKilled=false / ExitCode=0 and the kill was invisible to
# `docker inspect` (laker-93/pymix#81). Exec form also delivers SIGTERM straight
# to python, so shutdown is graceful instead of a 10s timeout.
CMD ["sh", "-c", "exec python /app/pymix/runner.py -e $APP_ENVIRONMENT"]