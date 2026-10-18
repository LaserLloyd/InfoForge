# syntax=docker/dockerfile:1.7
# =============================================================================
#  InfoForge -- slim CLI image
# =============================================================================
#  Build:  docker build -t infoforge:dev .      (or: podman build -t infoforge:dev .)
#  Run:    docker run --rm -v ./config.yaml:/config/config.yaml:ro infoforge:dev doctor
#
#  InfoForge is a plain subprocess-per-call CLI, not a daemon: this image has an
#  ENTRYPOINT of `infoforge` and no HEALTHCHECK. The long-running piece is the
#  kiwix-serve container it talks to -- deploy/compose/compose.yml pairs the two.
#
#  Inside a container `infoforge serve ...` (podman lifecycle) is NOT used --
#  there is no podman here. Point `kiwix_url` at the kiwix-serve service instead.
#
#  Same builder/runtime pairing rule as DisPatch's Dockerfile: the uv image and
#  the runtime image share a Python at the same absolute path, so the venv's
#  interpreter symlink resolves after the copy (astral-sh/uv#7758).
# =============================================================================

FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
# README.md + LICENSE are named by pyproject.toml; hatchling needs them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project
COPY src/ ./src/
# --reinstall-package: guard against uv serving the project's OWN previously built
# wheel from the cache mount (uv only invalidates it when pyproject.toml changes; seen
# in another repo's image). Not reproducible with uv 0.13.1 in this layout (a control
# build without the flag picked up the edit), but it costs one wheel build and removes
# the question. Verified: rebuild after a one-line source change, grep the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --reinstall-package infoforge
RUN find /app/.venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /app/.venv -name '*.pyc' -delete


FROM python:3.13-slim-trixie AS runtime

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED

LABEL org.opencontainers.image.title="InfoForge" \
      org.opencontainers.image.description="Grounded answers from offline Kiwix ZIM archives with a local LLM" \
      org.opencontainers.image.source="https://github.com/LaserLloyd/InfoForge" \
      org.opencontainers.image.documentation="https://github.com/LaserLloyd/InfoForge/blob/main/docs/deploy/docker.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# UID/GID 1000 so a bind-mounted ZIM directory owned by the person running
# compose is readable without ceremony.
RUN groupadd --gid 1000 infoforge \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin infoforge \
 && install -d -o infoforge -g infoforge -m 0755 /config /zims /state

COPY --from=builder --chown=infoforge:infoforge /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INFOFORGE_CONFIG=/config/config.yaml \
    INFOFORGE_LOG=/state/log.jsonl

USER infoforge

# Exec form so signals and exit codes pass straight through: InfoForge's exit
# codes (0/2/3/4/5/6) are the contract agents script against, and `doctor`
# exits 1 on any failed check.
ENTRYPOINT ["infoforge"]
CMD ["--help"]
