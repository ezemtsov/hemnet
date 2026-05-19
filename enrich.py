"""Enrich a listings JSONL with facts from each listing's detail page.

Usage:
    python3 enrich.py data/sold-2026-05-10.jsonl
    HEMNET_CDP_PORT=9223 python3 enrich.py data/onsale-2026-05-10.jsonl

The kind (sold vs onsale) is auto-detected from the input filename or the
first row's href. Cache directory is namespaced by kind so the same listing
can be enriched as both an active listing and (later) a sold listing without
collision.

**Cache freshness.** Sold listings are immutable post-sale, so the sold cache
has no TTL (re-runs always hit). On-sale listings change daily — asking price
drops, "Budgivning pågår" flips, accepted-price gets posted — so the on-sale
cache has a default TTL of 18 hours. Cache entries older than the TTL are
treated as misses and re-fetched. Override with --cache-ttl-hours.

**Merge precedence.** When the same field exists on both the list-page (fresh
each run) and the detail-page (potentially cached), the fresh list-page value
wins if it's non-null. This matters most for `asking_price_kr` on on-sale.
"""
import argparse, json, os, queue, threading, time, re, hashlib
from pathlib import Path
from cdp import CDP, find_tab

CACHE_ROOT = Path(__file__).parent / "cache" / "details"
DEFAULT_CACHE_TTL_H = {"sold": None, "onsale": 18, "kommande": 18}
# Kommande detail pages have the same structure and URLs as onsale; share the
# cache namespace so a listing that transitions from kommande → onsale doesn't
# get re-fetched.
CACHE_NAMESPACE = {"sold": "sold", "onsale": "onsale", "kommande": "onsale"}


# ---- Sold listings ---------------------------------------------------------

LABELS_SOLD = [
    "Slutpris", "Pris per kvadratmeter", "Utgångspris", "Prisutveckling",
    "Bostadstyp", "Upplåtelseform", "Antal rum", "Boarea", "Biarea",
    "Balkong", "Uteplats", "Våning", "Byggår", "Avgift", "Driftskostnad",
    "Energiklass", "Antal besök",
]

NAME_MAP_SOLD = {
    "Slutpris": "detail_price_kr",
    "Pris per kvadratmeter": "detail_kr_per_m2",
    "Utgångspris": "asking_price_kr",
    "Prisutveckling": "_prisutveckling_raw",
    "Bostadstyp": "bostadstyp",
    "Upplåtelseform": "upplatelseform",
    "Antal rum": "_rooms_raw",
    "Boarea": "_boarea_raw",
    "Biarea": "_biarea_raw",
    "Balkong": "balkong",
    "Uteplats": "uteplats",
    "Våning": "_vaning_raw",
    "Byggår": "byggar",
    "Avgift": "_avgift_raw",
    "Driftskostnad": "_driftskostnad_raw",
    "Energiklass": "energiklass",
    "Antal besök": "_antal_besok_raw",
}


# ---- On-sale listings ------------------------------------------------------

LABELS_ONSALE = [
    "Bostadstyp", "Upplåtelseform", "Antal rum", "Boarea", "Biarea",
    "Balkong", "Uteplats", "Våning", "Byggår", "Avgift", "Driftskostnad",
    "Energiklass", "Förening", "Pris/m²", "Utgångspris",
]

NAME_MAP_ONSALE = {
    "Bostadstyp": "bostadstyp",
    "Upplåtelseform": "upplatelseform",
    "Antal rum": "_rooms_raw",
    "Boarea": "_boarea_raw",
    "Biarea": "_biarea_raw",
    "Balkong": "balkong",
    "Uteplats": "uteplats",
    "Våning": "_vaning_raw",
    "Byggår": "byggar",
    "Avgift": "_avgift_raw",
    "Driftskostnad": "_driftskostnad_raw",
    "Energiklass": "energiklass",
    "Förening": "forening",
    "Pris/m²": "_kr_per_m2_raw",
    "Utgångspris": "_asking_price_raw",
}


# BRF section is BELOW the main facts panel (after Visningstider/Räkna),
# so the standard extract_facts slice misses it. Extracted separately
# from the full body. Equally applicable to sold and onsale listings.
LABELS_BRF = ["Äkta förening", "Antal lägenheter", "Årsavgift", "Belåning"]
NAME_MAP_BRF = {
    "Äkta förening":    "brf_akta_raw",        # value usually "Äger marken"
    "Antal lägenheter": "_brf_n_lgh_raw",
    "Årsavgift":        "_brf_arsavgift_raw",  # "458 kr/m²"
    "Belåning":         "_brf_belaning_raw",   # "7 kr/m²" (= per-m² debt)
}


def num(s, *, allow_decimal=False):
    if s is None: return None
    s = s.replace("\xa0", " ").replace(" ", "").replace(",", "." if allow_decimal else "")
    m = re.match(r"^-?\d+(?:\.\d+)?", s)
    if not m: return None
    v = m.group()
    return float(v) if allow_decimal and "." in v else int(float(v))


def kind_of(url: str) -> str:
    if "/salda/" in url:    return "sold"
    if "/kommande/" in url: return "kommande"
    return "onsale"


# Bostadstyp fallback: ~2% of onsale detail pages don't render the explicit
# "Bostadstyp" label, so parse_facts leaves the field None. The description
# usually names the type ("välkommen till denna radhus..."); failing that,
# the Hemnet URL encodes it as the path segment after /bostad/.
_BOSTADSTYP_DESC_PATTERNS = [
    (re.compile(r"\b(parvilla|parhus)\b", re.I),     "Parhus"),
    (re.compile(r"\b(kedjehus|kedjevilla)\b", re.I), "Kedjehus"),
    (re.compile(r"\b(gavelradhus|radhus)\b", re.I),  "Radhus"),
    (re.compile(r"\bvilla\b", re.I),                 "Villa"),
    (re.compile(r"\b(lägenhet|lägenheten)\b", re.I), "Lägenhet"),
]
_BOSTADSTYP_URL_HINT = {
    "lagenhet": "Lägenhet", "villa": "Villa", "radhus": "Radhus",
    "parhus": "Parhus",     "kedjehus": "Kedjehus",
}


def infer_bostadstyp(url: str | None, description: str | None) -> str | None:
    if description:
        for rx, label in _BOSTADSTYP_DESC_PATTERNS:
            if rx.search(description):
                return label
    m = re.search(r"/bostad/([a-z]+)-", url or "")
    return _BOSTADSTYP_URL_HINT.get(m.group(1)) if m else None


def listing_id(url: str) -> str:
    """Extract trailing numeric id from a Hemnet listing URL.

    Sold:   .../sandhamnsgatan-75c-2759425130997191301 -> '2759425130997191301'
    Onsale: .../dannemoragatan-16a,-4-tr-21721260      -> '21721260'
    """
    m = re.search(r"-(\d+)/?$", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def extract_facts(body_text: str, labels: list[str]) -> dict:
    """Slice the facts panel and parse Label / Value pairs."""
    end = re.search(r"All information om bostaden|Räkna på ditt nya boende|Visningstider", body_text)
    panel = body_text[:end.start()] if end else body_text
    lines = [l.strip() for l in panel.split("\n")]
    raw: dict[str, str] = {}
    for i, l in enumerate(lines):
        if l in labels and l not in raw:
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j]:
                    raw[l] = lines[j]
                    break
    return raw


def extract_brf_facts(body_text: str) -> dict:
    """Pull BRF financial labels from the full page body. They live below the
    main facts panel (after Visningstider / Räkna på ditt nya boende), which
    extract_facts slices off — so we scan the full body, scoped by the labels
    being unique enough not to false-match."""
    lines = [l.strip() for l in body_text.split("\n")]
    raw: dict[str, str] = {}
    for i, l in enumerate(lines):
        if l in LABELS_BRF and l not in raw:
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j]:
                    raw[l] = lines[j]
                    break
    return raw


def parse_brf_facts(raw: dict) -> dict:
    out: dict = {NAME_MAP_BRF[k]: v for k, v in raw.items() if k in NAME_MAP_BRF}
    if (s := out.pop("brf_akta_raw", None)):
        # Hemnet shows "Äger marken" when the BRF is äkta and owns the land
        # (no tomträtt). Tomträtt BRFs show "Tomträtt" in this slot.
        out["brf_akta"] = "äger marken" in s.lower()
        out["brf_marknad_raw"] = s
    if (s := out.pop("_brf_n_lgh_raw", None)):
        out["brf_n_lgh"] = num(s)
    if (s := out.pop("_brf_arsavgift_raw", None)):
        out["brf_arsavgift_kr_m2"] = num(s.replace("kr/m²", ""))
    if (s := out.pop("_brf_belaning_raw", None)):
        out["brf_belaning_kr_m2"] = num(s.replace("kr/m²", ""))
    return out


def parse_facts(raw: dict, kind: str) -> dict:
    name_map = NAME_MAP_SOLD if kind == "sold" else NAME_MAP_ONSALE
    out: dict = {name_map[k]: v for k, v in raw.items() if k in name_map}

    if (s := out.pop("_rooms_raw", None)):
        out["detail_rooms"] = num(s.replace(" rum", ""), allow_decimal=True)
    if (s := out.pop("_boarea_raw", None)):
        out["boarea_m2"] = num(s.replace(" m²", ""), allow_decimal=True)
    if (s := out.pop("_biarea_raw", None)):
        out["biarea_m2"] = num(s.replace(" m²", ""), allow_decimal=True)
    if (s := out.pop("_avgift_raw", None)):
        out["avgift_kr_mon"] = num(s.replace("kr/mån", ""))
    if (s := out.pop("_driftskostnad_raw", None)):
        out["drift_kr_year"] = num(s.replace("kr/år", ""))
    if (s := out.pop("_antal_besok_raw", None)):
        out["antal_besok"] = num(s)
    if (s := out.pop("_kr_per_m2_raw", None)):
        out["detail_kr_per_m2"] = num(s.replace("kr/m²", ""))
    if (s := out.pop("_asking_price_raw", None)):
        out["asking_price_kr"] = num(s.replace("kr", ""))
    if (s := out.pop("_vaning_raw", None)):
        if (m := re.match(r"\s*(\d+)\s*av\s*(\d+)", s)):
            out["vaning"] = int(m.group(1))
            out["vaning_total"] = int(m.group(2))
        elif (m := re.match(r"\s*(\d+)\s*tr", s)):
            out["vaning"] = int(m.group(1))
        out["hiss"] = "hiss finns" in s.lower()
    if (s := out.pop("_prisutveckling_raw", None)):
        m_kr = re.search(r"([+\-−])\s*([\d \s]+?)\s*kr", s)
        m_pct = re.search(r"\(([+\-−±])\s*(\d+)\s*%\)", s)
        if m_kr and (v := num(m_kr.group(2))) is not None:
            sign = -1 if m_kr.group(1) in ("-", "−") else 1
            out["asking_diff_kr"] = sign * v
        if m_pct:
            sign = -1 if m_pct.group(1) in ("-", "−") else 1
            out["asking_diff_pct"] = sign * int(m_pct.group(2))

    for k in ("detail_price_kr", "detail_kr_per_m2", "asking_price_kr", "byggar"):
        if k in out and isinstance(out[k], str):
            out[k] = num(out[k])
    for k in ("balkong", "uteplats"):
        if k in out and isinstance(out[k], str):
            out[k] = out[k].strip().lower() == "ja"
    return out


def ready_js_for(kind: str) -> str:
    if kind == "sold":
        return ("(document.body.innerText.match(/Boarea/g)||[]).length >= 1 && "
                "(document.body.innerText.match(/Slutpris/g)||[]).length >= 2")
    # On-sale / kommande: facts panel rendered when Boarea is in body text.
    # We previously also required "Avgift", but house listings on
    # Äganderätt have no monthly BR fee → that label never renders →
    # fetch_facts hit its 21s timeout and produced empty facts (no photos).
    return "(document.body.innerText.match(/Boarea/g)||[]).length >= 1"


PHOTOS_JS = r"""
(() => {
  const seen = new Set();
  const out = [];
  for (const i of document.querySelectorAll('img')) {
    const src = i.src || '';
    if (!src.includes('bilder.hemnet.se')) continue;
    // Dedupe by the image's content hash (the 32-char filename).
    const m = src.match(/([0-9a-f]{32})\.jpg/);
    const key = m ? m[1] : src;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({src, alt: i.alt || ''});
  }
  return out;
})()
"""


def extract_photos(items: list[dict]) -> list[str]:
    """Order photos by 'bild N' index in alt text; floor plans labelled 'planritning' last."""
    def order_key(it):
        alt = (it.get("alt") or "").lower()
        if "planritning" in alt or "ritning" in alt:
            return (1, 0)  # floor plans after photos
        m = re.search(r"bild\s+(\d+)", alt)
        return (0, int(m.group(1)) if m else 999)
    return [it["src"] for it in sorted(items, key=order_key)]


# JSON-LD parsing for the listing publication date. Hemnet emits a couple of
# schema.org blocks per detail page; the Event block carries an Offer whose
# `validFrom` is when the listing went live (independent of when our scraper
# first saw it, so accurate even for listings older than the daily ledger).
JSONLD_JS = ("[...document.querySelectorAll('script[type=\"application/ld+json\"]')]"
             ".map(s=>s.textContent)")


def extract_published_at(scripts: list[str]) -> str | None:
    """Return an ISO date string (YYYY-MM-DD) from the first JSON-LD block
    whose Offer has a validFrom, or None if not present."""
    for raw in scripts or []:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            offer = (obj or {}).get("offers") if isinstance(obj, dict) else None
            if isinstance(offer, dict):
                vf = offer.get("validFrom")
                if isinstance(vf, str) and len(vf) >= 10:
                    return vf[:10]
    return None


def fetch_facts(cdp: CDP, url: str, kind: str, *, timeout_s: float = 21.0) -> dict | None:
    cdp.navigate(url)
    ready = ready_js_for(kind)
    labels = LABELS_SOLD if kind == "sold" else LABELS_ONSALE
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.35)
        if cdp.eval(ready):
            time.sleep(0.3)
            body = cdp.eval("document.body.innerText")
            facts = parse_facts(extract_facts(body, labels), kind)
            facts.update(parse_brf_facts(extract_brf_facts(body)))
            facts["photos"] = extract_photos(cdp.eval(PHOTOS_JS) or [])
            facts["published_at"] = extract_published_at(cdp.eval(JSONLD_JS) or [])
            return facts
    return None


def merge_row_with_facts(row: dict, facts: dict) -> dict:
    """Merge list-page row (fresh) with detail-page facts (possibly cached).

    Fresh row values win when both have the field AND row's value is non-null.
    This keeps daily on-sale price changes from being shadowed by stale cache.
    Also fills bostadstyp from description/URL when the detail page omitted it.
    """
    out = dict(facts)
    for k, v in row.items():
        if v is not None or k not in out:
            out[k] = v
    if not out.get("bostadstyp"):
        inferred = infer_bostadstyp(out.get("href"), out.get("description"))
        if inferred:
            out["bostadstyp"] = inferred
    return out


def cache_is_fresh(path: Path, ttl_hours: float | None) -> bool:
    if not path.exists():
        return False
    if ttl_hours is None:
        return True  # no TTL => always fresh
    age_s = time.time() - path.stat().st_mtime
    return age_s < ttl_hours * 3600


def _cdp_ports() -> list[int]:
    """Read pool size from env. HEMNET_CDP_PORTS (comma-sep) takes precedence;
    otherwise the single HEMNET_CDP_PORT (or its default 9222) is used."""
    raw = os.environ.get("HEMNET_CDP_PORTS")
    if raw:
        return [int(p) for p in raw.split(",") if p.strip()]
    return [int(os.environ.get("HEMNET_CDP_PORT", "9222"))]


def _process_row(cdp: CDP, row: dict, cache_dir: Path, kind: str, ttl_h: float | None,
                 delay_s: float) -> tuple[dict, str]:
    """Cache-or-fetch one listing. Returns (merged_row, status) where status is
    one of: hits / stale_refetched / fetched_new / failed."""
    url = row["href"]
    cache_path = cache_dir / f"{listing_id(url)}.json"
    if cache_is_fresh(cache_path, ttl_h):
        facts = json.loads(cache_path.read_text())
        return merge_row_with_facts(row, facts), "hits"
    had_cache = cache_path.exists()
    facts = fetch_facts(cdp, url, kind)
    if facts is None:
        # URL- and description-based bostadstyp inference still runs via merge.
        return merge_row_with_facts(row, {}), "failed"
    cache_path.write_text(json.dumps(facts, ensure_ascii=False))
    time.sleep(delay_s)
    return merge_row_with_facts(row, facts), ("stale_refetched" if had_cache else "fetched_new")


def enrich(in_path_str: str, out_path_str: str | None = None, *,
           delay_s: float = 1.5, cache_ttl_hours: float | None = None):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else in_path.with_suffix(".enriched.jsonl")

    rows = [json.loads(l) for l in open(in_path)]
    if not rows:
        print(f"empty input {in_path}"); return
    if   "kommande" in in_path.name: kind = "kommande"
    elif "sold"     in in_path.name: kind = "sold"
    elif "onsale"   in in_path.name: kind = "onsale"
    else:                            kind = kind_of(rows[0]["href"])
    cache_dir = CACHE_ROOT / CACHE_NAMESPACE[kind]
    cache_dir.mkdir(parents=True, exist_ok=True)

    ttl_h = cache_ttl_hours if cache_ttl_hours is not None else DEFAULT_CACHE_TTL_H.get(kind)
    ports = _cdp_ports()
    n = len(rows)

    work_q: queue.Queue = queue.Queue()
    results: list[dict | None] = [None] * n
    counter = {"hits": 0, "stale_refetched": 0, "fetched_new": 0, "failed": 0, "done": 0}
    lock = threading.Lock()

    def worker(port: int):
        cdp = CDP(find_tab("hemnet.se", f"http://localhost:{port}"))
        while (item := work_q.get()) is not None:
            idx, row = item
            try:
                merged, status = _process_row(cdp, row, cache_dir, kind, ttl_h, delay_s)
            except Exception as e:
                merged, status = merge_row_with_facts(row, {}), "failed"
                with lock:
                    print(f"[err] {row.get('href')!r}: {e}", flush=True)
            with lock:
                results[idx] = merged
                counter[status] += 1
                counter["done"] += 1
                if counter["done"] % 50 == 0 or counter["done"] <= 3:
                    print(f"[{counter['done']}/{n}] hits={counter['hits']} "
                          f"stale_refetched={counter['stale_refetched']} "
                          f"fetched_new={counter['fetched_new']} "
                          f"failed={counter['failed']}", flush=True)

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in ports]
    for t in threads: t.start()
    for i, row in enumerate(rows): work_q.put((i, row))
    for _ in ports: work_q.put(None)  # one sentinel per worker
    for t in threads: t.join()

    with open(out_path, "w") as f:
        for r in results:
            if r is not None:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ttl_msg = f"TTL={ttl_h}h" if ttl_h is not None else "TTL=∞"
    print(f"DONE [{kind} {ttl_msg}, pool={len(ports)}]. wrote {n} "
          f"(hits {counter['hits']}, stale_refetched {counter['stale_refetched']}, "
          f"fetched_new {counter['fetched_new']}, failed {counter['failed']}) -> {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Path to listings JSONL produced by scrape.py")
    p.add_argument("--out", help="Output path (default: <input>.enriched.jsonl)")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds between fetches (cache misses only)")
    p.add_argument("--cache-ttl-hours", type=float, default=None,
                   help="Override cache TTL. Defaults: sold=∞, onsale=18h. Use 0 to force re-fetch all.")
    args = p.parse_args()
    enrich(args.input, args.out, delay_s=args.delay, cache_ttl_hours=args.cache_ttl_hours)


if __name__ == "__main__":
    main()
