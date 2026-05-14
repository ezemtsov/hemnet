"""Look up door-to-door public-transit travel time from each listing to
Odenplan via Trafiklab ResRobot 2.1. Modifies the input JSONL in place,
adding `min_to_odenplan` to each row.

Auth: set RESROBOT_KEY env var with your Trafiklab API key. Without it,
the script no-ops (sets min_to_odenplan=None for every row).

Cache: per-listing under cache/route/<listing_id>.json — locations don't
move, so once cached the value is reused forever. Free tier 30 000
req/month easily covers our ~600 listings/day.

Usage:
    python3 route.py data/onsale-2026-05-14.enriched.geo.jsonl
"""
import argparse, hashlib, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache" / "route"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESROBOT_KEY = os.environ.get("RESROBOT_KEY")
# Odenplan T-bana station (Vasastan, green line); coords from OSM.
ODENPLAN_LAT, ODENPLAN_LON = 59.34277, 18.04915


def listing_id(url: str) -> str:
    m = re.search(r"-(\d+)/?$", url or "")
    if m:
        return m.group(1)
    return hashlib.sha1((url or "").encode()).hexdigest()[:16]


def parse_iso_minutes(s: str | None) -> int | None:
    """ISO 8601 duration -> minutes. 'PT31M' -> 31, 'PT1H5M' -> 65."""
    if not s:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return h * 60 + mins


def fetch_minutes(lat: float, lon: float, *, max_attempts: int = 4) -> int | None:
    """ResRobot trip lookup with backoff on transient 401s (the API rate-limits
    bursts by returning 401, not 429). Returns None on persistent failure."""
    if not RESROBOT_KEY:
        return None
    qs = urllib.parse.urlencode({
        "format": "json", "accessId": RESROBOT_KEY,
        "originCoordLat": f"{lat:.5f}", "originCoordLong": f"{lon:.5f}",
        "destCoordLat": ODENPLAN_LAT, "destCoordLong": ODENPLAN_LON,
        "numF": 1,
    })
    url = f"https://api.resrobot.se/v2.1/trip?{qs}"
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r)
            trips = data.get("Trip") or []
            return parse_iso_minutes(trips[0].get("duration")) if trips else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
                continue
            print(f"  ERROR fetch ({lat}, {lon}): {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  ERROR fetch ({lat}, {lon}): {e}", file=sys.stderr)
            return None
    return None


def run(in_path_str: str, out_path_str: str | None = None, *, workers: int = 5):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else in_path
    rows = [json.loads(l) for l in open(in_path)]
    n = len(rows)
    if not RESROBOT_KEY:
        print("RESROBOT_KEY not set — leaving min_to_odenplan null for all rows", file=sys.stderr)
    print(f"route.py: {n} rows, workers={workers}", flush=True)

    found = [0]; nulls = [0]; done = [0]
    lock = threading.Lock()

    def process(idx_row):
        idx, row = idx_row
        lat, lon = row.get("lat"), row.get("lon")
        if lat is None or lon is None:
            mins = None
        else:
            lid = listing_id(row.get("href", ""))
            cache_path = CACHE_DIR / f"{lid}.json"
            if cache_path.exists():
                mins = json.loads(cache_path.read_text()).get("min_to_odenplan")
            else:
                mins = fetch_minutes(lat, lon)
                if mins is not None:
                    cache_path.write_text(json.dumps({"min_to_odenplan": mins}))
        with lock:
            done[0] += 1
            if mins is not None: found[0] += 1
            else: nulls[0] += 1
            if done[0] % 50 == 0 or done[0] <= 3 or done[0] == n:
                print(f"[{done[0]}/{n}] found={found[0]} null={nulls[0]}", flush=True)
        return idx, mins

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(process, list(enumerate(rows))))

    for idx, mins in results:
        rows[idx]["min_to_odenplan"] = mins

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp_path.replace(out_path)
    print(f"DONE. wrote {n} (found {found[0]}, null {nulls[0]}) -> {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out", help="defaults to overwriting input")
    p.add_argument("--workers", type=int, default=5, help="parallel API workers (free tier ~5 rps)")
    args = p.parse_args()
    run(args.input, args.out, workers=args.workers)


if __name__ == "__main__":
    main()
