#!/usr/bin/env bash
set -euo pipefail

# Configurable via env, with sensible defaults
MONGO_HOST=${MONGO_HOST:-127.0.0.1}
MONGO_PORT=${MONGO_PORT:-27017}
MONGO_DB_NAME=${MONGO_DB_NAME:-sefaria}

# How to handle index restoration:
#   all        -> restore all indexes via metadata-only pass
#   skip_links -> restore all indexes EXCEPT for the heavy sefaria.links indexes (default)
#   none       -> do not restore any indexes (data only)
RESTORE_INDEXES_MODE=${RESTORE_INDEXES_MODE:-skip_links}

echo "📦 Preparing to restore MongoDB dump"
echo "   Host: ${MONGO_HOST}:${MONGO_PORT}"
echo "   DB  : ${MONGO_DB_NAME}"
echo "   Index mode: ${RESTORE_INDEXES_MODE} (override with RESTORE_INDEXES_MODE=all|skip_links|none)"

if [ ! -d mongo_dump_pkg/sefaria ]; then
  echo "❌ mongo_dump_pkg/sefaria not found"; exit 1
fi

# Phase 1: Restore DATA ONLY (no indexes) to avoid long-running index builds during import
echo "▶️  Restoring data (no indexes)..."
mongorestore \
  --host "${MONGO_HOST}" \
  --port "${MONGO_PORT}" \
  --drop \
  --db "${MONGO_DB_NAME}" \
  --noIndexRestore \
  "mongo_dump_pkg/sefaria"

echo "✅ Data restore completed."

echo ""
# No released mongodb-database-tools carries a `--metadataOnly` flag — 100.9.4,
# the version this image pins, included — so this probe has been false on every
# run and the branches behind it have never executed.  It stays because it is
# the only thing that would notice the flag arriving, but it is an ℹ️ and not a
# ⚠️: the indexes come from ensure_indices() (create_critical_indexes.py, below)
# either way, and a warning that fires unconditionally only trains people to
# skip warnings.
MONGORESTORE_HELP=$(mongorestore --help 2>&1 || true)
if echo "$MONGORESTORE_HELP" | grep -q -- "--metadataOnly"; then
  HAS_METADATA_ONLY=1
  echo "✅ mongorestore supports --metadataOnly"
else
  HAS_METADATA_ONLY=0
  echo "ℹ️  mongorestore has no --metadataOnly flag (expected); indexes come from ensure_indices()."
fi

# Phase 2: Optionally restore metadata (indexes) with exclusions to avoid timeouts on heavy collections
case "${RESTORE_INDEXES_MODE}" in
  all)
    if [ "$HAS_METADATA_ONLY" -eq 1 ]; then
      echo "▶️  Restoring metadata (indexes) for all collections..."
      mongorestore \
        --host "${MONGO_HOST}" \
        --port "${MONGO_PORT}" \
        --db "${MONGO_DB_NAME}" \
        --metadataOnly \
        "mongo_dump_pkg/sefaria"
      echo "✅ Index metadata restored for all collections."
    else
      echo "ℹ️  No metadata restore (see above); ensure_indices() will build the indexes."
      echo "    To build them from the dump's own metadata.json instead, set ENABLE_INDEXES_FROM_METADATA=true"
    fi
    ;;
  skip_links)
    if [ "$HAS_METADATA_ONLY" -eq 1 ]; then
      echo "▶️  Restoring metadata (indexes) for all collections EXCEPT '${MONGO_DB_NAME}.links'..."
      mongorestore \
        --host "${MONGO_HOST}" \
        --port "${MONGO_PORT}" \
        --db "${MONGO_DB_NAME}" \
        --metadataOnly \
        --nsExclude "${MONGO_DB_NAME}.links" \
        "mongo_dump_pkg/sefaria"
      echo "✅ Index metadata restored (links collection skipped)."
    else
      echo "ℹ️  No metadata restore (see above); ensure_indices() will build the indexes."
      echo "    To build them from the dump's own metadata.json (excluding links) instead, set ENABLE_INDEXES_FROM_METADATA=true"
    fi
    ;;
  none)
    echo "⏭️  Skipping index restoration as requested (RESTORE_INDEXES_MODE=none)."
    ;;
  *)
    echo "⚠️  Unknown RESTORE_INDEXES_MODE='${RESTORE_INDEXES_MODE}'. Use one of: all | skip_links | none" >&2
    exit 2
    ;;
esac

# Optional fallback: create indexes by reading metadata.json files when --metadataOnly is unavailable
if [ "${ENABLE_INDEXES_FROM_METADATA:-false}" = "true" ] && [ "$HAS_METADATA_ONLY" -eq 0 ] && [ "${RESTORE_INDEXES_MODE}" != "none" ]; then
  echo "🧩 ENABLE_INDEXES_FROM_METADATA=true — attempting to apply indexes from dump metadata.json"
  python ./apply_indexes_from_dump.py || echo "⚠️  apply_indexes_from_dump.py encountered errors; continuing without blocking the pipeline."
fi

# Create the indexes the export queries rely on. mongorestore --metadataOnly
# does not always reapply the dump's secondary indexes (and the dump may not
# even ship them), which forces COLLSCAN on every texts.find({title,language})
# and turns the export into a multi-hour job on small runners.
python ./create_critical_indexes.py

# Ensure 'history' collection exists (some exports expect it)
python ./ensure_history_collection.py

if [ "${KEEP_MONGO_DUMP:-false}" != "true" ]; then
  rm -rf mongo_dump_pkg
fi

echo "✅ Mongo restore complete."
