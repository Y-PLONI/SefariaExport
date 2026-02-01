#!/usr/bin/env bash
set -euo pipefail

if [ -d "Sefaria-Project" ]; then
  echo "Sefaria-Project already exists, skipping clone"
else
  git clone --depth 1 https://github.com/Sefaria/Sefaria-Project.git
fi
ls -la Sefaria-Project | head -n 50
