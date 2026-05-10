"""Scrape Hemnet list pages — Slutpriser (sold) or Bostäder till salu (on-sale).

The kind is auto-detected from the URL path:
    /salda/bostader → sold pipeline    (data/sold-YYYY-MM-DD.jsonl)
    /bostader       → on-sale pipeline (data/onsale-YYYY-MM-DD.jsonl)

For the on-sale Chromium instance, set HEMNET_CDP_PORT=9223 (default 9222 = sold).

Hemnet caps pagination at 50 pages × 50 cards. Use this for batched re-scrapes
of the same filter; for fuller coverage iterate over sub-areas (separate
location_ids) and union the snapshots.
"""
import argparse, json, time, re
from datetime import date
from cdp import CDP, find_tab

CARD_SEL = '[data-testid="result-list"] [class*="ListingCardContainer_cardWrapper"]'
WAIT_JS = f"document.querySelectorAll({json.dumps(CARD_SEL)}).length"
EXTRACT_JS = (
    "(() => [...document.querySelectorAll(" + json.dumps(CARD_SEL) + ")]"
    ".map(w => ({href: w.querySelector('a')?.getAttribute('href'), text: w.innerText}))"
    ".filter(c => c.href && c.text))()"
)


def num(s):
    """Parse Swedish-style number: '1 650' -> 1650, '2,5' -> 2.5, '60+10' -> 60 (primary)."""
    if s is None:
        return None
    s = s.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if "+" in s:
        s = s.split("+")[0]
    try:
        return int(s) if "." not in s else float(s)
    except ValueError:
        return None


def kind_of(url: str) -> str:
    """Return 'sold' for Slutpriser URLs, 'onsale' for active listing URLs."""
    return "sold" if "/salda/" in url else "onsale"


def parse_card_sold(href: str, text: str) -> dict[str, object]:
    """Parse a single sold-listing card text block into structured fields.

    Card text shape (lines, blanks dropped):
        [Maps Data: Google © 2026]      # optional google-maps overlay leak
        Såld 16 mar. 2026
        [FirstName] / [LastName]        # optional spotlight broker
        Sandhamnsgatan 75C              # address
        Östermalm - Gärdet, Stockholms kommun
        76 m²    (or "60+10 m²" with biarea)
        3 rum    (or "2,5 rum")
        6 181 kr/mån
        [Balkong / Hiss / Uteplats ...] # 0+ tag lines
        Slutpris 6 900 000 kr
        -5 %     (or "+6 %" or "±0 %")
        90 789 kr/m²
        Bergets Ro Fastighetsförmedling # agency
    """
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("Maps Data")]
    rec: dict[str, object] = {"href": "https://www.hemnet.se" + href if href.startswith("/") else href, "raw": text}

    rec["sold_date"] = None
    for i, l in enumerate(lines):
        m = re.match(r"Såld\s+(.+)", l)
        if m:
            rec["sold_date"] = m.group(1).strip().rstrip(".")
            lines.pop(i)
            break

    kommun_idx = next((i for i, l in enumerate(lines) if "kommun" in l.lower()), None)
    if kommun_idx is not None and kommun_idx > 0:
        rec["address"] = lines[kommun_idx - 1]
        rec["area"] = lines[kommun_idx]
        body = lines[kommun_idx + 1:]
    else:
        rec["address"] = lines[0] if lines else None
        rec["area"] = None
        body = lines[1:]

    def find(pat):
        for l in body:
            m = re.search(pat, l)
            if m:
                return m.group(1)
        return None

    rec["m2"] = num(find(r"^([\d,\s+]+)\s*m²"))
    rec["rooms"] = num(find(r"^([\d,\s]+)\s*rum"))
    rec["fee_kr"] = num(find(r"^([\d\s]+)\s*kr/mån"))
    rec["price_kr"] = num(find(r"Slutpris\s+([\d\s]+)\s*kr"))
    rec["kr_per_m2"] = num(find(r"^([\d\s]+)\s*kr/m²"))
    diff_raw = find(r"^([+\-±]\s?\d+|±\s?0)\s*%")
    if diff_raw:
        d = diff_raw.replace(" ", "").replace("±", "")
        rec["price_diff_pct"] = 0 if d in ("0", "") else int(d)
    else:
        rec["price_diff_pct"] = None

    tags, in_tags = [], False
    for l in body:
        if "kr/mån" in l:
            in_tags = True; continue
        if l.startswith("Slutpris"):
            break
        if in_tags and not re.search(r"\d", l):
            tags.append(l)
    rec["tags"] = tags

    rec["agent"] = next(
        (l for l in reversed(body) if not re.search(r"\d|kr|m²|%", l)),
        None,
    )
    return rec


PROMOTION_TAGS = {"Premium", "Plus", "Premium Plus", "Nyproduktion", "Energieffektiv"}
FEATURE_TAGS   = {"Balkong", "Uteplats", "Hiss", "Tomt", "Tomträtt"}


def parse_card_onsale(href: str, text: str) -> dict[str, object]:
    """Parse a single on-sale listing card text block.

    Card text shape (lines, blanks dropped):
        [Visning: "Sön 17 maj kl 13:00"]   # optional next viewing
        Dannemoragatan 16A, 4 tr           # address (may include floor in suffix)
        Vasastan, Stockholms kommun        # area
        7 800 000 kr                       # asking price (no Slutpris/diff)
        71 m²
        2,5 rum
        vån 4/5                            # optional, also as suffix on address
        5 303 kr/mån
        109 859 kr/m²
        {long description excerpt}         # optional
        [Premium / Plus / Nyproduktion]    # 0+ promotion tags
        [Balkong / Uteplats / Hiss]        # 0+ feature tags
        Svensk Fastighetsförmedling …      # agency
    """
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("Maps Data")]
    rec: dict[str, object] = {"href": "https://www.hemnet.se" + href if href.startswith("/") else href, "raw": text}

    # next viewing time (line 1 if it matches the day-name + time pattern)
    rec["visning"] = None
    if lines and re.match(r"^(Idag|Imorgon|Mån|Tis|Ons|Tors|Fre|Lör|Sön|Budgivning)\b", lines[0]):
        rec["visning"] = lines[0]
        lines.pop(0)

    kommun_idx = next((i for i, l in enumerate(lines) if "kommun" in l.lower()), None)
    if kommun_idx is not None and kommun_idx > 0:
        rec["address"] = lines[kommun_idx - 1]
        rec["area"] = lines[kommun_idx]
        body = lines[kommun_idx + 1:]
    else:
        rec["address"] = lines[0] if lines else None
        rec["area"] = None
        body = lines[1:]

    def find(pat):
        for l in body:
            m = re.search(pat, l)
            if m:
                return m.group(1)
        return None

    rec["asking_price_kr"] = num(find(r"^([\d\s]+)\s*kr\s*$"))
    rec["m2"] = num(find(r"^([\d,\s+]+)\s*m²"))
    rec["rooms"] = num(find(r"^([\d,\s]+)\s*rum"))
    rec["fee_kr"] = num(find(r"^([\d\s]+)\s*kr/mån"))
    rec["kr_per_m2"] = num(find(r"^([\d\s]+)\s*kr/m²"))

    # våning: explicit "vån X/Y" line  OR  suffix on address like ", 4 tr"
    vaning_match = next((re.search(r"vån\s*(\d+)\s*/\s*(\d+)", l) for l in body if "vån" in l.lower()), None)
    if vaning_match:
        rec["vaning"] = int(vaning_match.group(1))
        rec["vaning_total"] = int(vaning_match.group(2))
    else:
        addr = rec.get("address") or ""
        m = re.search(r",\s*(\d+)\s*tr\s*$", str(addr))
        rec["vaning"] = int(m.group(1)) if m else None
        rec["vaning_total"] = None

    # tags: any line that's a known promotion or feature label
    promotions, features = [], []
    for l in body:
        if l in PROMOTION_TAGS:
            promotions.append(l)
        elif l in FEATURE_TAGS:
            features.append(l)
    rec["promotions"] = promotions
    rec["tags"] = features

    # description: longest line not matching anything we've already extracted
    structured_chars = re.compile(r"(kr/m|kr/mån|kr$|\bm²\b|\brum\b|\bvån\b)")
    candidates = [
        l for l in body
        if l not in PROMOTION_TAGS and l not in FEATURE_TAGS
        and not structured_chars.search(l)
        and len(l) > 60
    ]
    rec["description"] = max(candidates, key=len) if candidates else None

    # agency: last line that's not a tag and has no digits/units
    rec["agent"] = next(
        (l for l in reversed(body)
         if l not in PROMOTION_TAGS and l not in FEATURE_TAGS
         and not re.search(r"\d|kr|m²", l)),
        None,
    )
    return rec


def parse_card(href: str, text: str, kind: str) -> dict[str, object]:
    return parse_card_sold(href, text) if kind == "sold" else parse_card_onsale(href, text)


def goto_and_wait(cdp: CDP, url: str, *, timeout_s: float = 16.0) -> int:
    """Navigate and wait for at least 5 listing cards to render. Returns count seen."""
    cdp.navigate(url)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.4)
        n = cdp.eval(WAIT_JS) or 0
        if n >= 5:
            time.sleep(0.6)  # let lazy bits settle
            return n
    return 0


def scrape(url: str, out_path: str, *, max_pages: int = 50, delay_s: float = 1.5):
    # Match the tab by URL path family so we don't grab DevTools/about:blank tabs.
    # /bostad covers both /bostader (search) and /bostad/<slug> (detail) — useful
    # because the tab may have been navigated to a detail page during probing.
    tab_substring = "/salda/" if "/salda/" in url else "/bostad"
    kind = kind_of(url)
    cdp = CDP(find_tab(tab_substring))
    seen, total = set(), 0
    sep = "&" if "?" in url else "?"
    with open(out_path, "w") as f:
        for page in range(1, max_pages + 1):
            page_url = url + (f"{sep}page={page}" if page > 1 else "")
            goto_and_wait(cdp, page_url)
            cards = cdp.eval(EXTRACT_JS) or []
            new = 0
            for c in cards:
                if c["href"] in seen:
                    continue
                # Skip new-construction project landing pages — they aggregate units
                # and have no per-unit price/area on the card.
                if "/nybyggnadsprojekt/" in c["href"]:
                    continue
                seen.add(c["href"])
                rec = parse_card(c["href"], c["text"], kind)
                # Skip under-construction / never-occupied new builds — user wants
                # ready-to-use apartments only.
                if "Nyproduktion" in rec.get("promotions", []):
                    continue
                rec["scraped_page"] = page
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total += 1; new += 1
            f.flush()
            print(f"page {page:>2}: {len(cards)} cards rendered, {new} new, total {total}", flush=True)
            if new == 0 and page > 1:  # last page reached
                break
            time.sleep(delay_s)
    print(f"DONE. wrote {total} {kind} listings to {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="Hemnet filter URL (sold or on-sale)")
    p.add_argument("--out", help="Output JSONL path (default: data/{kind}-YYYY-MM-DD.jsonl)")
    p.add_argument("--pages", type=int, default=50, help="Max pages to scrape (Hemnet caps at 50)")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds between page loads")
    args = p.parse_args()
    out = args.out or f"data/{kind_of(args.url)}-{date.today().isoformat()}.jsonl"
    scrape(args.url, out, max_pages=args.pages, delay_s=args.delay)


if __name__ == "__main__":
    main()
