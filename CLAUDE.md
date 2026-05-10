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

## Two Chromium instances (required)

| port | purpose | profile dir |
|------|---------|-------------|
| 9222 | sold scrape (manual / monthly) | `/tmp/chromium-playwright` |
| 9223 | onsale scrape (daily, auto)    | `/tmp/chromium-onsale` |

Both need `--remote-debugging-port=N --remote-allow-origins='*'` (Chromium's
default origin policy rejects CDP from `http://localhost:N` otherwise).

## Required env vars

- `HEMNET_USER_AGENT` — Nominatim asks for a real contact email. Set in shell
  rc; **never commit hardcoded value** (geocode.py reads from env by design).
- `HEMNET_CDP_PORT` — which Chromium to drive (default 9222). `update.sh` sets
  it to 9223 for the onsale tab.
- `ONSALE_URL` — overrides the default onsale filter URL in `update.sh`.

## Environment quirks

- **NixOS + fish shell.** No `sklearn`, `statsmodels`, `pandas`. Pure numpy
  only — `score.py` does OLS via `np.linalg.lstsq`. Don't suggest sklearn.
- **Pyright "Import 'cdp' could not be resolved"** — LSP path artifact when
  the project root isn't its workspace root; not a runtime issue. Ignore.
- **One listing reliably times out** (Snickarbacken 7) — quirky page, no fix
  needed. Counted in `failed=`.

## Don't

- Don't put the user's email or personal data back in committed code.
- **Don't push without an explicit user instruction.** `./deploy.sh` is the
  user-authorized path; running it from another context still requires
  consent. `git push` from anywhere else is a surprise.
- Don't backfill sold photos — wasted effort (memory file explains).
- Don't suggest including new-construction listings — explicitly filtered
  out by design (memory file explains).

## Known data anomalies (not bugs)

- **Lilla Allmänna gränd 23 (Djurgården)** routinely appears as a +70% deal.
  Hemnet doesn't expose its tomträtt-style ownership form, so the model can't
  see the discount-justifier. Document in popup if user surfaces it.
- **Bottom-K of the deal ranking** are usually premium-renovated apartments —
  the model can't see condition. They may sell at asking anyway.
- **`upplatelseform` filled only ~11% on onsale.** Hemnet hides the label when
  default (Bostadsrätt). When `bostadstyp == "Lägenhet"` and `forening` is
  set, you can safely default to Bostadsrätt downstream.

## Score model — quick reference

`log(slutpris)` hedonic regression on:
- `α_stadsdel` (one-hots, rare bins folded into "Other" at <8 sold rows)
- `log(m²)`, `byggår_decade`, `våning`, `1[hiss]`, `log(avgift+1)`

Holdout MAPE ~7% (median ~6%, p90 ~14%). Treat `|deal_pct| < 10%` as noise.

```bash
python3 score.py --sold data/sold-LATEST.enriched.jsonl --onsale data/onsale-TODAY.enriched.geo.jsonl
```

## Cache freshness

| layer | TTL |
|-------|-----|
| `cache/details/sold/`  | ∞ (sold listings are immutable post-sale) |
| `cache/details/onsale/`| 18h (catches asking-price drops, accepted-price flips) |
| `cache/geocode/`       | ∞ (addresses don't move) |

Override via `python3 enrich.py … --cache-ttl-hours N`. `0` forces full re-fetch.

## Where things live (regenerable, not committed)

```
data/                 # snapshots — date-stamped JSONL per pipeline stage
cache/details/sold/   # immutable per-listing detail facts
cache/details/onsale/ # 18h TTL'd
cache/geocode/        # nominatim results
```

`.gitignore` excludes `data/` and `cache/`. The deployed `index.html` has the
needed data **embedded** by `build_map.py`, so the site works without the JSONL
files being on the server.
