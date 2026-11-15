# Groovehound: one container, one process.
#   docker build -t groovehound .
#   docker run -p 8080:8080 -v groovehound-data:/data -e GROOVEHOUND_CONTACT=you@example.com groovehound

# Base images are pinned by digest; Dependabot keeps the digests current (.github/dependabot.yml).
FROM ghcr.io/astral-sh/uv:0.13@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

# --- Build: install the locked dependencies and the app into /app/.venv -----------------
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS build
COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Runtime -----------------------------------------------------------------------------
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

LABEL org.opencontainers.image.title="Groovehound" \
      org.opencontainers.image.description="Wishlist, new releases and recommendations from MusicBrainz and ListenBrainz" \
      org.opencontainers.image.source="https://github.com/emilystap/groovehound" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ARG UID=568
ARG GID=568
RUN groupadd --gid "${GID}" groovehound \
    && useradd --uid "${UID}" --gid "${GID}" --no-create-home --shell /usr/sbin/nologin groovehound \
    && mkdir -p /data /config \
    && chown "${UID}:${GID}" /data /config

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    GROOVEHOUND_DB=/data/groovehound.db \
    TZ=UTC

USER ${UID}:${GID}
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["groovehound", "serve", "--host", "0.0.0.0", "--port", "8080"]
