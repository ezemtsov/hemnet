# CLAUDE.md — operational notes for assisting in this repo

The README is for users; this file is for future-me. Read this first; everything
else is downstream of what's here.

## What it does

Hemnet onsale + sold scraper → hedonic-regression deal scorer → static Leaflet
map at **https://ezemtsov.github.io/hemnet/**. Pipeline + methodology in README.

## The user's main loop

```bash
./update.sh        # fresh onsale scrape + enrich + geocode + score + rebuild index.html
./deploy.sh        # commit + push index.html (Pages rebuilds in ~1 min)
```

`update.sh` auto-starts Chromium on `:9223` if down. **The sold refresh is
monthly and manual** — see README "Sold pipeline".

`PARALLEL=N ./update.sh` enriches via a Python-internal worker pool: one
process, N threads, each holding its own CDP connection to a different
Chromium (`CDP_PORT..CDP_PORT+N-1`). update.sh translates this to the
`HEMNET_CDP_PORTS=9223,9224,...` env var the new enrich.py honors —
no shell sharding / merge dance. Workers pull from a shared queue, so
slow rows don't block fast workers (true work-stealing). Chromium
profile dirs `USER_DATA[-i]` are persisted between runs; shard 0 keeps
the original `USER_DATA` so warm cookies survive PARALLEL=1. Tested
to PARALLEL=10. With kommun-wide ~600 daily listings, PARALLEL=4-10
cuts first-of-day runs from ~60 min to ~6-15 min.

## Two Chromium instances (required)

| port | purpose | profile dir |
|------|---------|-------------|
| 9222 | sold scrape (manual / monthly) | `/tmp/chromium-playwright` |
| 9223 | onsale scrape (daily, auto)    | `/tmp/chromium-onsale` |

Both need `--remote-debugging-port=N --remote-allow-origins='*'` (Chromium's
default origin policy rejects CDP from `http://localhost:N` otherwise).

## Required env vars

- `MAPBOX_ACCESS_TOKEN` — public Mapbox token (`pk....`) for geocoding. With
  it set, `geocode.py` runs parallel workers at ~40 req/sec instead of
  Nominatim's 1 req/sec. Set in shell rc — **never commit the token value**
  (it's "public" in Mapbox terminology but still counts against your quota
  and shouldn't end up in git history). Falls back to Nominatim if unset.
- `HEMNET_USER_AGENT` — Nominatim asks for a real contact email when used as
  fallback. Set in shell rc; **never commit hardcoded value**.
- `HEMNET_CDP_PORT` — single Chromium port (default 9222). Used by scrape.py.
- `HEMNET_CDP_PORTS` — comma-sep list of Chromium ports for the enrich
  worker pool (e.g. `9223,9224,9225`). Overrides `HEMNET_CDP_PORT` in
  `enrich.py`. update.sh sets this from `${CDP_PORT}..${CDP_PORT}+PARALLEL-1`.
- `ONSALE_URL` — overrides the default onsale filter URL in `update.sh`.
- `PARALLEL` — shard count for enrich (default 10). See main loop section.

## Environment quirks

- **NixOS + fish shell.** No `sklearn`, `statsmodels`, `pandas`. Pure numpy
  only — `score.py` does OLS via `np.linalg.lstsq`. Don't suggest sklearn.
- **Pyright "Import 'cdp' could not be resolved"** — LSP path artifact when
  the project root isn't its workspace root; not a runtime issue. Ignore.
- **One listing reliably times out** (Snickarbacken 7) — quirky page, no fix
  needed. Counted in `failed=`.

## Don't

- Don't put the user's email, Mapbox token, or other credentials in committed
  code. All are env-var driven by design.
- **Don't push without an explicit user instruction.** `./deploy.sh` is the
  user-authorized path; running it from another context still requires
  consent. `git push` from anywhere else is a surprise.
- Don't backfill sold photos — wasted effort (memory file explains).
- Don't suggest including new-construction listings — explicitly filtered
  out by design (memory file explains).
- Don't suggest dropping the `bostadstyp == "Lägenhet"` filter in scoring.
  Houses (Villa/Radhus/Parhus/Kedjehus) have different feature semantics
  (no våning, no hiss, often no avgift) and including them adds noise.

## Known data anomalies (not bugs)

- **Lilla Allmänna gränd 23 (Djurgården)** routinely appears as a +70-85%
  deal. It's tomträtt but Hemnet doesn't expose the ownership form
  reliably (`upplatelseform` filled <2% on onsale), so the model can't see
  the discount-justifier. Document in popup if user surfaces it.
- **Outer-kommun "accepterat pris" listings** sometimes have asks 30-50%
  below model prediction. Common in Hässelby/Spånga/Vårberg — the asking
  is a low anchor for bidding; sold price typically lands near model
  prediction. Not a model bug.
- **Bottom-K of the deal ranking** are usually premium-renovated apartments —
  the model can't see condition. They may sell at asking anyway.
- **`upplatelseform` filled only ~11% on onsale.** Hemnet hides the label when
  default (Bostadsrätt). When `bostadstyp == "Lägenhet"` and `forening` is
  set, you can safely default to Bostadsrätt downstream.
- **Tensta/Spånga area-string conflation.** Hemnet tags sold "Tensta" as
  `"Spånga - Tensta, Stockholms kommun"` → coarse-normalizes to "Spånga".
  Onsale tags the same place as `"Tensta, ..."` → normalizes to "Tensta".
  Fixed by the lat/lon k-NN resolver in `score.py` — predict-time bucket
  comes from nearest sold rows' labels, not the onsale area string.

## Score model — quick reference

**Dual-region model** (apartments only — `bostadstyp == "Lägenhet"`):
- **Inner model** (inom-tullarna stadsdelar): MAPE ~8%, median ~6%, p90 ~16%.
  Coarse stadsdel encoder, ~10 buckets.
- **Outer model** (everything else): MAPE ~14-16%, median ~10%, p90 ~30%.
  Fine stadsdel encoder, ~90 buckets with rare-fold-to-coarse.

Features in both:
- `α_stadsdel` (one-hots, rare bins folded into "Other" at <4 sold rows;
  threshold 4 keeps small suburb buckets like Vårberg with 5 rows out of
  the biased "Other" bucket).
- `log(m²)`, `byggår_decade`, `våning`, `1[hiss]`, `log(avgift+1)`
- Ownership flags: `is_house`, `is_tomratt`, `is_aganderatt`, `is_andel`
  (BR + Lägenhet = reference). These barely move holdout MAPE because the
  apt-only filter leaves little variance, but they're needed at predict time
  for the tomträtt edge cases.

**Routing at predict time**: each onsale row's lat/lon → k=5 nearest sold
neighbors → majority `normalize_stadsdel(area)` → that bucket. The lat/lon
k-NN fixes Hemnet's inconsistent area-string tagging (see Tensta anomaly).

`INOM_TULLARNA` set in `score.py` defines the inner/outer split; includes
coarse parents plus known sub-areas (Västra Kungsholmen, Norra Djurgårdsstaden,
Fredhäll, Norr Mälarstrand, etc.) that `normalize_stadsdel` produces as
their own buckets.

Treat `|deal_pct| < 10%` as noise for inner, `< 20%` for outer.

```bash
python3 score.py --sold data/sold-LATEST.enriched.geo.jsonl --onsale data/onsale-TODAY.enriched.geo.jsonl
```

Note: sold now needs geocoding too (for the k-NN resolver). Re-run
`python3 geocode.py data/sold-YYYY-MM-DD.enriched.jsonl` after a fresh sold
scrape — ~1 minute with Mapbox.

## Experiments

`experiment.py` runs ablation sweeps over stadsdel granularity (coarse vs
fine) × ownership-features-on/off × all-properties-vs-apt-only × single-vs-
split-model. Run it to recalibrate when feature decisions are revisited.

```bash
python3 experiment.py --sold data/sold-LATEST.enriched.geo.jsonl
```

## Cache freshness

| layer | TTL |
|-------|-----|
| `cache/details/sold/`  | ∞ (sold listings are immutable post-sale) |
| `cache/details/onsale/`| 18h (catches asking-price drops, accepted-price flips) |
| `cache/geocode/`       | ∞ (addresses don't move); engine-agnostic — Mapbox and Nominatim entries coexist since the lat/lon delta is meters, which doesn't matter for k-NN-stadsdel |

Override via `python3 enrich.py … --cache-ttl-hours N`. `0` forces full re-fetch.

## Where things live (regenerable, not committed)

```
data/                 # snapshots — date-stamped JSONL per pipeline stage
cache/details/sold/   # immutable per-listing detail facts
cache/details/onsale/ # 18h TTL'd
cache/geocode/        # mapbox or nominatim results, engine-agnostic
```

`.gitignore` excludes `data/` and `cache/`. The deployed `index.html` has the
needed data **embedded** by `build_map.py`, so the site works without the JSONL
files being on the server.

## Filter scope

Currently 6 location_ids: Stockholm (18031), Danderyd (17892), Lidingö
(17846), Nacka (17853), plus 18028 and 18042 (added 2026-05-30 — confirm
with user before naming in docs). History: `898741` (inom-tullarna) until
2026-05-14 → 18031 only (kommun-wide Stockholm) → multi-kommun on
2026-05-30. The kommun-boundary used to be hard (daughter's kommunal
school tied to hemkommun); the user reopened that constraint when
expanding here, so don't argue against multi-kommun listings.

Hemnet pagination caps at 2500 rows; with the wider filter the sold scrape
hits this cap and we get the newest ~7 months instead of 12. Fine for
hedonic — recent comparables matter more anyway.
