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
# Parallel enrich: export PARALLEL=N (default 1). Maps to a Python-internal
# worker pool inside enrich.py (one process, N threads, each driving a
# Chromium on a distinct port CDP_PORT..CDP_PORT+N-1 with profile dirs
# USER_DATA[-i]). Workers pull from a shared queue → no shard pre-allocation,
# slow rows don't block fast workers. RAM cost ≈ 300 MB × N (one Chromium
# per worker) — keep N ≤ 4 on 16 GB hosts; PARALLEL=10 needs ~3 GB free.

set -euo pipefail
cd "$(dirname "$0")"

# Auto-source .env (gitignored project-local secrets: MAPBOX_ACCESS_TOKEN,
# RESROBOT_KEY, HEMNET_USER_AGENT). `set -a` exports every assignment.
[[ -f .env ]] && { set -a; source .env; set +a; }

ONSALE_URL="${ONSALE_URL:-https://www.hemnet.se/bostader?price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=18031}"
KOMMANDE_URL="${KOMMANDE_URL:-https://www.hemnet.se/kommande/bostader?price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=18031}"
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
# PARALLEL=1 case; shards 1..N-1 use suffixed profile dirs. Chromiums
# we start ourselves run headless and get cleaned up on EXIT; any tab
# that was already up before we ran is left alone.
SPAWNED_CHROMIUM_PIDS=()
cleanup_chromium() {
  for pid in "${SPAWNED_CHROMIUM_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup_chromium EXIT

ensure_chromium() {
  local port="$1" user_data="$2"
  if curl -s --max-time 2 "http://localhost:${port}/json/version" >/dev/null 2>&1; then
    return 0  # already up — leave alone, don't track for cleanup
  fi
  echo "▸ starting Chromium on :${port}  (profile $(basename "${user_data}"))"
  # NOTE: headed mode is required — Hemnet sits behind Cloudflare bot
  # detection, which serves a "Just a moment..." block page to
  # --headless=new and the real listing markup never renders.
  chromium --remote-debugging-port="${port}" --remote-allow-origins='*' \
           --user-data-dir="${user_data}" "${ONSALE_URL}" >/dev/null 2>&1 &
  SPAWNED_CHROMIUM_PIDS+=("$!")
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

CDP_PORTS=$(seq -s, "${CDP_PORT}" $((CDP_PORT + PARALLEL - 1)))

echo
echo "▸ enrich  (pool=${PARALLEL} on ports ${CDP_PORTS})"
HEMNET_CDP_PORTS="${CDP_PORTS}" python3 enrich.py "${ONSALE_JSONL}"

echo
echo "▸ geocode"
python3 geocode.py "${ENRICHED_JSONL}"

# Kommande pipeline — smaller than onsale (~30-200 listings). Shares the
# onsale cache namespace (set in enrich.py CACHE_NAMESPACE) since detail-page
# URLs/structure are identical; reuses the same worker pool.
KOMMANDE_JSONL="data/kommande-${DATE}.jsonl"
KOMMANDE_ENRICHED="data/kommande-${DATE}.enriched.jsonl"
echo
echo "▸ scrape kommande"
HEMNET_CDP_PORT="${CDP_PORT}" python3 scrape.py --url "${KOMMANDE_URL}"
echo
echo "▸ enrich kommande  (pool=${PARALLEL})"
HEMNET_CDP_PORTS="${CDP_PORTS}" python3 enrich.py "${KOMMANDE_JSONL}"
echo
echo "▸ geocode kommande"
python3 geocode.py "${KOMMANDE_ENRICHED}"

# Merge onsale + kommande into one live snapshot for scoring and map build.
LIVE_GEO="data/live-${DATE}.enriched.geo.jsonl"
cat "data/onsale-${DATE}.enriched.geo.jsonl" \
    "data/kommande-${DATE}.enriched.geo.jsonl" > "${LIVE_GEO}"
echo
echo "▸ merged onsale + kommande → ${LIVE_GEO} ($(wc -l < "${LIVE_GEO}") rows)"

echo
echo "▸ route (transit minutes to Odenplan via Trafiklab/ResRobot)"
python3 route.py "${LIVE_GEO}"

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
python3 score.py --sold "${SOLD}" --onsale "${LIVE_GEO}"

# Cross-day ledger (data/history.jsonl). Marks today's ids active and probes
# the detail page of any id that vanished since yesterday to detect sold
# (Slutpris / redirect to /salda/) or withdrawn. Stateful but reconstructible
# from data/live-*.scored.jsonl via `python3 history.py rebuild`.
LIVE_SCORED="data/live-${DATE}.enriched.geo.scored.jsonl"
echo
echo "▸ history (track outcomes across days)"
HEMNET_CDP_PORT="${CDP_PORT}" python3 history.py update --live "${LIVE_SCORED}"

echo
echo "▸ build map"
python3 build_map.py "${LIVE_SCORED}" --history data/history.jsonl

echo
echo "✓ done.  open  file://$(pwd)/index.html"
