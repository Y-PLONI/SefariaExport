#!/usr/bin/env bash
set -euo pipefail

# Compute release timestamp
TZ="${TZ_NAME:-Asia/Jerusalem}" date '+%Y-%m-%d_%H-%M' > ts.txt
export TS_STAMP="$(cat ts.txt)"
echo "Timestamp: $TS_STAMP"

# Export to GITHUB_OUTPUT if running in GitHub Actions
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "stamp=$TS_STAMP" >> "${GITHUB_OUTPUT}"
fi
