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
~30s warm-cache, ~3-5 min if many new listings appeared. Same-day re-runs
overwrite. Edit the `ONSALE_URL` near the top of the script (or `export
ONSALE_URL=…`) to change filters.

It auto-starts Chromium on `:9223` if it's not already up.

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
SOLD_URL='https://www.hemnet.se/salda/bostader?sold_age=12m&price_max=8000000&living_area_min=60&rooms_min=2.5&location_ids%5B%5D=898741'

chromium --remote-debugging-port=9222 --remote-allow-origins='*' \
         --user-data-dir=/tmp/chromium-playwright "$SOLD_URL" &

python3 scrape.py --url "$SOLD_URL"                       # → data/sold-YYYY-MM-DD.jsonl
python3 enrich.py data/sold-$(date +%F).jsonl             # → data/sold-YYYY-MM-DD.enriched.jsonl
```

Then run `./update.sh` to score on-sale against the new sold dataset and
regenerate the map. `update.sh` automatically picks the most-recent
`data/sold-*.enriched.jsonl` for scoring.

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
├── update.sh                              # one-shot daily refresh
├── deploy.sh                              # commit + push index.html to gh-pages
├── cdp.py                                 # tiny CDP helper, picks port from $HEMNET_CDP_PORT
├── scrape.py                              # list-page scraper, sold + onsale
├── enrich.py                              # detail-page enricher, sold + onsale
├── geocode.py                             # add lat/lon via Nominatim (cached)
├── score.py                               # hedonic regression: sold → onsale deal_pct
├── build_map.py                           # render index.html with deal-color pins
├── index.html                             # generated; double-click to view
├── data/
│   ├── sold-YYYY-MM-DD.jsonl              # raw sold snapshots
│   ├── sold-YYYY-MM-DD.enriched.jsonl
│   ├── onsale-YYYY-MM-DD.jsonl            # raw on-sale snapshots
│   └── onsale-YYYY-MM-DD.enriched.jsonl
└── cache/
    └── details/
        ├── sold/<listing_id>.json         # immutable — sold listings don't change
        └── onsale/<listing_id>.json       # mutable — clear to refresh price drops
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
location_ids  = 898741           # Stockholm — inom tullarna

# sold-only:
sold_age      = 12m              # trailing 12 months
```

Volumes (2026-05-10): **721 sold** in the trailing 12 months, **71 on-sale**
right now (after filtering out 9 `/nybyggnadsprojekt/` aggregate pages and 3
`Nyproduktion`-tagged listings — see below).

The 8M cap is intentional: real budget is ~6 MSEK, 8M provides comparable
headroom without polluting the distribution with luxury-segment outliers.

Hemnet caps result pagination at **50 pages × 50 cards = 2 500 listings max**.
Subdivide by sub-area (`location_ids`) if you need more coverage than that.

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

`--delay 1.5` (default) is real-browser pace. ~30–40 min for full enrichment
of a fresh ~720-listing sold snapshot from cold cache; ~3–5 min from warm
cache. On-sale enrichment is small (~70 listings) so always finishes in ~3 min.

### Snapshot semantics

Each `data/{kind}-YYYY-MM-DD.jsonl` is a self-contained snapshot. Re-running
overwrites the same date's file (use a different `--out` to keep both).
Sold's trailing-12m window means consecutive monthly snapshots have ~80%
overlap by listing-id; deduping for cross-snapshot analysis is a downstream
concern.

---

## Planned: geo-enrichment pass

Critical buy-criteria not on Hemnet:

- **Distance to water** (Stockholm-specific — Saltsjön, Mälaren, Riddarfjärden, canals)
- **Distance to public transport** (T-bana, pendeltåg, spårväg, buss)

Implementation plan (separate script `geo.py`, runs after `enrich.py`):

1. **Geocode** each unique `address + area` via Nominatim (OSM, free, 1 req/s).
   Cache results in `cache/geocode/<sha1>.json`. ~700 unique addresses → ~12 min cold.
2. **Fetch Stockholm geo features** *once* (refresh ~quarterly) from Overpass:
   - water: `natural=water`, `natural=coastline`, `waterway=*`
   - transit: `railway=station[+subway=yes]`, `railway=tram_stop`, `highway=bus_stop`
   Save to `cache/stockholm-geo-YYYY-MM.json`.
3. **Compute** per-listing nearest distance to each feature class
   (haversine to nearest stop / nearest water polygon edge), entirely offline.
   Output: `data/{kind}-YYYY-MM-DD.geo.jsonl` with added fields:
   - `lat`, `lon`
   - `dist_water_m`
   - `dist_subway_m`, `dist_train_m`, `dist_tram_m`, `dist_bus_m`
   - `nearest_subway_name`, etc.

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

Log-linear hedonic regression — standard real-estate pricing approach.
Implementation is pure-numpy (no sklearn / statsmodels dependency).

```
log(slutpris) = α + α_stadsdel
              + β_logm2     × log(m²)
              + β_byggar    × byggår_decade
              + β_våning    × våning
              + β_hiss      × 1[hiss]
              + β_avgift    × log(avgift_kr_mon + 1)
              + ε
```

Why log on the target: percentage errors (which is what we care about — a 5%
deal is the same news whether the apartment is 3M or 8M) become additive in
log-space, so plain OLS minimizes a percentage loss.

Why these features and not others:

- **stadsdel** — biggest single price driver, ~30k kr/m² spread inom tullarna.
- **log(m²)** — log so the marginal cost-per-extra-m² shrinks at large sizes.
- **byggår_decade** — coarse enough to dodge spurious year effects, fine
  enough to separate 1900s sekelskifte from 1960s miljonprogram.
- **våning, hiss** — top floor with hiss commands a real premium.
- **log(avgift+1)** — high BRF fee = lower price (you're capitalizing future
  payments). Log-shape because doubling a low fee hurts more than doubling a
  high one.

What the model does **not** capture (these are downstream upgrade paths):

- Renovation grade / interior condition (would need vision on photos)
- View / orientation / light
- BRF financial health (have BRF name; no balance-sheet pull yet)
- Distance to water / transit (planned `geo.py` pass)

### Rare-stadsdel handling

Stadsdelar with fewer than 8 sold rows in the snapshot are folded into a
single `Other` bucket. Without this, the regression assigns large coefficients
to thin slices and the predictions become unstable.

### Missing-feature imputation

For on-sale listings missing byggår / våning / avgift / hiss, we impute from
**per-stadsdel medians** of the sold data, with a global median as fallback.
This is preferred over row-dropping because Hemnet detail pages are sparse
on older listings and we'd lose ~10–30% of the on-sale set otherwise.

### Validation

Each run prints a holdout metric (80/20 split, evaluated on the held-out 20%
in linear price space):

```
MAPE (mean abs % error)   |  median APE  |  p90 APE
        ~7%                       ~6%           ~14%
```

Interpretation: a typical prediction is within 6–7% of actual sold price; the
worst 10% are off by ~15%. So treat any `|deal_pct| < ~10%` as noise — only
clearly-outside-the-band listings are real signals.

### Known model traps

- **Lilla Allmänna gränd 23 (Djurgården)** appears as a +73% "deal" because
  the model doesn't know about its unusual ownership form (bostadsrätt with
  marknadsavgäld or similar tomträtt-like structure). This isn't a parser
  bug — Hemnet hides `Upplåtelseform` on listings where it's the default,
  and the on-sale label coverage is only ~11%. Spot-check the top-K manually
  before trusting them.
- **Premium-renovated apartments** appear as "overpriced" by the model
  because the model can't see condition. They may sell at asking anyway —
  the bottom-K of the ranking is roughly "things the model can't explain."

### Re-running monthly

After both pipelines refresh:

```bash
python3 score.py \
    --sold   data/sold-$(date +%F).enriched.jsonl \
    --onsale data/onsale-$(date +%F).enriched.geo.jsonl
# writes data/onsale-$(date +%F).enriched.geo.scored.jsonl

python3 build_map.py data/onsale-$(date +%F).enriched.geo.scored.jsonl
# regenerates index.html with deal-score colored pins
```

Coefficients refit each run, so the model adapts to whatever the sold dataset
looks like in the trailing 12 months. No persisted model state.

