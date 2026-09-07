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

# How many zstd workers to start.
#
# `-T0` does NOT mean "use every CPU": it resolves to the number of PHYSICAL
# cores (zstd's UTIL_countPhysicalCores(), which de-duplicates /proc/cpuinfo by
# "core id"), not the logical CPUs the scheduler will actually hand us.  On a
# 4-vCPU GitHub runner that is 2 workers — run 33987734987 logged
# "Note: 2 physical core(s) detected" and then spent 20m13s here, 41% of the
# whole job.  `nproc` reports all 4.  Copied from LinkerToOtzaria's
# ci/zstd_mt.sh so both halves of the pipeline share one policy.
#
# Byte-neutral: zstd's frame output depends on the compression level, the job
# size and the overlap size — NOT on how many workers chew through the jobs.
# Verified on a 202 MiB tar: -T1/-T2/-T4 give an identical sha256, with both
# the old and the new flag set.
zstd_workers() {
  local n
  n="$(nproc 2>/dev/null || echo 0)"
  case "$n" in
    ''|*[!0-9]*) n=0 ;;
  esac
  # Bound worst-case resident set: each worker holds roughly one job buffer plus
  # one match-finder context.  0 falls back to zstd's own detection (i.e. -T0).
  if [ "$n" -gt 32 ]; then
    n=32
  fi
  printf '%s\n' "$n"
}
WORKERS="$(zstd_workers)"
if [ "${WORKERS}" -eq 0 ]; then
  WORKERS_LABEL="auto (-T0)"
else
  WORKERS_LABEL="${WORKERS}"
fi

# Archive all the contents of the exports directory.
# -19 --long=27 is nearly the same ratio as --ultra -22 but 5-10× faster; the
# level and window stay put because two downstream jobs download this asset.
# -B16M --zstd=ovlog=6 shrinks the compression jobs — zstd's default here is
# 64 MiB jobs at full overlap (ovlog=9), so every worker re-chews a whole job's
# worth of context.  Measured at -19/-T4 on two synthetic export corpora
# (105 MiB and 202 MiB): 1.4-1.8× faster for +0.8-1.6% bytes.  -B4M is a further
# ~15% faster but costs +2.2-4.7%, i.e. roughly 3× the bytes for a couple of
# seconds — and this asset is downloaded by two jobs every cycle and then kept
# forever on an immutable release, so the seconds are noise where the megabytes
# are not.  Pinning the geometry also stops the bytes drifting between zstd
# releases instead of inheriting a default that moves under us.
# --no-progress replaces -v: zstd still prints its one-line summary, without the
# 4,009 progress lines that -v emitted (13% of the entire job log).
EXPORT_BYTES=$(du -sb exports | cut -f1)
echo "📦 Compressing ${FILE_COUNT} files ($(numfmt --to=iec-i --suffix=B "${EXPORT_BYTES}")) from exports/ at zstd -19, ${WORKERS_LABEL} workers..."
SECONDS=0
tar -cf - -C exports . | zstd -19 --long=27 -B16M --zstd=ovlog=6 -T"${WORKERS}" --no-progress -o "${COMBINED}"
ELAPSED=${SECONDS}

ARCHIVE_BYTES=$(stat -c%s "${COMBINED}")
RATIO=$(awk -v a="${ARCHIVE_BYTES}" -v e="${EXPORT_BYTES}" 'BEGIN{printf "%.2f", (e > 0 ? a * 100 / e : 0)}')
echo "✅ Archive created: ${COMBINED} — $(numfmt --to=iec-i --suffix=B "${ARCHIVE_BYTES}") (${RATIO}% of exports/) in ${ELAPSED}s"
