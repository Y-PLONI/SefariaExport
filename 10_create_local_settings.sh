#!/usr/bin/env bash
set -euo pipefail

cd Sefaria-Project/sefaria
cp local_settings_example.py local_settings.py
python ../../configure_local_settings.py

echo ""
echo "📄 local_settings.py (non-secret keys only):"
# Anchored and terminated on purpose.  The old pattern also matched SEFARIA_DB*,
# so every run echoed `SEFARIA_DB_USER` and `SEFARIA_DB_PASSWORD` into the job
# log by design.  They are empty today; a log is still the wrong place for a
# line whose entire job is to hold a password.
grep -E "^(SEFARIA_EXPORT_PATH|MONGO_HOST|MONGO_PORT|SEFARIA_DB) *=" local_settings.py || true

# Django's relational DB stays at the upstream example value on purpose: this
# container restores Mongo and reads Mongo, and nothing on the export path
# touches a SQL table.  The cost is five upstream lines per run (counted in run
# 33987734987) — three `Remote config cache priming failed; will retry lazily.`
# and two `RemoteConfigCache: could not load from DB, using empty cache
# (OperationalError: unable to open database file)`.  Pointing DATABASES at a
# writable sqlite path does not remove them, it only changes the message to
# `no such table`, because nothing runs Sefaria's migrations here; making the
# priming actually succeed would mean running the whole Django migration set
# for a web app this job never starts.  Announced instead, so the expected
# lines are recognisable and a *new* database error still stands out.
echo "ℹ️  Django DATABASES is unused here (Mongo only); expect 5 upstream remote-config lines during the export (3 'Remote config cache priming failed' + 2 'RemoteConfigCache: could not load')."
