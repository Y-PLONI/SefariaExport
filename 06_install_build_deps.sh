#!/usr/bin/env bash
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get -o Acquire::Retries=3 update -y
  sudo apt-get -o Acquire::Retries=3 install -y libre2-dev pybind11-dev build-essential cmake ninja-build
fi
