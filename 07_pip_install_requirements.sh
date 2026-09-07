#!/usr/bin/env bash
set -euo pipefail

cd Sefaria-Project
# `python`/`pip` are the image's /opt/venv ones: 24.04 refuses installs into the
# system interpreter (PEP 668), so the venv — not --break-system-packages — is
# what keeps every install here working.
python -m pip install --upgrade pip setuptools wheel
# Let pip resolve google-re2 to whatever wheel matches the interpreter (the
# old `google-re2==1.0` pin breaks the install on Python 3.12).
pip install -r requirements.txt
