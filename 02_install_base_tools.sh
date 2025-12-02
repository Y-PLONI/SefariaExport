#!/usr/bin/env bash
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends aria2 ca-certificates tar zstd wget netcat-openbsd
  sudo apt-get clean
  sudo rm -rf /var/lib/apt/lists/*
fi
python3 -V || true
