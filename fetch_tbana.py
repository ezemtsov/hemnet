"""One-shot fetcher: download Stockholm T-bana station coordinates from
Overpass (OSM) and write a compact JSON to `tbana.json` for embedding in
the map. Stations only — no track polylines (the OSM line modeling has
doubled-track and shared-trunk topology that wasn't worth untangling for
this use case).

Run when:
- First time setup
- A new station opens (rare)

    python3 fetch_tbana.py

Output: `tbana.json` with shape
  {"stations": [{"name": "...", "lat": ..., "lon": ...}, ...]}
"""
import json, urllib.parse, urllib.request, sys
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Stockholm metro area bounding box — covers all current T-bana stations.
BBOX = "59.20,17.80,59.45,18.30"


def overpass(query: str) -> dict:
    req = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "hemnet-tbana-fetch/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def fetch_stations() -> list[dict]:
    q = (
        f"[out:json][timeout:60];"
        f'node["railway"="station"]["station"="subway"]({BBOX});'
        f"out body;"
    )
    data = overpass(q)
    out: list[dict] = []
    seen_names: set[str] = set()
    for n in data.get("elements", []):
        name = n.get("tags", {}).get("name")
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        out.append({
            "name": name,
            "lat": round(n["lat"], 5),
            "lon": round(n["lon"], 5),
        })
    return out


def main():
    print("Fetching T-bana stations from Overpass…", file=sys.stderr)
    stations = fetch_stations()
    print(f"  {len(stations)} stations", file=sys.stderr)

    out_path = Path(__file__).parent / "tbana.json"
    payload = {"stations": stations}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path}  ({size_kb:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
