"""Geocode addresses from a listings JSONL.

Two engines:
- **Mapbox** (default when `MAPBOX_ACCESS_TOKEN` env var is set): 50 req/s, parallel
  workers, ~1 minute to geocode 2500 fresh addresses.
- **Nominatim** (fallback when no Mapbox token): 1 req/s, single-threaded. Asks
  for a real contact email in `HEMNET_USER_AGENT`.

Usage:
    python3 geocode.py data/onsale-2026-05-10.enriched.jsonl
    # writes data/onsale-2026-05-10.enriched.geo.jsonl with lat/lon fields

Cache: cache/geocode/<sha1(query)>.json — engine-agnostic so already-cached
queries from one engine are reused by the other (the lat/lon delta is meters,
which doesn't matter for our k-NN-stadsdel use case).
"""
import argparse, json, os, time, hashlib, re, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache" / "geocode"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAPBOX_TOKEN = os.environ.get("MAPBOX_ACCESS_TOKEN")
USER_AGENT = os.environ.get(
    "HEMNET_USER_AGENT",
    "hemnet-personal-search/1.0 (set HEMNET_USER_AGENT env var with contact email)",
)
# Bias Mapbox results toward central Stockholm.
STOCKHOLM_PROXIMITY = "18.07,59.33"


def clean_address(address: str) -> str:
    """Strip floor suffixes, marketing junk, and trailing annotations from a Hemnet address."""
    s = address.strip()
    s = re.split(r"\s+-\s+", s, maxsplit=1)[0]
    while True:
        new = re.sub(
            r",\s*(?:vån\s*\d+(?:/\d+)?|\d+\s*vån|\d+(?:\s*\d/\d)?\s*tr\.?)\s*$",
            "", s, flags=re.I,
        )
        if new == s:
            break
        s = new
    s = re.sub(r",\s*[A-ZÅÄÖ][A-ZÅÄÖ \!\.]+!?$", "", s)
    s = re.sub(r",\s*ink\.\s.*$", "", s, flags=re.I)
    return s.strip().rstrip(",")


def build_queries(address: str | None, area: str | None) -> list[str]:
    """Return queries to try in order — most specific first.

    Hemnet's neighborhood field often concatenates names ("Kungsholmen Fridhemsplan")
    that confuse forward-geocoders, so we fall back to street + Stockholm if the
    neighborhood-qualified query misses.
    """
    if not address:
        return []
    addr = clean_address(address)
    hood = None
    if area:
        hood_part = area.split(",")[0].strip()
        hood = re.split(r"[/\-–]", hood_part)[0].strip()
    queries = []
    if hood:
        queries.append(f"{addr}, {hood}, Stockholm")
    queries.append(f"{addr}, Stockholm")
    addr_no_letter = re.sub(r"(\b\d+)[A-Za-z]\b", r"\1", addr)
    if addr_no_letter != addr:
        queries.append(f"{addr_no_letter}, Stockholm")
    return queries


# ---- Engines --------------------------------------------------------------

def _geocode_nominatim(query: str) -> dict | None:
    params = urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "se",
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": USER_AGENT, "Accept-Language": "sv,en"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        results = json.load(r)
    if not results:
        return None
    top = results[0]
    return {
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top.get("display_name"),
    }


def _geocode_mapbox(query: str) -> dict | None:
    encoded = urllib.parse.quote(query, safe="")
    url = (f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded}.json"
           f"?access_token={MAPBOX_TOKEN}&country=se&limit=1&proximity={STOCKHOLM_PROXIMITY}")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    feats = data.get("features") or []
    if not feats:
        return None
    f = feats[0]
    lon, lat = f["center"]  # Mapbox returns [lon, lat] not [lat, lon]!
    return {
        "lat": float(lat),
        "lon": float(lon),
        "display_name": f.get("place_name"),
    }


def cache_key(query: str) -> str:
    return hashlib.sha1(query.encode()).hexdigest()[:16]


def try_query(q: str, engine_fn, sleep_after: float = 0.0) -> dict | None:
    """Geocode one query with disk cache. Returns the cache entry."""
    cache_path = CACHE_DIR / f"{cache_key(q)}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    try:
        result = engine_fn(q)
    except Exception as e:
        print(f"  ERROR for {q!r}: {e}")
        return None
    entry = {"query": q, "result": result}
    cache_path.write_text(json.dumps(entry, ensure_ascii=False))
    if sleep_after:
        time.sleep(sleep_after)
    return entry


def geocode_listing(address: str | None, area: str | None, engine_fn, sleep_after: float
                    ) -> tuple[dict | None, str | None, int]:
    """Try query candidates in order. Returns (result, used_query, n_attempts)."""
    attempts = 0
    for q in build_queries(address, area):
        attempts += 1
        entry = try_query(q, engine_fn, sleep_after=sleep_after)
        if entry and entry.get("result"):
            return entry["result"], q, attempts
    return None, None, attempts


# ---- Run ------------------------------------------------------------------

def run(in_path_str: str, out_path_str: str | None = None, *, workers: int | None = None):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else in_path.with_suffix(".geo.jsonl")
    rows = [json.loads(l) for l in open(in_path)]

    if MAPBOX_TOKEN:
        engine_fn = _geocode_mapbox
        sleep_after = 0.0
        default_workers = 10
        engine_name = "mapbox"
    else:
        engine_fn = _geocode_nominatim
        sleep_after = 1.1  # Nominatim usage policy: max 1 req/s
        default_workers = 1
        engine_name = "nominatim"
    workers = workers or default_workers
    print(f"engine={engine_name}  workers={workers}  ({len(rows)} rows)", flush=True)

    found = [0]
    nulls = [0]
    done_count = [0]
    results: list[tuple[int, dict | None, str | None]] = [(-1, None, None)] * len(rows)

    def process(idx_row):
        idx, row = idx_row
        result, used_q, _ = geocode_listing(row.get("address"), row.get("area"),
                                            engine_fn, sleep_after)
        done_count[0] += 1
        if result:
            found[0] += 1
        else:
            nulls[0] += 1
        if done_count[0] % 25 == 0 or done_count[0] <= 3 or done_count[0] == len(rows):
            print(f"[{done_count[0]}/{len(rows)}] found={found[0]} nulls={nulls[0]}", flush=True)
        return idx, result, used_q

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, result, used_q in ex.map(process, list(enumerate(rows))):
            results[idx] = (idx, result, used_q)

    with open(out_path, "w") as f:
        for (idx, result, used_q), row in zip(results, rows):
            if result:
                row["lat"] = result["lat"]
                row["lon"] = result["lon"]
                row["geo_query"] = used_q
                row["geo_display_name"] = result.get("display_name")
            else:
                row["geo_query"] = (build_queries(row.get("address"), row.get("area"))[:1] or [None])
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"DONE. wrote {len(rows)} ({found[0]} geocoded, {nulls[0]} null) -> {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out")
    p.add_argument("--workers", type=int, help="override parallel workers (default: 10 mapbox / 1 nominatim)")
    args = p.parse_args()
    run(args.input, args.out, workers=args.workers)


if __name__ == "__main__":
    main()
