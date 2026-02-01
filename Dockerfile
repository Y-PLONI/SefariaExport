FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Jerusalem
ENV DJANGO_SETTINGS_MODULE=sefaria.settings
ENV MONGO_HOST=mongodb
ENV MONGO_PORT=27017
ENV MONGO_DB_NAME=sefaria
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install base system dependencies and add deadsnakes PPA for Python 3.9
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    software-properties-common \
    gpg-agent \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update -y && \
    apt-get install -y --no-install-recommends \
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
    python3.9 \
    python3.9-venv \
    python3.9-dev \
    python3.9-distutils \
    libre2-dev \
    pybind11-dev \
    build-essential \
    cmake \
    ninja-build \
    libpq-dev \
    sudo \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.9 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Make python3.9 the default python
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.9 1 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1

# Install MongoDB Database Tools (detect architecture)
ENV TOOLS_VER=100.9.4
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then \
        MONGO_ARCH="arm64"; \
    else \
        MONGO_ARCH="x86_64"; \
    fi && \
    wget -q "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-ubuntu2204-${MONGO_ARCH}-${TOOLS_VER}.tgz" && \
    tar -xzf "mongodb-database-tools-ubuntu2204-${MONGO_ARCH}-${TOOLS_VER}.tgz" && \
    mv mongodb-database-tools-ubuntu2204-${MONGO_ARCH}-${TOOLS_VER}/bin/* /usr/local/bin/ && \
    rm -rf mongodb-database-tools-ubuntu2204-${MONGO_ARCH}-${TOOLS_VER}*

# Install GitHub CLI (optional, for releases)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy all scripts and Python files
COPY *.sh *.py ./

# Make all scripts executable
RUN chmod +x *.sh

# Create exports directory
RUN mkdir -p /app/exports
ENV SEFARIA_EXPORT_PATH=/app/exports

# Create entrypoint script
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Sefaria Export Pipeline ==="\n\
echo "MongoDB: $MONGO_HOST:$MONGO_PORT"\n\
echo "Database: $MONGO_DB_NAME"\n\
echo ""\n\
\n\
# Wait for MongoDB\n\
echo "Waiting for MongoDB..."\n\
./11_wait_for_mongodb.sh\n\
\n\
# Run the export pipeline\n\
echo "Starting export pipeline..."\n\
\n\
./01_compute_timestamp.sh\n\
./04_download_small_dump.sh\n\
./05_clone_sefaria_project.sh\n\
./06_install_build_deps.sh || true\n\
./07_pip_install_requirements.sh || ./08_fallback_built_google_re2.sh\n\
./09_create_exports_dir.sh\n\
./10_create_local_settings.sh\n\
./12_restore_db_from_dump.sh\n\
./13_check_export_module.sh\n\
./14_run_exports.sh\n\
./15_verify_exports.sh\n\
./16_drop_db.sh\n\
./17a_remove_english_in_exports.sh\n\
./17b_flatten_hebrew_in_exports.sh\n\
./17_build_combined_archive.sh\n\
./18_split_archive.sh\n\
\n\
echo ""\n\
echo "=== Export complete! ==="\n\
echo "Archives available in /app/exports"\n\
ls -lah /app/exports/\n\
' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
