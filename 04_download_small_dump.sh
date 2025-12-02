#!/usr/bin/env bash
set -euo pipefail

mkdir -p mongo_dump
aria2c --file-allocation=none -x 16 -s 16 -k 1M -o dump_small.tar.gz "https://storage.googleapis.com/sefaria-mongo-backup/dump_small.tar.gz"
tar -xzf dump_small.tar.gz -C mongo_dump
rm -f dump_small.tar.gz
mkdir -p mongo_dump_pkg
if [ -d mongo_dump/dump/sefaria ]; then
  cp -a mongo_dump/dump/sefaria mongo_dump_pkg/
elif [ -d mongo_dump/sefaria ]; then
  cp -a mongo_dump/sefaria mongo_dump_pkg/
else
  echo "❌ 'sefaria' folder not found in dump"; exit 1
fi
rm -rf mongo_dump
find mongo_dump_pkg -maxdepth 2 -type d -print
