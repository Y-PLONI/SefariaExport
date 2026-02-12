#!/bin/bash
set -e

echo "=== Sefaria Export Pipeline ==="
echo "MongoDB: $MONGO_HOST:$MONGO_PORT"
echo "Database: $MONGO_DB_NAME"
echo ""

# Wait for MongoDB
echo "Waiting for MongoDB..."
./11_wait_for_mongodb.sh

# Handle timestamp: use env if provided, otherwise compute
if [ -z "${TS_STAMP:-}" ]; then
  ./01_compute_timestamp.sh
  export TS_STAMP="$(cat ts.txt)"
else
  export TS_STAMP
fi
echo "Using timestamp: $TS_STAMP"

# Run the export pipeline
echo "Starting export pipeline..."

./04_download_small_dump.sh
./05_clone_sefaria_project.sh
./06_install_build_deps.sh || true
./07_pip_install_requirements.sh || ./08_fallback_built_google_re2.sh
./09_create_exports_dir.sh
./10_create_local_settings.sh
./12_restore_db_from_dump.sh
./13_check_export_module.sh
./14_run_exports.sh
./15_verify_exports.sh
./16_drop_db.sh
./17a_remove_english_in_exports.sh
./17b_flatten_hebrew_in_exports.sh
./17_build_combined_archive.sh
./18_split_archive.sh

# Move archives to output directory (mapped as volume)
mkdir -p /app/output
mv /app/sefaria-exports-*.tar.zst* /app/output/ 2>/dev/null || true

echo ""
echo "=== Export complete! ==="
echo "Archives available in /app/output"
ls -lah /app/output/
