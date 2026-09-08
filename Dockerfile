FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Jerusalem
ENV DJANGO_SETTINGS_MODULE=sefaria.settings
ENV MONGO_HOST=mongodb
ENV MONGO_PORT=27017
ENV MONGO_DB_NAME=sefaria
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONUNBUFFERED=1

# Every network call in a build layer is a single point of failure for the whole
# weekly cycle: a Launchpad 504 inside `add-apt-repository` once killed one four
# minutes in.  apt-get retries through Acquire::Retries, everything else through
# this wrapper.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'for attempt in 1 2 3; do' \
    '  "$@" && exit 0' \
    '  status=$?' \
    '  echo "retry $attempt/3 failed (exit $status): $*" >&2' \
    '  [ "$attempt" -lt 3 ] || exit "$status"' \
    '  sleep $((attempt * 5))' \
    'done' \
    'exit 1' > /usr/local/bin/retry && chmod +x /usr/local/bin/retry

# Install base system dependencies.  24.04 (noble) ships Python 3.12 itself, so
# there is no deadsnakes PPA — and therefore no software-properties-common, no
# gpg-agent and no Launchpad round-trip — in this layer at all.
RUN apt-get -o Acquire::Retries=3 update -y && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    aria2 \
    ca-certificates \
    tar \
    zstd \
    wget \
    netcat-openbsd \
    git \
    curl \
    jq \
    unzip \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    libre2-dev \
    pybind11-dev \
    build-essential \
    cmake \
    ninja-build \
    libpq-dev \
    sudo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 24.04 marks the system interpreter externally managed (PEP 668), so the
# pipeline installs into its own virtualenv instead of fighting apt.  It is
# first on PATH, so the plain `python` / `python3` / `pip` the numbered scripts
# already use keep resolving to it, and the image no longer fetches get-pip.py.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN python3.12 -m venv "$VIRTUAL_ENV"

# Install MongoDB Database Tools (detect architecture).  100.9.4 predates noble,
# so no ubuntu2404 build of the pinned version exists; the jammy build runs
# unchanged here (same libssl 3 soname).
ENV TOOLS_VER=100.9.4
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then \
        MONGO_ARCH="arm64"; \
    else \
        MONGO_ARCH="x86_64"; \
    fi && \
    TOOLS_DIR="mongodb-database-tools-ubuntu2204-${MONGO_ARCH}-${TOOLS_VER}" && \
    retry wget -q -O "${TOOLS_DIR}.tgz" "https://fastdl.mongodb.org/tools/db/${TOOLS_DIR}.tgz" && \
    tar -xzf "${TOOLS_DIR}.tgz" && \
    mv "${TOOLS_DIR}/bin"/* /usr/local/bin/ && \
    rm -rf "${TOOLS_DIR}"*

# Install GitHub CLI (optional, for releases)
RUN retry curl -fsSL -o /usr/share/keyrings/githubcli-archive-keyring.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y gh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy all scripts and Python files
COPY *.sh *.py ./

# Make all scripts executable
RUN chmod +x *.sh

# Create exports and output directories
RUN mkdir -p /app/exports /app/output
ENV SEFARIA_EXPORT_PATH=/app/exports

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
