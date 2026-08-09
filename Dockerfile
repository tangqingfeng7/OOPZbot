FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BOT_REDIS_HOST=redis

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

ARG BOT_UID=1000
ARG BOT_GID=1000
RUN test "${BOT_UID}" -gt 0 && test "${BOT_GID}" -gt 0 \
    && groupadd --non-unique --gid "${BOT_GID}" bot \
    && useradd --non-unique --uid "${BOT_UID}" --gid "${BOT_GID}" \
        --home-dir /home/bot --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /home/bot/.cache /home/bot/.config \
        /home/bot/.local/share /home/bot/.runtime \
    && chown -R bot:bot /home/bot \
    && chmod 700 /home/bot/.runtime

ENV HOME=/home/bot \
    XDG_CACHE_HOME=/home/bot/.cache \
    XDG_CONFIG_HOME=/home/bot/.config \
    XDG_DATA_HOME=/home/bot/.local/share \
    XDG_RUNTIME_DIR=/home/bot/.runtime

COPY main.py /app/main.py
COPY config.example.py /app/config.example.py
COPY private_key.example.py /app/private_key.example.py
COPY README.md /app/README.md
COPY src /app/src
COPY plugins /app/plugins
COPY tools /app/tools
COPY docs /app/docs
COPY config /app/config

RUN ln -s /app/config/runtime.py /app/config.py \
    && ln -s /app/config/private_key.py /app/private_key.py \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app/data /app/logs

USER bot

CMD ["python", "main.py"]
