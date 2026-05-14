# Hemnet pipeline — sold + on-sale apartments

Scrape Stockholm apartment listings from Hemnet (both *Slutpriser* and *Bostäder
till salu*), enrich each row with detail-page facts, and (planned) compute
geo-features like distance to water and to public-transport stops.

Output is plain JSONL — one record per line — date-stamped per run, so
re-running periodically produces a series of comparable snapshots.

The end goal: predict fair market value from the sold dataset, then flag
on-sale listings whose asking price is well below predicted slutpris.

---

## Daily refresh

Once the sold dataset exists (see *Sold pipeline* below — that's a monthly thing),
the daily on-sale loop is one command:

```bash
./update.sh        # rebuild index.html from fresh data
./deploy.sh        # commit + push so GitHub Pages picks up the change
# or just: ./update.sh && ./deploy.sh
```

`deploy.sh` adds, commits and pushes only `index.html`. Data files and caches
stay local (see `.gitignore`).

`update.sh` does scrape → enrich → geocode → score → rebuild map. Runs in
~30s warm-cache, ~5-10 min if many new listings appeared (kommun-wide ~600
daily listings; first-of-day re-fetches the 18h-stale onsale cache).
Same-day re-runs are nearly instant. Edit the `ONSALE_URL` near the top of
the script (or `export ONSALE_URL=…`) to change filters.

It auto-starts Chromium on `:9223` if it's not already up.

**Parallel enrich**: `PARALLEL=N ./update.sh` shards the enrich step across
N Chromium instances on consecutive ports. With kommun-wide volumes this
matters — sequential is ~60 min/day, `PARALLEL=2` is ~30 min, `PARALLEL=10`
is ~6-10 min. Shard 0 keeps the original profile/cookies; shards 1..N-1
use suffixed profile dirs (first run may face one-time Hemnet cookie banner
per fresh profile).

**Geocoding via Mapbox** (recommended): set `MAPBOX_ACCESS_TOKEN` (free
public token, sign up at mapbox.com — 100k requests/month free tier).
`geocode.py` then uses 10 parallel workers at ~40 req/sec instead of
Nominatim's 1 req/sec. Without the token, falls back to Nominatim (set
`HEMNET_USER_AGENT` with a contact email per Nominatim's usage policy).
**Don't commit the token** — load via env var only.

### What's actually fetched fresh

- **List page** — always re-fetched. New listings appear, removed ones drop
  out, on-card fields like asking price, "Budgivning pågår", and next viewing
  reflect what Hemnet shows right now.
- **Detail pages** — cached per listing. On-sale cache has an **18-hour TTL**,
  so first run of the day re-fetches every detail page (catches accepted-price
  flips, new photos, BRF info changes); same-day re-runs reuse cache.
- **Merge precedence** — fresh list-page values override cached detail-page
  values when both have the field (so today's asking price wins over a stale
  cached one).
- **Geocoding** — cached forever (addresses don't move).
- **Sold cache** — no TTL (sold listings are immutable).

Override cache freshness with `python3 enrich.py … --cache-ttl-hours N`
(use `0` to force a full re-fetch).

---

## Sold pipeline (monthly)

The sold dataset is the model's training set. It only needs refreshing every
month or so since the trailing-12m window changes slowly.

```bash
SOLD_URL='https://www.hemnet.se/salda/bostader?sold_age=12m&price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=18031'

chromium --remote-debugging-port=9222 --remote-allow-origins='*' \
         --user-data-dir=/tmp/chromium-playwright "$SOLD_URL" &

python3 scrape.py --url "$SOLD_URL"                       # → data/sold-YYYY-MM-DD.jsonl
python3 enrich.py data/sold-$(date +%F).jsonl             # → data/sold-YYYY-MM-DD.enriched.jsonl
python3 geocode.py data/sold-$(date +%F).enriched.jsonl   # → data/sold-YYYY-MM-DD.enriched.geo.jsonl
```

The geocode step is required: `score.py` uses a lat/lon k-NN resolver at
predict time to map onsale listings to the right stadsdel bucket
(see *Scoring methodology* below).

Then run `./update.sh` to score on-sale against the new sold dataset and
regenerate the map. `update.sh` automatically picks the most-recent
`data/sold-*.enriched.geo.jsonl` for scoring (falls back to
`data/sold-*.enriched.jsonl` if not geocoded yet, but predictions will be
worse — see methodology).

The kind (sold vs onsale) is auto-detected from the URL path — `/salda/bostader`
is sold, `/bostader` is on-sale. Output filenames and cache subdirs are picked
accordingly.

Re-running enrichment hits `cache/details/{kind}/` for previously-seen
listings — monthly sold refreshes typically fetch only 50–150 newly-sold ones;
on-sale refreshes are smaller still.

---

## Layout

```
hemnet/
├── README.md                              # this file
├── CLAUDE.md                              # operational notes for AI assistants
├── update.sh                              # one-shot daily refresh (PARALLEL=N for sharded enrich)
├── deploy.sh                              # commit + push index.html to gh-pages
├── cdp.py                                 # tiny CDP helper, picks port from $HEMNET_CDP_PORT
├── scrape.py                              # list-page scraper, sold + onsale
├── enrich.py                              # detail-page enricher, sold + onsale
├── geocode.py                             # add lat/lon via Mapbox (or Nominatim fallback)
├── score.py                               # dual-region hedonic regression: sold → onsale deal_pct
├── experiment.py                          # feature/granularity ablation harness
├── build_map.py                           # render index.html with deal-color pins
├── index.html                             # generated; double-click to view
├── data/
│   ├── sold-YYYY-MM-DD.jsonl              # raw sold snapshots
│   ├── sold-YYYY-MM-DD.enriched.jsonl
│   ├── sold-YYYY-MM-DD.enriched.geo.jsonl # geocoded (needed for k-NN resolver)
│   ├── onsale-YYYY-MM-DD.jsonl            # raw on-sale snapshots
│   ├── onsale-YYYY-MM-DD.enriched.jsonl
│   └── onsale-YYYY-MM-DD.enriched.geo.jsonl
└── cache/
    ├── details/
    │   ├── sold/<listing_id>.json         # immutable — sold listings don't change
    │   └── onsale/<listing_id>.json       # mutable — clear to refresh price drops
    └── geocode/<sha1>.json                # engine-agnostic (Mapbox + Nominatim coexist)
```

Sold and on-sale caches are namespaced separately because the same property
can appear in both (an on-sale listing eventually becomes a sold listing) and
the field shapes differ.

---

## Filters in use

```
price_max     = 8 000 000 kr     # ~6 MSEK real budget + comparable headroom
living_area   ≥ 60 m²
rooms         ≥ 2.5
location_ids  = 18031            # Stockholms kommun

# sold-only:
sold_age      = 12m              # trailing 12 months
```

Volumes (2026-05-14, kommun-wide): **2 500 sold** (the pagination cap, ~7-month
window instead of full 12), **~666 on-sale** right now after filtering out
new-construction.

The kommun-wide filter (vs the earlier inom-tullarna 898741) exists because
the user's binding constraint is the kommun boundary, not commute time:
kommunal schools tie to your hemkommun, so moving across municipal lines
forces a school change.

The 8M cap is intentional: real budget is ~6 MSEK, 8M provides comparable
headroom without polluting the distribution with luxury-segment outliers.

Hemnet caps result pagination at **50 pages × 50 cards = 2 500 listings max**.
The Stockholms-kommun sold scrape hits this cap → we get the newest ~7
months instead of 12. That's fine for hedonic — recent comparables matter
more anyway.

### What's filtered out automatically

- **`/nybyggnadsprojekt/` URLs** — these are aggregate landing pages for
  new-construction *projects*, with no per-unit price/area on the card.
- **Listings tagged `Nyproduktion`** — never-occupied or under-construction
  new builds. The user wants ready-to-use apartments only; if a unit is a
  truly-finished new-build that's also tagged Nyproduktion, it gets dropped
  too (acceptable trade-off).

---

## Field reference

### List-page fields (`scrape.py`)

| field             | sold | onsale | notes                                         |
|-------------------|:----:|:------:|-----------------------------------------------|
| href              |  ✓   |   ✓    | listing detail page URL                       |
| address           |  ✓   |   ✓    | street + number; on-sale may include `, X tr` |
| area              |  ✓   |   ✓    | `Stadsdel, Stockholms kommun`                 |
| sold_date         |  ✓   |        | Swedish date e.g. `16 mar. 2026`              |
| visning           |      |   ✓    | next viewing time, e.g. `Sön 17 maj kl 13:00` |
| m2                |  ✓   |   ✓    | primary living area only                      |
| rooms             |  ✓   |   ✓    | `2,5 rum` → `2.5`                             |
| fee_kr            |  ✓   |   ✓    | månadsavgift                                  |
| price_kr          |  ✓   |        | slutpris                                      |
| asking_price_kr   |      |   ✓    | utgångspris on the card                       |
| kr_per_m2         |  ✓   |   ✓    | Hemnet's value, not derived                   |
| price_diff_pct    |  ✓   |        | sold-vs-asking %, can be negative or null     |
| vaning            |      |   ✓    | floor (often present on on-sale cards)        |
| vaning_total      |      |   ✓    |                                               |
| tags              |  ✓   |   ✓    | `Balkong`, `Hiss`, `Uteplats`                 |
| promotions        |      |   ✓    | `Premium`, `Plus` (paid-promotion flags)      |
| description       |      |   ✓    | ad-copy excerpt; nullable                     |
| agent             |  ✓   |   ✓    | mäklarbyrå name                               |

### Detail-page fields (`enrich.py`)

| field              | sold | onsale | notes                                          |
|--------------------|:----:|:------:|------------------------------------------------|
| asking_price_kr    |  ✓   |   ✓    | utgångspris                                    |
| asking_diff_kr     |  ✓   |        | sold − asking, kr (sign preserved)             |
| asking_diff_pct    |  ✓   |        | sold − asking, % (sign preserved)              |
| upplatelseform     |  ✓   |   ✓    | `Bostadsrätt` / `Tomträtt` / `Andel`           |
| bostadstyp         |  ✓   |   ✓    | `Lägenhet` / `Villa` / ...                     |
| forening           |      |   ✓    | BRF name — useful for grouping same-building   |
| boarea_m2          |  ✓   |   ✓    | confirms list-page `m2`                        |
| biarea_m2          |  ✓   |   ✓    | secondary area when present                    |
| detail_rooms       |  ✓   |   ✓    |                                                |
| byggar             |  ✓   |   ✓    | construction year — **key valuation feature**  |
| vaning             |  ✓   |   ✓    | floor                                          |
| vaning_total       |  ✓   |   ✓    | building's top floor                           |
| hiss               |  ✓   |   ✓    | parsed from "hiss finns" in våning text        |
| balkong            |  ✓   |   ✓    |                                                |
| uteplats           |  ✓   |   ✓    |                                                |
| avgift_kr_mon      |  ✓   |   ✓    | månadsavgift                                   |
| drift_kr_year      |  ✓   |   ✓    | driftskostnad (often missing on on-sale)       |
| energiklass        |  ✓   |   ✓    | A–G; rare on older listings                    |
| antal_besok        |  ✓   |        | listing pageviews while live                   |
| photos             |  ✓   |   ✓    | ordered list of `bilder.hemnet.se` URLs (high-res); floor-plan images last |

Some detail-page fields are **missing at source** on a fraction of listings
(byggår/våning ~10–30% gaps). These are not parsing bugs — Hemnet just doesn't
have the data. Enricher records `null` and continues.

---

## Methodology notes

### Why drive existing Chromium via CDP

You already have Chromium open with DevTools to inspect filters; driving it
via `--remote-debugging-port=9222`/`9223` reuses your session cookies, applies
your manually-set filters automatically, and lets you watch what's being
scraped. Pure Playwright headless would also work but adds a separate browser
context and obscures debugging.

### Why per-listing cache

Sold listings are immutable post-sale. Caching by listing-id (the trailing
numeric in the URL) means a monthly re-run mostly hits cache and only the
~50–150 newly-sold listings since the last run actually go to the network.
On-sale caches are the same shape but should be cleared periodically since
asking prices and showings change while a listing is live.

### Pacing

`--delay 1.5` (default) is real-browser pace. ~60 min for the kommun-wide
~2500-listing sold snapshot from cold cache; ~3–5 min from warm cache.
On-sale enrichment with the kommun filter is ~600 listings → ~50-60 min
cold, near-instant warm. Use `PARALLEL=N ./update.sh` to shard across N
Chromium instances and cut first-of-day runs roughly N× (tested up to 10).

### Snapshot semantics

Each `data/{kind}-YYYY-MM-DD.jsonl` is a self-contained snapshot. Re-running
overwrites the same date's file (use a different `--out` to keep both).
Sold's trailing-12m window means consecutive monthly snapshots have ~80%
overlap by listing-id; deduping for cross-snapshot analysis is a downstream
concern.

---

## Planned: geo-feature pass

Geocoding is done (`geocode.py` — Mapbox or Nominatim, both cached). What's
still planned: distance-to-water and distance-to-public-transport.

Critical buy-criteria not on Hemnet:

- **Distance to water** (Stockholm-specific — Saltsjön, Mälaren, Riddarfjärden, canals)
- **Distance to public transport** (T-bana, pendeltåg, spårväg, buss)

Implementation sketch (separate script, runs after `geocode.py`):

1. **Fetch Stockholm geo features** *once* (refresh ~quarterly) from Overpass:
   - water: `natural=water`, `natural=coastline`, `waterway=*`
   - transit: `railway=station[+subway=yes]`, `railway=tram_stop`, `highway=bus_stop`
   Save to `cache/stockholm-geo-YYYY-MM.json`.
2. **Compute** per-listing nearest distance to each feature class
   (haversine to nearest stop / nearest water polygon edge), entirely offline.
   Add fields: `dist_water_m`, `dist_subway_m`, `nearest_subway_name`, etc.

Haversine is fine for "is the apartment near water?" (binary-ish), but for
transit you may eventually want walking distance via OSRM. Start with
haversine; upgrade if the simple metric doesn't track intuition.

---

## Scoring methodology

Goal: rank each on-sale listing by how far below predicted fair-value its
asking price is — the "deal %". Predictions come from a hedonic regression
fit on the sold dataset.

### One-line definition

```
deal_pct = (predicted_slutpris − asking_price) / asking_price × 100
```

A listing with `deal_pct = +20%` is asking 20% under what comparable sold
properties suggest it should fetch. Negative numbers mean priced above model.

### The model

Log-linear hedonic regression, but **trained as two separate models** —
inner (inom-tullarna stadsdelar) and outer (everything else). Implementation
is pure-numpy (no sklearn / statsmodels dependency). Both models are
filtered to **apartments only** (`bostadstyp == "Lägenhet"`); houses
have different feature semantics (no våning, no hiss, often no avgift) and
including them adds noise.

```
log(slutpris) = α + α_stadsdel
              + β_logm2     × log(m²)
              + β_byggar    × byggår_decade
              + β_våning    × våning
              + β_hiss      × 1[hiss]
              + β_avgift    × log(avgift_kr_mon + 1)
              + β_house     × 1[bostadstyp ∈ houses]
              + β_tomratt   × 1[upplatelseform == "Tomträtt"]
              + β_aganderatt× 1[upplatelseform == "Äganderätt"]
              + β_andel     × 1[upplatelseform == "Andel i bostadsförening"]
              + ε
```

Why log on the target: percentage errors (which is what we care about — a 5%
deal is the same news whether the apartment is 3M or 8M) become additive in
log-space, so plain OLS minimizes a percentage loss.

Why two models: with kommun-wide training data, a single model has to
compromise between the tight inner-city market and the heterogeneous suburbs.
Splitting recovers ~8% MAPE on inner (close to the old inom-tullarna-only
baseline) while accepting ~14-16% MAPE on outer as a genuine market property.

Why these features:

- **stadsdel** — biggest single price driver; inner uses coarse encoder
  (~10 buckets), outer uses fine encoder (~90 buckets after rare-fold).
- **log(m²)** — log so the marginal cost-per-extra-m² shrinks at large sizes.
- **byggår_decade** — coarse enough to dodge spurious year effects, fine
  enough to separate 1900s sekelskifte from 1960s miljonprogram.
- **våning, hiss** — top floor with hiss commands a real premium.
- **log(avgift+1)** — high BRF fee = lower price (you're capitalizing future
  payments). Log-shape because doubling a low fee hurts more than doubling
  a high one.
- **ownership flags** — barely move holdout MAPE (apt-only filter leaves
  little variance), but needed at predict time for tomträtt edge cases like
  Lilla Allmänna gränd 23 where the model would otherwise miss the discount.

What the model does **not** capture (these are downstream upgrade paths):

- Renovation grade / interior condition (would need vision on photos)
- View / orientation / light
- BRF financial health (have BRF name; no balance-sheet pull yet)
- Distance to water / transit (planned `geo.py` pass)

### Predict-time bucket routing (lat/lon k-NN)

Hemnet tags the same place inconsistently in sold vs onsale. For example,
sold Tensta is `"Spånga - Tensta, Stockholms kommun"` → coarse-normalizes to
"Spånga"; onsale Tensta is `"Tensta, Stockholms kommun"` → "Tensta". A naive
predict path looks up "Tensta" in the model, doesn't find it, falls back to
the biased "Other" bucket, and over-predicts by ~2×.

Fix: at predict time, each onsale row's `(lat, lon)` is matched against
the **k=5 nearest geocoded sold rows**, and we take the majority
`normalize_stadsdel(area)` of those neighbors. The onsale row then uses
*that* bucket — whichever label the sold side actually trained on for the
geographic area. This is robust to whatever inconsistent area strings
Hemnet invents in the future, because the resolver is purely geographic.

This is why the sold pipeline now needs the geocode step too.

### Rare-stadsdel handling

Stadsdelar with fewer than 4 sold rows in the snapshot are folded into a
single `Other` bucket. The threshold is a compromise: higher (8+) pushes
small suburb buckets like Vårberg or Sätra into "Other", which then averages
across heterogeneous folds and biases predictions for those areas high.
Lower keeps more buckets but their coefficients are noisier — acceptable
trade-off here.

### Missing-feature imputation

For on-sale listings missing byggår / våning / avgift / hiss, we impute from
**per-stadsdel medians** of the sold data, with a global median as fallback.
This is preferred over row-dropping because Hemnet detail pages are sparse
on older listings and we'd lose ~10–30% of the on-sale set otherwise.

For ownership = Äganderätt (houses on freehold), `avgift` is genuinely zero
rather than missing — `featurize` detects that case and skips the median
imputation so houses don't get falsely tagged with an apartment-typical fee.

### Validation

Each run prints separate holdout metrics for the inner and outer models
(80/20 split, evaluated on the held-out 20% in linear price space):

```
Inner model: MAPE ~8%   median APE ~6%   p90 APE ~16%
Outer model: MAPE ~14%  median APE ~10%  p90 APE ~30%
```

Interpretation: inner-tullarna predictions are within ~6-8% on average,
worst 10% off by ~16%. Outer is noisier — treat `|deal_pct| < 20%` as noise
for outer-kommun listings, `< 10%` for inner.

### Known model traps

- **Lilla Allmänna gränd 23 (Djurgården)** still appears as a +70-85%
  "deal" because the model doesn't know about its unusual tomträtt-style
  ownership form. The ownership flags would catch it if Hemnet reliably
  exposed `Upplåtelseform`, but on-sale label coverage is only ~11%. Spot-
  check the top-K manually.
- **"Accepterat pris" outer listings** sometimes show large positive
  deal-pct because the asking is a deliberately low bidding anchor;
  expected sold price is near or above asking. Common in Hässelby /
  Spånga / Vårberg.
- **Premium-renovated apartments** appear as "overpriced" by the model
  because the model can't see condition. They may sell at asking anyway —
  the bottom-K of the ranking is roughly "things the model can't explain."

### Experimentation

`experiment.py` runs ablation sweeps over stadsdel granularity, ownership
features, apt-only filtering, and single-vs-split model architecture. Useful
when revisiting feature decisions.

```bash
python3 experiment.py --sold data/sold-$(date +%F).enriched.geo.jsonl
```

### Re-running monthly

After both pipelines refresh:

```bash
python3 score.py \
    --sold   data/sold-$(date +%F).enriched.geo.jsonl \
    --onsale data/onsale-$(date +%F).enriched.geo.jsonl
# writes data/onsale-$(date +%F).enriched.geo.scored.jsonl

python3 build_map.py data/onsale-$(date +%F).enriched.geo.scored.jsonl
# regenerates index.html with deal-score colored pins
```

Coefficients refit each run, so each model adapts to whatever the sold
dataset looks like in the trailing window. No persisted model state.

