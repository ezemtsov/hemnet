"""Backfill brf_* fields on sold/onsale rows using the allabrf cache. Hemnet
strips the BRF panel from /salda/ pages, so this is the only way to attach
äkta + tomträtt to historical sold listings — which is what the model needs
for training. Runs against any JSONL with `address` + `area` fields.

Sources of BRF facts, in priority order:
  1. allabrf — `cache/details/allabrf/` (built by `scrape_allabrf.py`). One
     record per BRF, keyed by address slug. Most reliable since allabrf is
     the upstream of Hemnet's own BRF panel.
  2. Same-building onsale cache — for buildings missing from allabrf but
     present elsewhere in our daily-update snapshots, copy `brf_*` from a
     row with the same normalized address + area string.

A row is only modified for keys it doesn't already carry — existing values
from enrich.py win, so the onsale snapshot stays the source of truth on
"this listing right now".

Cross-city slug collisions ("varvsgatan-6" → BRF in Nyköping) are guarded
against by requiring the listing's `area` to look like a plausible match
for the BRF's location (currently: heuristic on BRF slug suffix; replaceable
with a structured kommun field once the scraper captures it).

Usage:
    python3 brf_join.py --sold data/sold-2026-05-14.enriched.geo.jsonl
"""
import argparse, glob, json, re
from pathlib import Path


ROOT = Path(__file__).parent
ALLABRF_DIR = ROOT / "cache" / "details" / "allabrf"
ADDR_TO_BRF_PATH = ALLABRF_DIR / "addr_to_org.json"
BY_ORG_DIR = ALLABRF_DIR / "by_org"

BRF_FIELDS = (
    "brf_akta", "brf_ager_marken", "brf_n_lgh",
    "brf_arsavgift_kr_m2", "brf_belaning_kr_m2",
)

# When a sold listing's `area` mentions one of these tokens we treat it as
# Stockholm-region — any BRF slug whose city-suffix doesn't match is a
# cross-city collision and gets skipped. Cheap heuristic, but eliminates the
# false positives we saw during the scrape (ramundberget / nykoping / tierp / ...).
STOCKHOLM_AREA_TOKENS = (
    "Stockholms kommun", "Sundbybergs kommun", "Solna kommun",
    "Nacka kommun", "Lidingö kommun", "Huddinge kommun", "Järfälla kommun",
)

# BRF slugs whose tail token implies they're located outside the Stockholm region.
# Conservative list — only matches the most common non-Stockholm endings we saw.
_NON_STHLM_SUFFIXES = re.compile(
    r"-(falun|filipstad|tierp|are|nykoping|lund|ramundberget|bruksvallarna"
    r"|malmo|goteborg|uppsala|orebro|umea|gavle|sundsvall|linkoping|jonkoping"
    r"|halmstad|karlstad|vasteras|borlange)\b"
)


def slug_from_street(addr: str | None) -> str | None:
    """Mirror the slug rule in scrape_allabrf.py so we look up the same key
    the scraper wrote (lower / ASCII-folded / hyphenated)."""
    if not addr:
        return None
    s = addr.split(",")[0]
    s = re.sub(r"\b(lgh|våning|våning|vån)\s*\d+", "", s, flags=re.I)
    s = s.lower()
    s = (s.replace("å", "a").replace("ä", "a").replace("ö", "o")
           .replace("é", "e").replace("ü", "u"))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or None


def load_allabrf() -> dict[str, dict]:
    """Return {address_slug: brf_facts} merged from addr→brf mapping + BRF
    cache files. Skips entries where the matched BRF's slug looks non-
    Stockholm (slug-suffix heuristic)."""
    if not ADDR_TO_BRF_PATH.exists():
        return {}
    addr_to_brf = json.loads(ADDR_TO_BRF_PATH.read_text())
    brfs: dict[str, dict] = {}
    out: dict[str, dict] = {}
    for slug, res in addr_to_brf.items():
        if not res:
            continue
        brf_slug = res.get("slug")
        if not brf_slug or _NON_STHLM_SUFFIXES.search(brf_slug):
            continue
        if brf_slug not in brfs:
            fp = BY_ORG_DIR / f"{brf_slug}.json"
            if not fp.exists():
                continue
            try:
                brfs[brf_slug] = json.loads(fp.read_text())
            except Exception:
                continue
        out[slug] = brfs[brf_slug]
    return out


def in_stockholm_region(area: str | None) -> bool:
    return bool(area) and any(tok in area for tok in STOCKHOLM_AREA_TOKENS)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sold", required=True, help="sold-*.enriched.geo.jsonl")
    p.add_argument("--onsale-sources", nargs="*", default=None,
                   help="extra JSONL files to use as fallback BRF sources (defaults to all live-*)")
    args = p.parse_args()

    allabrf = load_allabrf()
    print(f"allabrf: {len(allabrf)} address→BRF mappings after region guard")

    # Secondary source — pull brf_* from any onsale snapshot keyed by (slug, area)
    # so a building that's in our daily cache but not in allabrf still backfills.
    onsale_sources = args.onsale_sources or sorted(glob.glob("data/live-*.enriched.geo*.jsonl"))
    onsale_idx: dict[tuple[str, str], dict] = {}
    for fp in onsale_sources:
        if not Path(fp).exists():
            continue
        for line in open(fp):
            try:
                r = json.loads(line)
            except Exception:
                continue
            slug = slug_from_street(r.get("address"))
            area = r.get("area")
            if not slug or not area:
                continue
            key = (slug, area)
            facts = {k: r.get(k) for k in BRF_FIELDS if r.get(k) is not None}
            if facts and (key not in onsale_idx or len(facts) > len(onsale_idx[key])):
                onsale_idx[key] = facts
    print(f"onsale fallback: {len(onsale_idx)} keyed buildings")

    sold_path = Path(args.sold)
    out_path = sold_path.with_name(sold_path.name.replace(".enriched.geo.jsonl",
                                                          "-brf.enriched.geo.jsonl"))

    counts = {"total": 0, "allabrf_hit": 0, "onsale_hit": 0,
              "no_match": 0, "skipped_region": 0}
    field_counts = {k: 0 for k in BRF_FIELDS}

    with open(out_path, "w") as f:
        for line in open(sold_path):
            r = json.loads(line)
            counts["total"] += 1
            slug = slug_from_street(r.get("address"))
            area = r.get("area")
            facts = None
            source = None
            if slug and in_stockholm_region(area):
                if slug in allabrf:
                    facts = allabrf[slug]
                    source = "allabrf"
                elif (slug, area) in onsale_idx:
                    facts = onsale_idx[(slug, area)]
                    source = "onsale"
            elif slug and not in_stockholm_region(area):
                counts["skipped_region"] += 1
            if facts:
                for k in BRF_FIELDS:
                    v = facts.get(k)
                    if v is not None and r.get(k) is None:
                        r[k] = v
                        field_counts[k] += 1
                counts[f"{source}_hit"] += 1
            else:
                counts["no_match"] += 1
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nmatched {counts['allabrf_hit'] + counts['onsale_hit']}/{counts['total']} sold rows  "
          f"(allabrf={counts['allabrf_hit']}, onsale-fallback={counts['onsale_hit']})")
    print(f"skipped (non-Stockholm region): {counts['skipped_region']}")
    print(f"no match found:                 {counts['no_match']}")
    print(f"\nfields filled:")
    for k, n in field_counts.items():
        print(f"  {k}: +{n}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
