FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 10001 dexbot

COPY pyproject.toml README.md ./
COPY config ./config
COPY dexbot ./dexbot

RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown -R dexbot:dexbot /app

USER dexbot

# Бумажный цикл раз в час. Ордера на mainnet образ сам не включает.
CMD ["python", "-m", "dexbot", "run", "--config", "/app/config/default.yaml"]
