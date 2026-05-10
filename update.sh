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

set -euo pipefail
cd "$(dirname "$0")"

ONSALE_URL="${ONSALE_URL:-https://www.hemnet.se/bostader?price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=898741}"
CDP_PORT="${CDP_PORT:-9223}"
USER_DATA="${USER_DATA:-/tmp/chromium-onsale}"
DATE="$(date +%F)"

echo "▸ on-sale refresh for ${DATE}"
echo "  filter:  ${ONSALE_URL}"
echo "  cdp:     :${CDP_PORT}"
echo

# Start Chromium on the CDP port if not already there
if ! curl -s --max-time 2 "http://localhost:${CDP_PORT}/json/version" >/dev/null 2>&1; then
  echo "▸ starting Chromium on :${CDP_PORT}…"
  chromium --remote-debugging-port="${CDP_PORT}" --remote-allow-origins='*' \
           --user-data-dir="${USER_DATA}" "${ONSALE_URL}" >/dev/null 2>&1 &
  for i in $(seq 1 30); do
    sleep 0.5
    if curl -s --max-time 1 "http://localhost:${CDP_PORT}/json/version" >/dev/null 2>&1; then
      echo "  Chromium up after ${i} polls"
      break
    fi
  done
fi

export HEMNET_CDP_PORT="${CDP_PORT}"

echo
echo "▸ scrape"
python3 scrape.py  --url "${ONSALE_URL}"

echo
echo "▸ enrich"
python3 enrich.py  "data/onsale-${DATE}.jsonl"

echo
echo "▸ geocode"
python3 geocode.py "data/onsale-${DATE}.enriched.jsonl"

# Pick the most recent sold snapshot for scoring
SOLD="$(ls -t data/sold-*.enriched.jsonl 2>/dev/null | head -1 || true)"
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
