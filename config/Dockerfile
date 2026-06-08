# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG TANDEM_RELEASE_VERSION=latest
ARG TANDEM_ENGINE_RELEASE_VERSION=${TANDEM_RELEASE_VERSION}
ARG TANDEM_ENGINE_PACKAGE=@frumu/tandem

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

RUN groupadd --gid "${HOST_GID}" tandem \
  && useradd --uid "${HOST_UID}" --gid "${HOST_GID}" --create-home --home-dir /home/node tandem \
  && mkdir -p /home/node/npm \
  && chown -R "${HOST_UID}:${HOST_GID}" /home/node

RUN --mount=type=cache,target=/root/.npm \
    npm install -g "${TANDEM_ENGINE_PACKAGE}@${TANDEM_ENGINE_RELEASE_VERSION}" pnpm@10.23.0

RUN mkdir -p /workspace/tandem-agents/runs /workspace/repos /workspace/tandem-data

ENV PATH="/home/node/npm/bin:${PATH}"

COPY --chown=${HOST_UID}:${HOST_GID} requirements.txt /workspace/tandem-agents/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /workspace/tandem-agents/requirements.txt

WORKDIR /workspace/tandem-agents

COPY --chown=${HOST_UID}:${HOST_GID} src/tandem_agents /workspace/tandem-agents/src/tandem_agents
COPY --chown=${HOST_UID}:${HOST_GID} pyproject.toml /workspace/tandem-agents/pyproject.toml
COPY --chown=${HOST_UID}:${HOST_GID} requirements.txt /workspace/tandem-agents/requirements.txt
COPY --chown=${HOST_UID}:${HOST_GID} config/agent.yaml /workspace/tandem-agents/config/agent.yaml
COPY --chown=${HOST_UID}:${HOST_GID} config/board.yaml /workspace/tandem-agents/config/board.yaml
COPY --chown=${HOST_UID}:${HOST_GID} scripts/run.sh /workspace/tandem-agents/scripts/run.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/monitor.sh /workspace/tandem-agents/scripts/monitor.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/entrypoint.sh /workspace/tandem-agents/scripts/entrypoint.sh
COPY --chown=${HOST_UID}:${HOST_GID} scripts/tandem-engine-serve.sh /workspace/tandem-agents/scripts/tandem-engine-serve.sh
RUN chmod +x /workspace/tandem-agents/scripts/run.sh /workspace/tandem-agents/scripts/monitor.sh /workspace/tandem-agents/scripts/entrypoint.sh /workspace/tandem-agents/scripts/tandem-engine-serve.sh

COPY LICENSE /LICENSE
COPY NOTICE /NOTICE
RUN chown -R "${HOST_UID}:${HOST_GID}" /home/node /workspace/tandem-agents /workspace/repos /workspace/tandem-data

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Tandem Agents" \
      org.opencontainers.image.description="Tandem Agents portable autonomous coding runtime" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 39735

USER ${HOST_UID}:${HOST_GID}

CMD ["/workspace/tandem-agents/scripts/entrypoint.sh"]
