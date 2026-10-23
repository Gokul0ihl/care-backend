FROM python:3.13-slim-bookworm AS base

ARG APP_HOME=/app

ARG BUILD_ENVIRONMENT="production"

WORKDIR $APP_HOME

ENV BUILD_ENVIRONMENT=$BUILD_ENVIRONMENT
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH=$APP_HOME/.venv/bin:$PATH
ENV HOME=$APP_HOME


# ---
FROM base AS builder

RUN apt-get update && apt-get install --no-install-recommends -y \
  build-essential libjpeg-dev zlib1g-dev libgmp-dev libpq-dev git wget \
  libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0 libffi-dev libopenjp2-7-dev \
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*

# uv installs the locked dependencies into $APP_HOME/.venv, using the
# image's own Python (the runtime stage has the same one).
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=$APP_HOME/.venv \
  UV_PYTHON_DOWNLOADS=never \
  UV_COMPILE_BYTECODE=1 \
  UV_NO_CACHE=1

COPY pyproject.toml uv.lock $APP_HOME/
RUN uv sync --locked --no-default-groups --no-install-project

COPY plugs/ $APP_HOME/plugs/
COPY install_plugins.py plug_config.py $APP_HOME/

ARG ADDITIONAL_PLUGS=""
ENV ADDITIONAL_PLUGS=$ADDITIONAL_PLUGS
RUN python3 $APP_HOME/install_plugins.py

# ---
FROM base AS runtime

RUN addgroup --system django \
  && adduser --system --ingroup django django

RUN apt-get update && apt-get install --no-install-recommends -y \
  libpq-dev libgmp-dev libpangoft2-1.0-0 gettext wget curl gnupg \
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*

RUN chown django:django $APP_HOME

COPY --from=builder --chown=django:django $APP_HOME/.venv $APP_HOME/.venv

ARG APP_VERSION="unknown"
ENV APP_VERSION=$APP_VERSION

COPY --chmod=0755 --chown=django:django ./scripts/*.sh $APP_HOME

COPY --chown=django:django . $APP_HOME

USER django

HEALTHCHECK \
  --interval=30s \
  --timeout=5s \
  --start-period=10s \
  --retries=12 \
  CMD ["./healthcheck.sh"]

EXPOSE 9000
