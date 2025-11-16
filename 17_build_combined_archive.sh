#!/usr/bin/env bash
set -euo pipefail

cd "${GITHUB_WORKSPACE:-$PWD}"
COMBINED="sefaria-exports-${TS_STAMP}.tar.zst"

# Verify that the exports directory contains files
FILE_COUNT=$(find exports -type f 2>/dev/null | wc -l)
echo "📊 Found ${FILE_COUNT} files in exports/"

if [ "${FILE_COUNT}" -eq 0 ]; then
  echo "❌ No files found in exports directory!"
  exit 1
fi

# Archive all the contents of the exports directory
echo "📦 Creating archive from exports/ directory..."
tar -cf - -C exports . | zstd --ultra -22 -T0 -o "${COMBINED}"

ls -lh "${COMBINED}"
echo "✅ Archive created: ${COMBINED}"
