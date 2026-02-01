#!/usr/bin/env bash
set -euo pipefail

EXPORTS_DIR="${SEFARIA_EXPORT_PATH:-${GITHUB_WORKSPACE:-$PWD}/exports}"
mkdir -p "${EXPORTS_DIR}"
export SEFARIA_EXPORT_BASE="${EXPORTS_DIR}"
echo "Exports directory: ${EXPORTS_DIR}"

# Export to GITHUB_ENV if running in GitHub Actions
if [ -n "${GITHUB_ENV:-}" ]; then
  echo "SEFARIA_EXPORT_BASE=${EXPORTS_DIR}" >> "${GITHUB_ENV}"
fi
