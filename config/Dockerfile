# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG TANDEM_RELEASE_VERSION=latest
ARG TANDEM_ENGINE_RELEASE_VERSION=${TANDEM_RELEASE_VERSION}
ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_PREFIX=/home/node/npm \
    npm_config_update_notifier=false

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    nodejs \
    npm \
    jq \
    openssh-client \
  && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.npm \
    npm install -g "@frumu/tandem@${TANDEM_ENGINE_RELEASE_VERSION}"

RUN mkdir -p /workspace/aca/runs /workspace/repos /workspace/tandem-data

ENV PATH="/home/node/npm/bin:${PATH}"

COPY --chown=${HOST_UID}:${HOST_GID} requirements.txt /workspace/aca/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /workspace/aca/requirements.txt

WORKDIR /workspace/aca

COPY --chown=${HOST_UID}:${HOST_GID} src/aca /workspace/aca/src/aca
COPY --chown=${HOST_UID}:${HOST_GID} pyproject.toml /workspace/aca/pyproject.toml
COPY --chown=${HOST_UID}:${HOST_GID} requirements.txt /workspace/aca/requirements.txt
COPY --chown=${HOST_UID}:${HOST_GID} config/agent.yaml /workspace/aca/config/agent.yaml
COPY --chown=${HOST_UID}:${HOST_GID} config/board.yaml /workspace/aca/config/board.yaml
COPY --chown=${HOST_UID}:${HOST_GID} scripts/run.sh /workspace/aca/scripts/run.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/monitor.sh /workspace/aca/scripts/monitor.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/entrypoint.sh /workspace/aca/scripts/entrypoint.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/tandem-engine-serve.sh /workspace/aca/scripts/tandem-engine-serve.sh
RUN chmod +x /workspace/aca/scripts/run.sh /workspace/aca/scripts/monitor.sh /workspace/aca/scripts/entrypoint.sh /workspace/aca/scripts/tandem-engine-serve.sh

COPY LICENSE /LICENSE
COPY NOTICE /NOTICE

LABEL org.opencontainers.image.title="ACA" \
      org.opencontainers.image.description="ACA portable auto-coder runtime" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 39735

CMD ["/workspace/aca/scripts/entrypoint.sh"]
