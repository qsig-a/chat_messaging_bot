# ---- build stage: install deps into a venv ----
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt is a pip-compile lock: exact versions + hashes for every
# transitive dep. --require-hashes makes the build fail loudly rather than
# silently drift if the lock is ever hand-edited or a hash doesn't match.
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim AS runtime

# curl is only here for the container HEALTHCHECK below; drop it if you don't want it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. /data is chowned to it below, and a named volume inherits
# those permissions when Docker first initialises it.
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/bridge.sqlite3 \
    BIND_HOST=0.0.0.0 \
    BIND_PORT=8080

WORKDIR /app
COPY sms_bridge/ ./sms_bridge/

# SQLite lives here; mounted as a named volume in compose.
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "curl -fsS http://127.0.0.1:8080/healthz || exit 1"]

ENTRYPOINT ["python", "-m", "sms_bridge"]
