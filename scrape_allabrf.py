"""One-shot scraper for allabrf.se. Builds a per-BRF cache (tomträtt, äkta,
antal lägenheter, årsavgift, addresses) keyed by org-number so it survives
slug changes. The data is stable for years — re-run only when the model
needs fresher BRF financials.

Pipeline:
  1. Download address sitemaps (~5 × 50k) once into cache/details/allabrf/sitemaps/.
  2. Collect unique addresses from sold + onsale data files.
  3. For each address, generate a candidate allabrf slug and verify against
     the local sitemap (no network needed).
  4. For matched addresses, fetch the allabrf address page in Chromium to
     extract the parent org-number.
  5. For each unique org-number, fetch /summering and parse:
       - brf_akta            ← "äkta"/"oäkta" in summary text
       - brf_ager_marken     ← inverse of "tomträtt" appearing in body
       - brf_n_lgh           ← "Antal lägenheter" table row
       - brf_byggar          ← "Föreningen bildades år XXXX"
       - brf_arsavgift_kr_m2 ← year + value from "Årsavgift per m²" chart
       - organisationsnummer
       - addresses[]         ← derived from the BRF page's address list

Outputs:
  cache/details/allabrf/by_org/<orgnr>.json       — one BRF, full record
  cache/details/allabrf/addr_to_org.json          — slug-address → orgnr

Both are infinitely cacheable. Re-run is incremental: existing org files are
skipped unless --force.

Usage:
  python3 scrape_allabrf.py \
      --addresses-from data/sold-*.enriched.geo.jsonl data/live-*.enriched.geo*.jsonl

Env:
  HEMNET_CDP_PORT (default 9223) — uses a single Chromium tab. Allabrf serves
  recaptcha v3 silently; one polite worker (≥0.5s between fetches) avoids it.
"""
import argparse, glob, gzip, json, os, queue, re, threading, time, urllib.request
from pathlib import Path

from cdp import CDP, find_tab

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "cache" / "details" / "allabrf"
SITEMAP_DIR = CACHE_DIR / "sitemaps"
BY_ORG_DIR = CACHE_DIR / "by_org"
ADDR_TO_ORG_PATH = CACHE_DIR / "addr_to_org.json"

ADDR_SITEMAPS = [
    f"https://allabrf-documents.s3.amazonaws.com/sitemaps/addresses{i}.xml.gz"
    for i in ["", "1", "2", "3", "4", "5"]
]


def slug_from_street(addr: str | None) -> str | None:
    """Hemnet address ("Storgatan 5, 2 tr") → allabrf slug ("storgatan-5").
    Strips floor/Lgh suffixes and ASCII-folds Swedish chars."""
    if not addr:
        return None
    s = addr.split(",")[0]                       # drop "2 tr" / "Lgh 1101"
    s = re.sub(r"\b(lgh|våning|våning|vån)\s*\d+", "", s, flags=re.I)
    s = s.lower()
    s = (s.replace("å", "a").replace("ä", "a").replace("ö", "o")
           .replace("é", "e").replace("ü", "u"))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or None


def download_sitemaps():
    """Fetch each addresses-sitemap once into local cache."""
    SITEMAP_DIR.mkdir(parents=True, exist_ok=True)
    for url in ADDR_SITEMAPS:
        name = url.rsplit("/", 1)[-1]
        out = SITEMAP_DIR / name
        if out.exists():
            continue
        print(f"  downloading {name} …", flush=True)
        urllib.request.urlretrieve(url, out)
        time.sleep(0.5)


def load_address_slug_set() -> set[str]:
    """Read all sitemap .gz files into one big in-memory set of address slugs.
    Sitemaps are emitted as one giant line of XML so we do a single findall
    per file rather than per-line scanning."""
    slugs: set[str] = set()
    rx = re.compile(r"<loc>https?://[^/]*/([^<]+)</loc>")
    for fp in sorted(SITEMAP_DIR.glob("addresses*.xml.gz")):
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            slugs.update(rx.findall(f.read()))
    return slugs


def collect_addresses(paths: list[str]) -> dict[str, dict]:
    """Walk input JSONL files; return {slug: {"address": ..., "area": ...}}.
    Skips rows in non-Stockholm kommuns (allabrf has them but for now we only
    care about the search area)."""
    seen: dict[str, dict] = {}
    for pat in paths:
        for fp in glob.glob(pat):
            for line in open(fp):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                area = r.get("area") or ""
                if "Stockholms kommun" not in area:
                    continue  # narrow scope; expand later if needed
                slug = slug_from_street(r.get("address"))
                if not slug or slug in seen:
                    continue
                seen[slug] = {"address": r.get("address"), "area": area}
    return seen


def _wait_for(predicate, timeout_s: float = 15.0, poll_s: float = 0.3) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def _cdp_ports() -> list[int]:
    """Match enrich.py's convention. HEMNET_CDP_PORTS (comma-sep) takes
    precedence; otherwise fall back to the single HEMNET_CDP_PORT default."""
    raw = os.environ.get("HEMNET_CDP_PORTS")
    if raw:
        return [int(p) for p in raw.split(",") if p.strip()]
    return [int(os.environ.get("HEMNET_CDP_PORT", "9223"))]


def run_parallel(tasks: list, ports: list[int], fn, label: str,
                 on_result, delay_s: float, log_every: int = 25):
    """Run `fn(cdp, task) -> result` over `tasks` using one worker per CDP
    port. `on_result(task, result)` is called under a lock so it can mutate
    shared state safely.

    Each worker grabs a tab on its port, then loops the queue until drained.
    A short `delay_s` between requests per worker keeps allabrf happy."""
    q: queue.Queue = queue.Queue()
    for t in tasks:
        q.put(t)
    counter = {"done": 0, "n": len(tasks)}
    lock = threading.Lock()

    def worker(port: int):
        try:
            cdp = CDP(find_tab("allabrf", f"http://localhost:{port}"))
        except Exception:
            # Tab is currently elsewhere (e.g. hemnet); grab any tab on this
            # port and steer it to allabrf so future navigations stay there.
            cdp = CDP(find_tab("://", f"http://localhost:{port}"))
            cdp.navigate("https://www.allabrf.se/")
            time.sleep(0.5)
        while True:
            try:
                task = q.get_nowait()
            except queue.Empty:
                return
            try:
                result = fn(cdp, task)
            except Exception as e:
                with lock:
                    print(f"  [{label} err] {task}: {e}", flush=True)
                result = None
            with lock:
                on_result(task, result)
                counter["done"] += 1
                if counter["done"] % log_every == 0 or counter["done"] <= 3:
                    print(f"  [{label} {counter['done']}/{counter['n']}] {task} → {result}", flush=True)
            time.sleep(delay_s)

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


_ORG_RE = re.compile(r"\borganisationsnummer\s*[:\-]?\s*(\d{6}-\d{4})", re.I)


def resolve_addr_to_brf(cdp, addr_slug: str) -> dict | None:
    """Visit https://www.allabrf.se/<addr_slug> and pull the parent BRF's
    canonical URL slug + (where available) organisationsnummer. Returns
    {"slug": "<brf-slug>", "orgnr": "NNNNNN-NNNN"|None} or None for empty /
    un-mapped address pages. Some BRFs are routed by named slug, others by
    org-nr; we keep whichever the address page links to."""
    url = f"https://www.allabrf.se/{addr_slug}"
    cdp.navigate(url)
    if not _wait_for(lambda: len(cdp.eval("document.body.innerText") or "") > 500):
        return None
    body = cdp.eval("document.body.innerText") or ""
    if "Sidan du letade efter kunde inte hittas" in body or "404" in body[:200]:
        return None
    # The address page links to the parent BRF via its sidebar tabs
    # (/ekonomi, /info_bilder, etc.) — the /summering anchor isn't always
    # present in the DOM. Pick the slug whose first-path-segment is shared
    # across multiple tab links — that's the BRF root.
    tab_hrefs = cdp.eval(
        "[...document.querySelectorAll('a[href]')].map(a => a.href).filter("
        "  h => /allabrf\\.se\\/[^\\/]+\\/(ekonomi|info_bilder|forsaljningar|lagenheter|omradet|styrelse|dokument)\\b/.test(h)"
        ")"
    ) or []
    brf_slug = None
    for href in tab_hrefs:
        m = re.search(r"allabrf\.se/([^/]+)/(?:ekonomi|info_bilder|forsaljningar|lagenheter|omradet|styrelse|dokument)", href)
        if m and m.group(1) != addr_slug:  # ignore self-anchors
            brf_slug = m.group(1)
            break
    if not brf_slug:
        return None
    org_match = _ORG_RE.search(body)
    return {"slug": brf_slug, "orgnr": org_match.group(1) if org_match else None}


_TOMRATT_RE = re.compile(r"tomträtt", re.I)
_AKTA_RE    = re.compile(r"\bäkta bostadsrätt", re.I)
_OAKTA_RE   = re.compile(r"\boäkta bostadsrätt", re.I)
_NLGH_RE    = re.compile(r"Antal\s+lägenheter\s+(\d+)", re.I)
_BYGGAR_RE  = re.compile(r"bildades\s+(?:år\s+)?(\d{4})", re.I)
_ARSAVG_RE  = re.compile(r"Årsavgift per m²\s*\(\s*(\d{4})\s*\)\s*[^\d]+([\d\s]+)", re.I)


def fetch_brf_summering(cdp, brf_slug: str) -> dict | None:
    url = f"https://www.allabrf.se/{brf_slug}/summering"
    cdp.navigate(url)
    # Wait for the JS-rendered description to land (tomträtt clause lives there).
    if not _wait_for(
        lambda: any(k in (cdp.eval("document.body.innerText") or "")
                    for k in ("Antal lägenheter", "Föreningen bildades", "Föreningens betyg")),
        timeout_s=15.0,
    ):
        return None
    body = cdp.eval("document.body.innerText") or ""

    has_tomratt = bool(_TOMRATT_RE.search(body))
    out = {
        "slug": brf_slug,
        "url": url,
        "fetched_at": int(time.time()),
        # Description text always says "tomträtt" when applicable; otherwise
        # assume freehold. (Free-text signal — allabrf doesn't expose a
        # structured Äger-marken field on the summering page.)
        "brf_ager_marken": (not has_tomratt),
        "brf_akta": (False if _OAKTA_RE.search(body) else True if _AKTA_RE.search(body) else None),
    }
    if m := _NLGH_RE.search(body):
        out["brf_n_lgh"] = int(m.group(1))
    if m := _BYGGAR_RE.search(body):
        out["brf_byggar"] = int(m.group(1))
    if m := _ARSAVG_RE.search(body):
        out["brf_arsavgift_kr_m2_year"] = int(m.group(1))
        try:
            out["brf_arsavgift_kr_m2"] = int(m.group(2).replace(" ", ""))
        except ValueError:
            pass

    # Extract BRF name from <h1> or page title.
    name = cdp.eval("(document.querySelector('h1')||{}).textContent || ''") or ""
    out["forening"] = name.strip() or None

    # Capture all street addresses we see in the page so future joins can
    # cross-reference. They appear in the description and in the "Området" tab.
    addr_matches = re.findall(r"\b([A-ZÅÄÖ][a-zA-ZåäöéÉ\-]+(?:vägen|gatan|gränd|stigen|backen|torget|plan|hamnen|allén?|gången|kajen|udden))\s+(\d+(?:[A-Z])?(?:-\d+(?:[A-Z])?)?)", body)
    out["addresses"] = sorted({f"{s} {n}" for s, n in addr_matches})
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--addresses-from", nargs="+", required=True,
                   help="JSONL files (sold + onsale enriched) to pull addresses from")
    p.add_argument("--limit", type=int, default=None,
                   help="cap number of BRF fetches (for smoke-test)")
    p.add_argument("--delay-s", type=float, default=0.6,
                   help="polite delay between allabrf requests")
    p.add_argument("--force", action="store_true",
                   help="re-fetch BRFs that are already cached")
    args = p.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BY_ORG_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. sitemaps ----
    print("▸ download address sitemaps")
    download_sitemaps()
    addr_slugs = load_address_slug_set()
    print(f"  {len(addr_slugs):,} address slugs in sitemap")

    # ---- 2. collect candidates ----
    print("▸ collect addresses from data files")
    cands = collect_addresses(args.addresses_from)
    in_sitemap = {s: meta for s, meta in cands.items() if s in addr_slugs}
    print(f"  {len(cands)} unique street-slugs in data;  {len(in_sitemap)} found in sitemap")
    if args.limit:
        in_sitemap = dict(list(in_sitemap.items())[: args.limit])
        print(f"  --limit truncated to {len(in_sitemap)}")

    # ---- 3. address → brf slug (cached on disk so reruns are cheap) ----
    addr_to_brf = json.loads(ADDR_TO_ORG_PATH.read_text()) if ADDR_TO_ORG_PATH.exists() else {}
    ports = _cdp_ports()
    print(f"  using {len(ports)} Chromium worker(s) on ports {ports}")

    todo_addrs = [s for s in sorted(in_sitemap) if args.force or s not in addr_to_brf]
    print(f"  {len(todo_addrs)} addresses to resolve")

    flush_lock = threading.Lock()
    def on_addr(slug, res):
        addr_to_brf[slug] = res
        # Persist every result so a kill mid-run is recoverable.
        with flush_lock:
            ADDR_TO_ORG_PATH.write_text(json.dumps(addr_to_brf, ensure_ascii=False, indent=2))

    if todo_addrs:
        run_parallel(todo_addrs, ports, resolve_addr_to_brf, "addr", on_addr, args.delay_s)

    resolved_n = sum(1 for v in addr_to_brf.values() if v)
    print(f"  resolved {resolved_n}/{len(addr_to_brf)} addresses to a BRF slug")

    # ---- 4. one fetch per unique BRF ----
    print("▸ fetch BRF summering pages")
    unique_slugs = sorted({v["slug"] for v in addr_to_brf.values() if v})
    todo_brfs = [s for s in unique_slugs if args.force or not (BY_ORG_DIR / f"{s}.json").exists()]
    print(f"  {len(unique_slugs)} unique BRFs; {len(todo_brfs)} need fetch")

    def on_brf(brf_slug, facts):
        if facts is not None:
            (BY_ORG_DIR / f"{brf_slug}.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2))

    if todo_brfs:
        run_parallel(todo_brfs, ports, fetch_brf_summering, "brf", on_brf, args.delay_s, log_every=10)

    # ---- 5. summary ----
    done = sum(1 for _ in BY_ORG_DIR.glob("*.json"))
    print(f"\n✓ done — {done} BRFs cached in {BY_ORG_DIR}")


if __name__ == "__main__":
    main()
