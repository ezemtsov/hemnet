"""One-shot fetcher: download Stockholm public-transit reference points
(T-bana stations + ferry terminals) from Overpass (OSM) and write a
compact JSON to `tbana.json` for embedding in the map.

Run when:
- First time setup
- New stations/terminals open (rare)

    python3 fetch_tbana.py

Output: `tbana.json` with shape
  {
    "stations": [{"name": "...", "lat": ..., "lon": ...}, ...],  # T-bana
    "ferries":  [{"name": "...", "lat": ..., "lon": ...}, ...],  # public ferry
  }

Ferry filter: keep named `amenity=ferry_terminal` entries and skip
tourist operators (Strömma Kanalbolaget, Viking Line, cruise lines) and
the Trafikverket car-ferry network — only SL pendelbåtar and
Waxholmsbolaget archipelago commute routes are useful for a property
buyer judging transit access.
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
    return _dedupe_named_nodes(data.get("elements", []))


# Tourist/private ferry operators to skip — only commuter ferries qualify
# as "public transit" for the property-buyer transit-proximity use case.
_FERRY_SKIP_OPERATORS = {
    "Strömma Kanalbolaget", "Viking Line", "Tallink Silja", "Birka Cruises",
    "Trafikverket",  # vehicle ferries for road network, not passenger transit
}
# Some tourist stops carry the operator in the name instead of the
# operator tag, so we also pattern-match the name field.
_FERRY_SKIP_NAME_PATTERNS = (
    "Viking Line", "Tallink", "Birka,", "Birka ", "Mälarlunch",
    "Mälarparty", "Hop-on", "skoltrafik",
)


def fetch_ferries() -> list[dict]:
    q = (
        f"[out:json][timeout:60];"
        f"("
        f'node["amenity"="ferry_terminal"]({BBOX});'
        f'node["public_transport"="stop_position"]["ferry"="yes"]({BBOX});'
        f");"
        f"out body;"
    )
    data = overpass(q)
    keep: list[dict] = []
    for n in data.get("elements", []):
        tags = n.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        operator = tags.get("operator") or ""
        if any(skip in operator for skip in _FERRY_SKIP_OPERATORS):
            continue
        if any(p in name for p in _FERRY_SKIP_NAME_PATTERNS):
            continue
        keep.append(n)
    return _dedupe_named_nodes(keep)


def _dedupe_named_nodes(nodes: list[dict]) -> list[dict]:
    """Keep one entry per name, preserving first-seen lat/lon."""
    out: list[dict] = []
    seen: set[str] = set()
    for n in nodes:
        name = n.get("tags", {}).get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "lat": round(n["lat"], 5), "lon": round(n["lon"], 5)})
    return out


def main():
    print("Fetching T-bana stations from Overpass…", file=sys.stderr)
    stations = fetch_stations()
    print(f"  {len(stations)} stations", file=sys.stderr)
    print("Fetching public ferry terminals from Overpass…", file=sys.stderr)
    ferries = fetch_ferries()
    print(f"  {len(ferries)} ferry terminals", file=sys.stderr)

    out_path = Path(__file__).parent / "tbana.json"
    payload = {"stations": stations, "ferries": ferries}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path}  ({size_kb:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
