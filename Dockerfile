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
COPY ./pymix ./pymix

# ---------- Non-root user ----------
RUN useradd -u 1000 -m deploy
USER deploy

# ---------- Run ----------
CMD python /app/pymix/runner.py -e $APP_ENVIRONMENT