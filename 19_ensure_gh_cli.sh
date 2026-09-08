#!/usr/bin/env bash
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get -o Acquire::Retries=3 update -y
    sudo apt-get -o Acquire::Retries=3 install -y gh
  else
    echo "❌ gh CLI not found and apt-get unavailable" >&2
    exit 1
  fi
fi
gh --version
