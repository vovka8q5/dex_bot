FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEXBOT_LOG_FILE=/app/data/logs/dexbot.log

WORKDIR /app

RUN useradd --create-home --uid 10001 dexbot \
    && mkdir -p /app/data/logs \
    && chown -R dexbot:dexbot /app

COPY pyproject.toml README.md ./
COPY config ./config
COPY dexbot ./dexbot

RUN pip install --no-cache-dir . \
    && chown -R dexbot:dexbot /app

USER dexbot

# Hourly loop. DEXBOT_MODE in .env selects paper or Bybit demo.
CMD ["python", "-m", "dexbot", "run", "--config", "/app/config/default.yaml"]
