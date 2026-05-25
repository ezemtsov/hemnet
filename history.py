"""Track listings across daily onsale snapshots.

State file: data/history.jsonl, keyed by Hemnet listing id (the trailing
numeric segment of the listing URL — see enrich.listing_id). Records the
first/last day each listing was present on the onsale grid, asking-price
history, and a terminal status:

    active     — still on today's live snapshot
    sold       — vanished from onsale, detail page now shows Slutpris
                 (or has been redirected to /salda/)
    withdrawn  — vanished and unresolved for > WITHDRAW_AFTER_DAYS

Subcommands:

    python3 history.py update --live data/live-YYYY-MM-DD.enriched.geo.scored.jsonl
        Daily incremental update. Marks today's live ids active and refetches
        the detail page once for ids that vanished (CDP via $HEMNET_CDP_PORT,
        default 9223). Adds sold_price_kr + sold_url when resolved.

    python3 history.py rebuild
        Wipe history.jsonl, replay every data/live-*.scored.jsonl in date
        order (no per-day CDP resolve), then resolve all currently-vanished
        ids in one final CDP pass. Reproducible from the daily snapshots alone.

The CDP resolve step needs a Chromium on $HEMNET_CDP_PORT with a Hemnet tab
open — update.sh already has one up by the time history runs. Without it
(e.g. running rebuild manually) pass --no-resolve and run a second pass
later.
"""
import argparse, json, os, re, time
from datetime import date
from pathlib import Path

from enrich import listing_id

ROOT = Path(__file__).parent
HISTORY = ROOT / "data" / "history.jsonl"
WITHDRAW_AFTER_DAYS = 14

# Fields carried from each live row into the active snapshot. Photos are not
# stored here — build_map.py looks them up by id from cache/details/onsale/.
SNAPSHOT_FIELDS = [
    "address", "area", "lat", "lon", "bostadstyp",
    "m2", "rooms", "kr_per_m2", "byggar", "vaning", "vaning_total", "hiss",
    "forening", "predicted_price_kr", "stadsdel_liquidity",
    "brf_akta", "brf_ager_marken", "brf_n_lgh",
    "brf_arsavgift_kr_m2", "brf_belaning_kr_m2",
    "min_to_odenplan", "published_at", "tags", "is_tomratt",
]


def load_history() -> dict[str, dict]:
    if not HISTORY.exists():
        return {}
    return {r["id"]: r for r in (json.loads(l) for l in open(HISTORY))}


def save_history(by_id: dict[str, dict]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(by_id.values(), key=lambda r: r.get("first_seen", ""))
    with open(HISTORY, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def upsert_from_live(rec: dict | None, row: dict, today: str) -> dict:
    """Insert or refresh a history record from a live-snapshot row."""
    ask = row.get("asking_price_kr")
    snap = {k: row.get(k) for k in SNAPSHOT_FIELDS if row.get(k) is not None}
    if rec is None:
        return {
            "id": listing_id(row["href"]),
            "url": row["href"],
            "first_seen": today,
            "last_seen": today,
            "status": "active",
            "asking_history": [[today, ask]] if ask is not None else [],
            "deal_pct_first": row.get("deal_pct"),
            "deal_pct_last": row.get("deal_pct"),
            **snap,
        }
    rec["last_seen"] = today
    rec["deal_pct_last"] = row.get("deal_pct")
    if ask is not None:
        hist = rec.setdefault("asking_history", [])
        if not hist or hist[-1][1] != ask:
            hist.append([today, ask])
    rec.update(snap)
    # Relisting after a vanish: snap back to active, clear resolution traces.
    rec["status"] = "active"
    for k in ("sold_url", "sold_price_kr", "sold_date", "resolved_at", "vanished_since"):
        rec.pop(k, None)
    return rec


# --- CDP resolution of vanished ids ----------------------------------------

# Hemnet shows three signals on the /bostad/ URL after a listing leaves the
# onsale grid:
#   "Borttagen den 18 maj 2026"            — removed (sold OR withdrawn)
#   "har sålts"                            — confirmed sold (price hidden)
#   /salda/ link in page                   — link to the sold detail page
# The /salda/ detail page is where Slutpris is actually rendered. We follow
# the link to extract the final price; if it's not present the listing is
# either still pending or just withdrawn.
_SLUTPRIS_RE = re.compile(r"Slutpris\s*([\d\s\xa0]+?)\s*kr")
_BORTTAGEN_RE = re.compile(r"Borttagen den\s+(\d{1,2})\s+(\w+)\s+(\d{4})")
_SV_MONTHS = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2,
    "mar": 3, "mars": 3, "apr": 4, "april": 4,
    "maj": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
    "aug": 8, "augusti": 8, "sep": 9, "september": 9,
    "okt": 10, "oktober": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_borttagen_date(body: str) -> str | None:
    m = _BORTTAGEN_RE.search(body)
    if not m:
        return None
    day, month, year = int(m.group(1)), _SV_MONTHS.get(m.group(2).lower()), int(m.group(3))
    if not month:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _slutpris_from_body(body: str) -> int | None:
    m = _SLUTPRIS_RE.search(body)
    if not m:
        return None
    raw = m.group(1).replace("\xa0", "").replace(" ", "")
    return int(raw) if raw.isdigit() else None


def _fetch_body(cdp, url: str, timeout_s: float = 12.0) -> tuple[str, str]:
    cdp.navigate(url)
    deadline = time.time() + timeout_s
    body = ""
    while time.time() < deadline:
        time.sleep(0.35)
        body = cdp.eval("document.body.innerText") or ""
        if len(body) > 200:
            break
    final = cdp.eval("location.href") or url
    return final, body


def _find_salda_link(cdp) -> str | None:
    """First /salda/ anchor on the current page, or None."""
    links = cdp.eval(
        '[...document.querySelectorAll(\'a[href*="/salda/"]\')].map(a=>a.href).slice(0,1)'
    ) or []
    return links[0] if links else None


def resolve_vanished(cdp, rec: dict, today: str) -> None:
    """One CDP probe to decide sold/withdrawn for a record no longer on the
    live snapshot. Mutates `rec` in place.

    Probe sequence:
      1. Navigate to the original /bostad/ URL.
      2. If body contains "har sålts" or /salda/ in final URL or "Slutpris":
         it's sold. Follow the /salda/ link (if any) to extract Slutpris;
         otherwise leave sold_price_kr null.
      3. If "Borttagen den …" appears without a sold marker: withdrawn.
      4. Otherwise: stay vanished; flip to withdrawn after grace period.
    """
    try:
        final_url, body = _fetch_body(cdp, rec["url"])
    except Exception as e:
        print(f"[resolve err] {rec['url']}: {e}", flush=True)
        return

    has_sold_marker = (
        "/salda/" in final_url or "har sålts" in body or "Slutpris" in body
    )
    borttagen_date = _parse_borttagen_date(body)

    if has_sold_marker:
        rec["status"] = "sold"
        rec["resolved_at"] = today
        if borttagen_date:
            rec["sold_date"] = borttagen_date
        elif "sold_date" not in rec:
            rec["sold_date"] = today
        # Try to follow the /salda/ link for the actual Slutpris.
        salda_url = final_url if "/salda/" in final_url else _find_salda_link(cdp)
        if salda_url:
            rec["sold_url"] = salda_url
            try:
                _, salda_body = _fetch_body(cdp, salda_url)
                if (price := _slutpris_from_body(salda_body)) is not None:
                    rec["sold_price_kr"] = price
            except Exception as e:
                print(f"[salda fetch err] {salda_url}: {e}", flush=True)
        elif (price := _slutpris_from_body(body)) is not None:
            rec["sold_price_kr"] = price
        rec.pop("vanished_since", None)
        return

    if borttagen_date:
        # Withdrawn (listing removed but no sold marker).
        rec["status"] = "withdrawn"
        rec["resolved_at"] = today
        rec["withdrawn_date"] = borttagen_date
        rec.pop("vanished_since", None)
        return

    # No signal yet — leave vanished; age out to withdrawn after grace period.
    rec.setdefault("vanished_since", today)
    if days_between(rec["vanished_since"], today) >= WITHDRAW_AFTER_DAYS:
        rec["status"] = "withdrawn"
        rec["resolved_at"] = today


def _cdp_session():
    from cdp import CDP, find_tab
    port = int(os.environ.get("HEMNET_CDP_PORT", "9223"))
    return CDP(find_tab("hemnet.se", f"http://localhost:{port}"))


def update_one(live_path: Path, when: str, *, resolve: bool) -> dict:
    today = when
    history = load_history()
    live = [json.loads(l) for l in open(live_path)]

    live_ids: set[str] = set()
    for row in live:
        if not row.get("href"):
            continue
        id_ = listing_id(row["href"])
        live_ids.add(id_)
        history[id_] = upsert_from_live(history.get(id_), row, today)

    # Flip newly-missing actives to vanished. Any record already in
    # "vanished" from a previous day stays in the pool — we keep retrying
    # until it either resolves to sold or ages out to withdrawn.
    for r in history.values():
        if r.get("status") == "active" and r["id"] not in live_ids:
            r["status"] = "vanished"
            r.setdefault("vanished_since", today)
    vanished = [r for r in history.values() if r.get("status") == "vanished"]

    if resolve and vanished:
        print(f"resolving {len(vanished)} vanished via CDP …", flush=True)
        try:
            cdp = _cdp_session()
            for i, r in enumerate(vanished, 1):
                resolve_vanished(cdp, r, today)
                if i % 25 == 0:
                    print(f"  {i}/{len(vanished)} probed", flush=True)
                time.sleep(0.8)
        except Exception as e:
            print(f"[resolve setup err] {e}; skipping resolution this run", flush=True)

    # Withdraw stale vanished entries even when not resolving via CDP.
    for r in history.values():
        if r.get("status") == "vanished" and "vanished_since" in r:
            if days_between(r["vanished_since"], today) >= WITHDRAW_AFTER_DAYS:
                r["status"] = "withdrawn"
                r["resolved_at"] = today

    save_history(history)

    counts: dict[str, int] = {}
    for r in history.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def cmd_update(args: argparse.Namespace) -> None:
    when = args.date or date.today().isoformat()
    counts = update_one(Path(args.live), when, resolve=not args.no_resolve)
    print(f"history {when}: {dict(sorted(counts.items()))}")


_DATE_RE = re.compile(r"live-(\d{4}-\d{2}-\d{2})\.")


def cmd_rebuild(args: argparse.Namespace) -> None:
    if HISTORY.exists():
        HISTORY.unlink()
    files = []
    for p in sorted((ROOT / "data").glob("live-*.enriched.geo.scored.jsonl")):
        m = _DATE_RE.search(p.name)
        if m:
            files.append((m.group(1), p))
    files.sort()
    print(f"replaying {len(files)} daily snapshots …", flush=True)
    for when, p in files:
        counts = update_one(p, when, resolve=False)
        print(f"  {when}  {p.name}  {dict(sorted(counts.items()))}", flush=True)
    if not args.no_resolve and files:
        today = date.today().isoformat()
        history = load_history()
        vanished = [r for r in history.values() if r.get("status") == "vanished"]
        print(f"resolving {len(vanished)} vanished ids via CDP …", flush=True)
        if vanished:
            try:
                cdp = _cdp_session()
                for r in vanished:
                    resolve_vanished(cdp, r, today)
                    time.sleep(0.8)
            except Exception as e:
                print(f"[resolve setup err] {e}", flush=True)
            for r in history.values():
                if r.get("status") == "vanished" and "vanished_since" in r:
                    if days_between(r["vanished_since"], today) >= WITHDRAW_AFTER_DAYS:
                        r["status"] = "withdrawn"
                        r["resolved_at"] = today
            save_history(history)
    counts: dict[str, int] = {}
    for r in load_history().values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"rebuild done: {dict(sorted(counts.items()))}")


def cmd_dump(args: argparse.Namespace) -> None:
    by_id = load_history()
    rows = [r for r in by_id.values() if not args.status or r.get("status") == args.status]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="apply one day's live snapshot")
    u.add_argument("--live", required=True, help="data/live-YYYY-MM-DD.enriched.geo.scored.jsonl")
    u.add_argument("--date", help="override 'today' (YYYY-MM-DD)")
    u.add_argument("--no-resolve", action="store_true", help="skip CDP probe for vanished ids")
    u.set_defaults(func=cmd_update)

    r = sub.add_parser("rebuild", help="wipe + replay all daily snapshots")
    r.add_argument("--no-resolve", action="store_true", help="skip final CDP resolution pass")
    r.set_defaults(func=cmd_rebuild)

    d = sub.add_parser("dump", help="print history.jsonl (optionally filtered)")
    d.add_argument("--status", choices=["active", "vanished", "sold", "withdrawn"])
    d.set_defaults(func=cmd_dump)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
