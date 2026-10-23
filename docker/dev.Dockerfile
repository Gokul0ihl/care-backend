FROM python:3.13-slim-bookworm

ARG APP_HOME=/app

WORKDIR $APP_HOME

RUN apt-get update && apt-get install --no-install-recommends -y \
  build-essential libjpeg-dev zlib1g-dev libgmp-dev \
  libpq-dev libpangoft2-1.0-0 gettext wget curl gnupg git \
  libharfbuzz0b libharfbuzz-subset0 libffi-dev libjpeg-dev libopenjp2-7-dev \
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*

# The venv lives outside $APP_HOME, which compose bind-mounts from the host.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/.venv \
  UV_CACHE_DIR=/root/.cache/uv \
  UV_PYTHON_DOWNLOADS=never \
  UV_LINK_MODE=copy
ENV PATH=/.venv/bin:$PATH

COPY pyproject.toml uv.lock $APP_HOME/
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --all-groups --no-install-project

ARG ADDITIONAL_PLUGS=""
ENV ADDITIONAL_PLUGS=$ADDITIONAL_PLUGS

COPY . $APP_HOME/

RUN --mount=type=cache,target=/root/.cache/uv python3 $APP_HOME/install_plugins.py

HEALTHCHECK \
  --interval=10s \
  --timeout=5s \
  --start-period=10s \
  --retries=48 \
  CMD ["./scripts/healthcheck.sh"]
