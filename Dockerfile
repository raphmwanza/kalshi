FROM python:3.12-slim-bookworm

WORKDIR /app

# Worker process: the tennis loop does not listen on a port. Logs go to stdout
# (PYTHONUNBUFFERED). Configuration comes from environment variables only.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    KALSHI_PRIVATE_KEY_PATH=/app/kalshi_private_key.pem

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cli.py beast_mode_bot.py beast_mode_dashboard.py ./
COPY src ./src
COPY docker-entrypoint.sh ./

RUN chmod +x docker-entrypoint.sh \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENTRYPOINT ["./docker-entrypoint.sh"]
