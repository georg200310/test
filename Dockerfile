FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    MAX_CONCURRENT_STREAMS=2 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
ARG BGUTIL_VERSION=2.0.2
RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc

RUN python3 -m venv /opt/venv
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chmod +x /app/start.sh \
    && mkdir -p /app/media_cache

EXPOSE 10000
CMD ["/app/start.sh"]
