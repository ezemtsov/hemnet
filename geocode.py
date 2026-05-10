"""Geocode addresses from a listings JSONL via Nominatim (OSM).

Usage:
    python3 geocode.py data/onsale-2026-05-10.enriched.jsonl
    # writes data/onsale-2026-05-10.enriched.geo.jsonl with added lat/lon fields

Uses cache/geocode/<sha1>.json keyed on the normalized query string. Pace is
1 req/s per Nominatim's usage policy. Free, no API key, but please don't run
this in tight loops on the public instance.
"""
import argparse, json, os, time, hashlib, re, urllib.parse, urllib.request
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache" / "geocode"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Nominatim's usage policy asks for a real contact email in the User-Agent.
# Don't hardcode yours — `export HEMNET_USER_AGENT='hemnet/1.0 (you@example.com)'`
USER_AGENT = os.environ.get(
    "HEMNET_USER_AGENT",
    "hemnet-personal-search/1.0 (set HEMNET_USER_AGENT env var with contact email)",
)
DELAY_S = 1.1  # Nominatim asks for max 1 req/s


def clean_address(address: str) -> str:
    """Strip floor suffixes, marketing junk, and trailing annotations from a Hemnet address."""
    s = address.strip()
    # drop anything after " - " (e.g. "Döbelnsgatan 93 - ink. egen parkeringsplats")
    s = re.split(r"\s+-\s+", s, maxsplit=1)[0]
    # drop trailing ", X tr" / ", vån X" / ", X vån" / ", X tr." / ", vån X/Y" floor markers
    while True:
        new = re.sub(
            r",\s*(?:vån\s*\d+(?:/\d+)?|\d+\s*vån|\d+(?:\s*\d/\d)?\s*tr\.?)\s*$",
            "", s, flags=re.I,
        )
        if new == s:
            break
        s = new
    # drop trailing all-caps comments like ", JURIDISK PERSON OK!" or ", ink. ..."
    s = re.sub(r",\s*[A-ZÅÄÖ][A-ZÅÄÖ \!\.]+!?$", "", s)
    s = re.sub(r",\s*ink\.\s.*$", "", s, flags=re.I)
    return s.strip().rstrip(",")


def build_queries(address: str | None, area: str | None) -> list[str]:
    """Return Nominatim queries to try in order — most specific first.

    Hemnet's neighborhood field often concatenates names ("Kungsholmen Fridhemsplan")
    that confuse Nominatim, so we fall back to street + Stockholm if the
    neighborhood-qualified query misses.
    """
    if not address:
        return []
    addr = clean_address(address)
    # extract neighborhood from "Vasastan, Stockholms kommun" → "Vasastan"
    hood = None
    if area:
        hood_part = area.split(",")[0].strip()
        hood = re.split(r"[/\-–]", hood_part)[0].strip()
    queries = []
    if hood:
        queries.append(f"{addr}, {hood}, Stockholm")
    queries.append(f"{addr}, Stockholm")
    # last resort: drop the house-number letter suffix (16A → 16)
    addr_no_letter = re.sub(r"(\b\d+)[A-Za-z]\b", r"\1", addr)
    if addr_no_letter != addr:
        queries.append(f"{addr_no_letter}, Stockholm")
    return queries


def geocode_one(query: str) -> dict | None:
    """Hit Nominatim and return {lat, lon, display_name} or None on miss."""
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
        "osm_type": top.get("osm_type"),
        "place_class": top.get("class"),
    }


def cache_key(query: str) -> str:
    return hashlib.sha1(query.encode()).hexdigest()[:16]


def try_query(q: str) -> dict | None:
    """Geocode a single query, with disk cache. Cached null is retried only if you
    delete the cache file — the fallback ladder above gives us multiple shots already.
    """
    cache_path = CACHE_DIR / f"{cache_key(q)}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    try:
        result = geocode_one(q)
    except Exception as e:
        print(f"  ERROR for {q!r}: {e}")
        return None
    entry = {"query": q, "result": result}
    cache_path.write_text(json.dumps(entry, ensure_ascii=False))
    time.sleep(DELAY_S)
    return entry


def geocode_listing(address: str | None, area: str | None) -> tuple[dict | None, str | None, int]:
    """Try query candidates in order. Return (result_dict, used_query, n_attempts_made)."""
    attempts = 0
    for q in build_queries(address, area):
        attempts += 1
        entry = try_query(q)
        if entry and entry.get("result"):
            return entry["result"], q, attempts
    return None, None, attempts


def run(in_path_str: str, out_path_str: str | None = None):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else in_path.with_suffix(".geo.jsonl")
    rows = [json.loads(l) for l in open(in_path)]
    found = nulls = 0
    with open(out_path, "w") as f:
        for i, row in enumerate(rows, 1):
            result, used_q, _ = geocode_listing(row.get("address"), row.get("area"))
            if result:
                row["lat"] = result["lat"]
                row["lon"] = result["lon"]
                row["geo_query"] = used_q
                row["geo_display_name"] = result.get("display_name")
                found += 1
            else:
                nulls += 1
                row["geo_query"] = build_queries(row.get("address"), row.get("area"))[:1] or [None]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            if i % 25 == 0 or i <= 3 or i == len(rows):
                print(f"[{i}/{len(rows)}] found={found} nulls={nulls}", flush=True)
    print(f"DONE. wrote {len(rows)} ({found} geocoded, {nulls} null) -> {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out")
    args = p.parse_args()
    run(args.input, args.out)


if __name__ == "__main__":
    main()
