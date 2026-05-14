#!/usr/bin/env bash
# Daily on-sale refresh: scrape → enrich → geocode → score → rebuild map.
#
# Run multiple times a day if you like — output filename is date-stamped so
# same-day re-runs overwrite. Cache layers mean repeat runs are fast (~30s
# warm, ~3-5 min if many new listings appeared).
#
# Configure: edit $ONSALE_URL below or export it before running.
# The sold dataset is refreshed separately (monthly) via the longer pipeline
# in README; this script picks the latest sold-*.enriched.jsonl in data/.
#
# Parallel enrich: export PARALLEL=N (default 1) to shard the enrich step
# across N Chromium instances on ports CDP_PORT..CDP_PORT+N-1 with profile
# dirs USER_DATA-0..USER_DATA-(N-1). Each shard processes every N-th listing.
# RAM cost ≈ 300 MB × N (a Chromium instance per shard) — keep N ≤ 4 on
# 16 GB hosts; PARALLEL=10 needs ~3 GB free.

set -euo pipefail
cd "$(dirname "$0")"

ONSALE_URL="${ONSALE_URL:-https://www.hemnet.se/bostader?price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=18031}"
CDP_PORT="${CDP_PORT:-9223}"
USER_DATA="${USER_DATA:-/tmp/chromium-onsale}"
PARALLEL="${PARALLEL:-1}"
DATE="$(date +%F)"

echo "▸ on-sale refresh for ${DATE}  (parallel=${PARALLEL})"
echo "  filter:  ${ONSALE_URL}"
echo "  cdp:     :${CDP_PORT}$([[ $PARALLEL -gt 1 ]] && echo "..${CDP_PORT}+$((PARALLEL-1))")"
echo

# Launch (or reuse) one Chromium per shard. Shard 0 uses the original
# $USER_DATA path so existing warm cookies/session keep working in the
# PARALLEL=1 case; shards 1..N-1 use suffixed profile dirs.
ensure_chromium() {
  local port="$1" user_data="$2"
  if curl -s --max-time 2 "http://localhost:${port}/json/version" >/dev/null 2>&1; then
    return 0
  fi
  echo "▸ starting Chromium on :${port}  (profile $(basename "${user_data}"))"
  chromium --remote-debugging-port="${port}" --remote-allow-origins='*' \
           --user-data-dir="${user_data}" "${ONSALE_URL}" >/dev/null 2>&1 &
  for j in $(seq 1 30); do
    sleep 0.5
    if curl -s --max-time 1 "http://localhost:${port}/json/version" >/dev/null 2>&1; then
      echo "  Chromium up on :${port} after ${j} polls"
      return 0
    fi
  done
  echo "  ✗ Chromium failed to come up on :${port}" >&2
  return 1
}

shard_profile() {
  local i="$1"
  if [[ "$i" -eq 0 ]]; then echo "${USER_DATA}"; else echo "${USER_DATA}-${i}"; fi
}

for i in $(seq 0 $((PARALLEL - 1))); do
  ensure_chromium $((CDP_PORT + i)) "$(shard_profile "$i")"
done

echo
echo "▸ scrape"
HEMNET_CDP_PORT="${CDP_PORT}" python3 scrape.py --url "${ONSALE_URL}"

ONSALE_JSONL="data/onsale-${DATE}.jsonl"
ENRICHED_JSONL="data/onsale-${DATE}.enriched.jsonl"

echo
echo "▸ enrich"
if [[ "${PARALLEL}" -eq 1 ]]; then
  HEMNET_CDP_PORT="${CDP_PORT}" python3 enrich.py "${ONSALE_JSONL}"
else
  TMPDIR="$(mktemp -d)"
  trap 'rm -rf "${TMPDIR}"' EXIT
  # Round-robin shard the JSONL by line index so each shard sees the same
  # rough distribution of cache-hits/misses (no shard hogs all the fresh URLs).
  for i in $(seq 0 $((PARALLEL - 1))); do
    awk -v shard="$i" -v n="${PARALLEL}" 'NR % n == shard' \
      "${ONSALE_JSONL}" > "${TMPDIR}/shard-${i}.jsonl"
    echo "  shard ${i}: $(wc -l < "${TMPDIR}/shard-${i}.jsonl") listings → :$((CDP_PORT + i))"
  done
  pids=()
  for i in $(seq 0 $((PARALLEL - 1))); do
    port=$((CDP_PORT + i))
    HEMNET_CDP_PORT="${port}" python3 enrich.py \
      "${TMPDIR}/shard-${i}.jsonl" --out "${TMPDIR}/shard-${i}.enriched.jsonl" \
      2>&1 | sed "s/^/[shard ${i}] /" &
    pids+=("$!")
  done
  fail=0
  for pid in "${pids[@]}"; do wait "${pid}" || fail=1; done
  if [[ "${fail}" -ne 0 ]]; then
    echo "✗ at least one shard failed" >&2
    exit 1
  fi
  cat "${TMPDIR}"/shard-*.enriched.jsonl > "${ENRICHED_JSONL}"
  echo "  merged $(wc -l < "${ENRICHED_JSONL}") rows → ${ENRICHED_JSONL}"
fi

echo
echo "▸ geocode"
python3 geocode.py "${ENRICHED_JSONL}"

# Pick the most recent sold snapshot for scoring. Prefer the geocoded
# (.enriched.geo.jsonl) variant since score.py uses the lat/lon k-NN
# resolver at predict time.
SOLD="$(ls -t data/sold-*.enriched.geo.jsonl 2>/dev/null | head -1 || true)"
if [[ -z "${SOLD}" ]]; then
  SOLD="$(ls -t data/sold-*.enriched.jsonl 2>/dev/null | grep -v '\.geo\.' | head -1 || true)"
fi
if [[ -z "${SOLD}" ]]; then
  echo "✗ No sold dataset found in data/. Run the sold pipeline first (see README)." >&2
  exit 1
fi
echo
echo "▸ score (against $(basename "${SOLD}"))"
python3 score.py --sold "${SOLD}" --onsale "data/onsale-${DATE}.enriched.geo.jsonl"

echo
echo "▸ build map"
python3 build_map.py "data/onsale-${DATE}.enriched.geo.scored.jsonl"

echo
echo "✓ done.  open  file://$(pwd)/index.html"
