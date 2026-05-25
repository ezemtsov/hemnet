"""Render a static index.html showing on-sale listings as Leaflet pins.

Usage:
    python3 build_map.py data/onsale-2026-05-10.enriched.geo.jsonl
    python3 build_map.py … --history data/history.jsonl    # adds sold/withdrawn ghosts

Embeds the listing data inline so the file works with file:// (no server needed).
Re-run after each on-sale refresh to regenerate.
"""
import argparse, json
from pathlib import Path

from enrich import listing_id
from score import region_of

ROOT = Path(__file__).parent

POPUP_FIELDS = (
    "href address area asking_price_kr m2 rooms kr_per_m2 byggar vaning vaning_total "
    "hiss forening photos lat lon visning predicted_price_kr deal_pct stadsdel_liquidity "
    "bostadstyp status min_to_odenplan published_at region tags is_tomratt "
    "brf_akta brf_ager_marken brf_n_lgh brf_arsavgift_kr_m2 brf_belaning_kr_m2 "
    "sold_price_kr sold_date sold_url realized_deal_pct first_seen last_seen "
    "asking_history"
).split()

ONSALE_CACHE = ROOT / "cache" / "details" / "onsale"


def trim_row(r: dict) -> dict:
    out = {k: r.get(k) for k in POPUP_FIELDS if r.get(k) is not None}
    # Keep only the first 5 photos to keep the embedded payload reasonable.
    if photos := out.get("photos"):
        out["photos"] = photos[:5]
    return out


def _photos_from_cache(id_: str) -> list[str]:
    """Best-effort photo lookup for a sold/withdrawn ghost row. Reads the
    same cache enrich.py wrote when the listing was active."""
    path = ONSALE_CACHE / f"{id_}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    return (data.get("photos") or [])[:5]


def _ghost_from_history(rec: dict) -> dict | None:
    """Convert a sold/withdrawn history record to a map-ready row. Skips
    rows without coordinates (can't be placed) or without a predicted_price
    (no useful color/calibration signal)."""
    if rec.get("lat") is None or rec.get("lon") is None:
        return None
    last_ask = None
    if rec.get("asking_history"):
        last_ask = rec["asking_history"][-1][1]
    pred = rec.get("predicted_price_kr")
    sold = rec.get("sold_price_kr")
    realized = None
    if rec.get("status") == "sold" and pred and sold:
        realized = round((pred - sold) / sold * 100, 1)
    out = {
        "href":               rec.get("sold_url") or rec["url"],
        "address":            rec.get("address"),
        "area":               rec.get("area"),
        "region":             region_of(rec.get("area")),
        "lat":                rec.get("lat"),
        "lon":                rec.get("lon"),
        "bostadstyp":         rec.get("bostadstyp"),
        "m2":                 rec.get("m2"),
        "rooms":              rec.get("rooms"),
        "kr_per_m2":          rec.get("kr_per_m2"),
        "byggar":             rec.get("byggar"),
        "vaning":             rec.get("vaning"),
        "vaning_total":       rec.get("vaning_total"),
        "hiss":               rec.get("hiss"),
        "tags":               rec.get("tags"),
        "is_tomratt":         bool(rec.get("is_tomratt")) if rec.get("is_tomratt") is not None
                              else (rec.get("brf_ager_marken") is False),
        "brf_ager_marken":    rec.get("brf_ager_marken"),
        "forening":           rec.get("forening"),
        "predicted_price_kr": pred,
        "deal_pct":           rec.get("deal_pct_last"),
        "asking_price_kr":    last_ask,
        "status":             rec["status"],
        "sold_price_kr":      sold,
        "sold_date":          rec.get("sold_date"),
        "sold_url":           rec.get("sold_url"),
        "realized_deal_pct":  realized,
        "first_seen":         rec.get("first_seen"),
        "last_seen":          rec.get("last_seen"),
        "asking_history":     rec.get("asking_history"),
        "published_at":       rec.get("published_at"),
        "min_to_odenplan":    rec.get("min_to_odenplan"),
        "brf_akta":           rec.get("brf_akta"),
        "brf_n_lgh":          rec.get("brf_n_lgh"),
        "brf_arsavgift_kr_m2": rec.get("brf_arsavgift_kr_m2"),
        "brf_belaning_kr_m2":  rec.get("brf_belaning_kr_m2"),
        "photos":             _photos_from_cache(rec["id"]),
    }
    return out


def load_history_ghosts(history_path: Path, live_ids: set[str]) -> list[dict]:
    """Return one ghost row per sold/withdrawn history record whose id is
    not in today's live snapshot."""
    if not history_path.exists():
        return []
    ghosts: list[dict] = []
    for line in open(history_path):
        rec = json.loads(line)
        if rec["id"] in live_ids:
            continue
        if rec.get("status") not in ("sold", "withdrawn"):
            continue
        row = _ghost_from_history(rec)
        if row is not None:
            ghosts.append(row)
    return ghosts


HTML_TEMPLATE = r"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>Hemnet — Stockholm onsale</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
  html, body { height: 100%; margin: 0; font-family: -apple-system, system-ui, sans-serif; }
  body { display: flex; }
  #sidebar { width: 380px; flex: 0 0 380px; height: 100%; overflow-y: auto;
             border-right: 1px solid #ddd; background: #fafafa; }
  #map { flex: 1; height: 100%; }
  #mobile-toggle { display: none; }
  @media (max-width: 720px) {
    #sidebar {
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      width: auto; flex: none; z-index: 10;
      transform: translateX(-100%);
      transition: transform 0.2s ease;
    }
    body.show-sidebar #sidebar { transform: translateX(0); z-index: 1010; }
    #mobile-toggle {
      display: block;
      position: fixed; top: 10px; right: 10px; z-index: 1020;
      background: #fff; border: 1px solid #ccc; border-radius: 4px;
      padding: 7px 11px; font: 600 13px -apple-system, system-ui, sans-serif;
      box-shadow: 0 2px 6px rgba(0,0,0,0.15);
      cursor: pointer; min-width: 80px;
    }
  }
  #sidebar header {
    position: sticky; top: 0; background: white; padding: 10px 14px;
    border-bottom: 1px solid #eee; z-index: 5;
  }
  #sidebar header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  #sidebar header .sub { font-size: 11px; color: #888; margin-top: 2px; }
  #filters { display: flex; flex-direction: column; gap: 5px; margin-top: 8px; }
  .filter-group { display: flex; gap: 6px; }
  .filter-btn {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 8px; height: 26px; box-sizing: border-box;
    border: 1px solid #d4d4d4; border-radius: 4px;
    background: #fff; color: #555;
    font-size: 11px; cursor: pointer;
    transition: background .12s, color .12s, border-color .12s;
  }
  .filter-btn:hover { background: #f3f3f3; }
  .filter-btn.active { background: #2a6df4; color: #fff; border-color: #2a6df4; }
  /* Per-group accent — keeps it obvious which row each button belongs to
     as the filter set grows. Type stays on the default blue. */
  .fg-status    .filter-btn.active { background: #1f9930; border-color: #1f9930; }
  .fg-region    .filter-btn.active { background: #7c3aed; border-color: #7c3aed; }
  .fg-feature   .filter-btn.active { background: #d97706; border-color: #d97706; }
  .fg-ownership .filter-btn.active { background: #444;    border-color: #444;    }
  .filter-btn .count { opacity: 0.7; }
  ul#list { list-style: none; padding: 0; margin: 0; }
  li.row { padding: 10px 14px; border-bottom: 1px solid #eee; cursor: pointer; transition: background .12s; }
  li.row:hover { background: #f0f4fa; }
  li.row.active { background: #e3edff; }
  li.row .top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  li.row .deal { font-weight: 700; font-size: 13px; flex-shrink: 0; }
  .gauge { vertical-align: middle; }
  li.row .price { font-size: 13px; color: #666; }
  li.row .addr { font-weight: 600; font-size: 13px; margin-top: 2px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  li.row .star { color: #e0a012; margin-right: 4px; cursor: help; }
  /* Liked indicator — distinct brighter gold so it doesn't read as the
     liquidity star. Always present in DOM; visibility flipped via .liked
     on the row so toggling is a one-line classList op. */
  li.row .liked-star { color: #f59e0b; margin-right: 4px; font-weight: 700;
                       display: none; }
  li.row.liked .liked-star { display: inline; }
  li.row .ptype { color: #777; margin-right: 5px; vertical-align: -2px; cursor: help; }
  li.row .area { font-size: 11px; color: #888; }
  li.row .meta { font-size: 11px; color: #555; margin-top: 3px; }
  .popup-photo { width: 260px; height: 160px; object-fit: cover; border-radius: 4px; display: block; }
  .popup-section { padding: 8px 0; border-top: 1px solid #eee; }
  .popup-section:first-of-type { border-top: 0; padding-top: 6px; }
  .popup-title { font-weight: 600; font-size: 14px; margin-bottom: 2px; }
  .popup-area { color: #888; font-size: 11px; }
  .popup-price-row { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; }
  .popup-price { font-size: 16px; font-weight: 600; }
  .popup-deal { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
  .popup-meta { font-size: 12px; color: #555; line-height: 1.5; }
  .popup-meta b { color: #222; }
  .popup-brf-head { font-size: 12px; color: #444; margin-bottom: 6px; }
  .popup-brf-stats { display: flex; gap: 6px; }
  .brf-stat { flex: 1; text-align: center; font-size: 11px; color: #666; }
  .brf-stat .brf-val { font-weight: 600; color: #222; }
  .brf-stat .brf-cap { display: block; color: #888; font-size: 10px; margin-top: 1px; }
  .popup-footer { font-size: 11px; color: #666; margin-top: 4px; display: flex; justify-content: space-between; align-items: center; }
  .popup-link { font-size: 11px; color: #2a6df4; text-decoration: none; }
  .popup-link:hover { text-decoration: underline; }
  /* Favorite (star) button — pinned to the top-right of the popup so it's
     reachable without scrolling and doesn't interfere with the photo. */
  .like-btn {
    position: absolute; top: 6px; right: 6px;
    width: 28px; height: 28px; padding: 0;
    border: 1px solid #ccc; border-radius: 50%;
    background: rgba(255,255,255,0.9); color: #888;
    font-size: 18px; line-height: 26px; text-align: center;
    cursor: pointer; z-index: 2;
    transition: color .12s, transform .12s, border-color .12s;
  }
  .like-btn:hover { color: #f59e0b; transform: scale(1.08); }
  .like-btn.liked { color: #f59e0b; border-color: #f59e0b; }
  /* "Seen" eye overlay — a Leaflet permanent tooltip positioned at the
     bottom-right of the marker (offset set per-marker in JS so it tracks
     the radius). Strip Leaflet's chrome and pass pointer events through. */
  .leaflet-tooltip.seen-marker {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    margin: 0 !important;
    font-size: 12px;
    line-height: 1;
    pointer-events: none;
    white-space: nowrap;
    text-shadow: 0 0 2px rgba(255,255,255,0.9);
  }
  .leaflet-tooltip.seen-marker::before { display: none !important; }
  .deal-na   { background: #eee;    color: #777; }
</style>
</head>
<body>
<!-- Shared SVG gradient defs for the BRF gauges. Three-zone hard stops
     at p25/p75 thresholds, anchored at the per-metric scale_max. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <linearGradient id="g-belaning" x1="0" x2="100%">
      <stop offset="0%"    stop-color="#1f9930"/>
      <stop offset="22.7%" stop-color="#1f9930"/>
      <stop offset="22.7%" stop-color="#bea423"/>
      <stop offset="64.7%" stop-color="#bea423"/>
      <stop offset="64.7%" stop-color="#b71f1f"/>
      <stop offset="100%"  stop-color="#b71f1f"/>
    </linearGradient>
    <linearGradient id="g-arsavgift" x1="0" x2="100%">
      <stop offset="0%"    stop-color="#1f9930"/>
      <stop offset="54.7%" stop-color="#1f9930"/>
      <stop offset="54.7%" stop-color="#bea423"/>
      <stop offset="71.5%" stop-color="#bea423"/>
      <stop offset="71.5%" stop-color="#b71f1f"/>
      <stop offset="100%"  stop-color="#b71f1f"/>
    </linearGradient>
    <linearGradient id="g-nlgh" x1="0" x2="100%">
      <stop offset="0%"    stop-color="#bea423"/>
      <stop offset="13%"   stop-color="#bea423"/>
      <stop offset="13%"   stop-color="#888"/>
      <stop offset="46.6%" stop-color="#888"/>
      <stop offset="46.6%" stop-color="#1f9930"/>
      <stop offset="100%"  stop-color="#1f9930"/>
    </linearGradient>
  </defs>
</svg>
<aside id="sidebar">
  <header>
    <h1 id="title">On-sale listings</h1>
    <div class="sub" id="sub"></div>
    <div id="filters"></div>
  </header>
  <ul id="list"></ul>
</aside>
<div id="map"></div>
<button id="mobile-toggle" type="button">≡ List</button>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const LISTINGS = __DATA__;

// Per-user state, persisted in localStorage. `seen` is auto-tracked on
// popup open; `liked` is toggled by the star button in each popup.
// Keyed by listing href (unique per Hemnet ad). v1 suffix lets us bump
// the schema without colliding with old entries.
const STORE_SEEN  = 'hemnet:seen:v1';
const STORE_LIKED = 'hemnet:liked:v1';
function loadStore(key) {
  try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); }
  catch (_e) { return new Set(); }
}
function saveStore(key, set) {
  try { localStorage.setItem(key, JSON.stringify([...set])); }
  catch (_e) { /* quota / disabled — silently no-op */ }
}
const seenSet  = loadStore(STORE_SEEN);
const likedSet = loadStore(STORE_LIKED);

// Restore the user's last map position/zoom so reloads land where they
// left off instead of always re-centering on Stockholm. Saved on every
// moveend below.
const STORE_MAP = 'hemnet:map:v1';
function loadMapState() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE_MAP) || 'null');
    if (v && Number.isFinite(v.lat) && Number.isFinite(v.lon) && Number.isFinite(v.zoom)) return v;
  } catch (_e) {}
  return null;
}
const _savedMap = loadMapState();
const map = L.map('map', { zoomSnap: 0.5 }).setView(
  _savedMap ? [_savedMap.lat, _savedMap.lon] : [59.330, 18.067],
  _savedMap ? _savedMap.zoom : 13,
);
// Esri ArcGIS — one of the few tile providers that serve file:// origins
// without a Referer. If you see a blank map, run `python3 -m http.server`
// in the project root and open http://localhost:8000/ instead.
L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: '&copy; Esri, OpenStreetMap contributors' }
).addTo(map);

// Transit overlay — T-bana stations (dark dots) + public ferry terminals
// (blue dots). Both rendered before listing markers so deal pins stay on top.
const TBANA = __TBANA__;
TBANA.stations.forEach(s => {
  L.circleMarker([s.lat, s.lon], {
    radius: 3.5, color: '#fff', weight: 1.5,
    fillColor: '#666', fillOpacity: 0.85,
  }).bindTooltip(s.name, { direction: 'top', offset: [0, -4] }).addTo(map);
});
(TBANA.ferries || []).forEach(f => {
  L.circleMarker([f.lat, f.lon], {
    radius: 3.5, color: '#fff', weight: 1.5,
    fillColor: '#7aa5cf', fillOpacity: 0.85,
  }).bindTooltip(f.name, { direction: 'top', offset: [0, -4] }).addTo(map);
});

const fmtKr = n => n == null ? '–' : n.toLocaleString('sv-SE') + ' kr';
const fmtM2 = n => n == null ? '–' : Math.round(n) + ' m²';

// Map deal_pct to a hue on the red→yellow→green sweep with tanh compression
// so extreme values don't all collapse to the same colour. Anchored so 0%
// lands at hue 50 (yellow); piecewise so the negative half (red → yellow)
// covers 50 hue units and the positive half (yellow → green) covers 80.
function dealHue(pct) {
  const t = Math.tanh(pct / 30);
  return t <= 0 ? (1 + t) * 50 : 50 + t * 80;
}
function dealColor(pct) {
  if (pct == null) return '#999';
  return `hsl(${dealHue(pct)} 70% 42%)`;
}
function dealStyle(pct) {
  if (pct == null) return 'background:#eee;color:#777';
  const h = dealHue(pct);
  return `background:hsl(${h} 75% 88%);color:hsl(${h} 70% 26%)`;
}

// Property-type icons. Three visual categories matching the model split:
// Lägenhet (apartment building), Villa (single house), everything else
// shares one "row of houses" glyph (Radhus, Parhus, Kedjehus, Par-/kedje-).
const PROPERTY_ICON_SVG = {
  building: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"><rect x="2.5" y="1.5" width="9" height="11"/><line x1="2.5" y1="5" x2="11.5" y2="5"/><line x1="2.5" y1="8.5" x2="11.5" y2="8.5"/><line x1="7" y1="1.5" x2="7" y2="12.5"/></svg>',
  villa:    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"><path d="M1.5 7 L7 1.5 L12.5 7 L12.5 12.5 L1.5 12.5 Z"/></svg>',
  row:      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"><path d="M1 8 L4 4 L7 8 L10 4 L13 8 L13 12.5 L1 12.5 Z"/></svg>',
};
const BOSTADSTYP_ICON = {
  'Lägenhet':           PROPERTY_ICON_SVG.building,
  'Villa':              PROPERTY_ICON_SVG.villa,
  'Radhus':             PROPERTY_ICON_SVG.row,
  'Parhus':             PROPERTY_ICON_SVG.row,
  'Kedjehus':           PROPERTY_ICON_SVG.row,
  'Par-/kedje-/radhus': PROPERTY_ICON_SVG.row,
};

// BRF metric thresholds — anchored at p25/p75 of today's Stockholm apartment
// dataset (n≈250-400 per metric). Hardcoded; recompute against the sold/
// onsale data if/when the distribution shifts.
const BRF_T = {
  belaning:  { good_max:  3400, bad_min:  9700, scale_max: 15000 }, // kr/m² (lower better)
  arsavgift: { good_max:   711, bad_min:   930, scale_max:  1300 }, // kr/m² (lower better)
  nlgh:      { warn_max:    65,  good_min:  233, scale_max:   500 }, // count (mid normal; large = stable)
};
const COLOR_GOOD = '#1e6e26', COLOR_BAD = '#a02020', COLOR_WARN = '#8a6d00', COLOR_NEUTRAL = '#555';

function brfNumColor(value, kind) {
  if (value == null) return COLOR_NEUTRAL;
  const t = BRF_T[kind];
  if (kind === 'nlgh')  return value <  t.warn_max ? COLOR_WARN : value >= t.good_min ? COLOR_GOOD : COLOR_NEUTRAL;
  return value <= t.good_max ? COLOR_GOOD : value > t.bad_min ? COLOR_BAD : COLOR_NEUTRAL;
}

// Speedometer-style arc gauge. Gradient defs (#g-belaning / #g-arsavgift /
// #g-nlgh) live once in <body>; each call returns a 22×22 inline SVG.
function gaugeSvg(value, kind) {
  if (value == null) return '';
  const t = BRF_T[kind];
  const f = Math.max(0, Math.min(1, value / t.scale_max));
  const theta = Math.PI * (1 - f);
  // Square viewBox; arc center (11,15), radius 8 — leaves 3px padding on each side.
  const cx = 11, cy = 15, r = 8;
  const nx = (cx + r * Math.cos(theta)).toFixed(2);
  const ny = (cy - r * Math.sin(theta)).toFixed(2);
  return `<svg class="gauge" viewBox="0 0 22 22" width="22" height="22" aria-hidden="true">`
    + `<path d="M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}" fill="none" stroke="url(#g-${kind})" stroke-width="3" stroke-linecap="round"/>`
    + `<line x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}" stroke="#444" stroke-width="1.4" stroke-linecap="round"/>`
    + `<circle cx="${cx}" cy="${cy}" r="1.6" fill="#444"/>`
    + `</svg>`;
}

function brfNum(value, kind, suffix, thresholdTip) {
  if (value == null) return '';
  return `${gaugeSvg(value, kind)} <b style="color:${brfNumColor(value, kind)}" title="${thresholdTip}">${value.toLocaleString('sv-SE')}</b>${suffix}`;
}
// Commute-time color: <20 great (green), 20-45 normal, >45 red.
function commuteColor(mins) {
  if (mins == null) return COLOR_NEUTRAL;
  if (mins <  20) return COLOR_GOOD;
  if (mins >  45) return COLOR_BAD;
  return COLOR_NEUTRAL;
}

// Listing freshness: days if < 7, otherwise whole weeks. Green when fresh
// (≤ 1 week), amber when stale (> 4 weeks) — stale usually means seller
// is having trouble moving it.
function freshnessFromIso(iso) {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso + 'T00:00:00').getTime();
  return Math.max(0, Math.floor(ms / 86400000));
}
function freshnessLabel(iso) {
  const d = freshnessFromIso(iso);
  if (d == null) return '';
  if (d < 7)  return `${d} ${d === 1 ? 'dag' : 'dagar'}`;
  const w = Math.floor(d / 7);
  return `${w} ${w === 1 ? 'vecka' : 'veckor'}`;
}
function freshnessColor(iso) {
  const d = freshnessFromIso(iso);
  if (d == null) return COLOR_NEUTRAL;
  if (d <= 7)  return COLOR_GOOD;
  if (d >  30) return COLOR_WARN;
  return COLOR_NEUTRAL;
}

function aktaBadge(value) {
  if (value === true)  return `<span style="color:${COLOR_GOOD}" title="Hemnet displays 'Äger marken' — confirmed äkta förening">äkta ✓</span>`;
  if (value === false) return `<span style="color:${COLOR_BAD}">oäkta</span>`;
  return `<span style="color:${COLOR_WARN}" title="Hemnet didn't display the äkta label — could be tomträtt, oäkta, or just missing">äkta?</span>`;
}

function brfStat(value, kind, label, unit, thresholdTip) {
  if (value == null) return '';
  return `<div class="brf-stat" title="${thresholdTip}">
    ${gaugeSvg(value, kind)}
    <div><span class="brf-val" style="color:${brfNumColor(value, kind)}">${value.toLocaleString('sv-SE')}</span>${unit ? ' ' + unit : ''}</div>
    <span class="brf-cap">${label}</span>
  </div>`;
}

function popupHtml(d) {
  const photo = (d.photos && d.photos[0])
    ? `<img class="popup-photo" src="${d.photos[0]}" loading="lazy" alt="">` : '';
  // Liked-state visual is set in the popupopen handler — bindPopup caches
  // the rendered HTML so we sync the icon/class there to reflect whatever
  // the user's latest toggle was.
  const likeBtn = `<button class="like-btn" type="button" title="Spara favorit" aria-label="Spara favorit">☆</button>`;
  const vaning = d.vaning != null
    ? `${d.vaning}${d.vaning_total ? ' av ' + d.vaning_total : ''}${d.hiss ? ', hiss' : ''}`
    : '–';
  const isSold = d.status === 'sold';
  const isWithdrawn = d.status === 'withdrawn';
  // For sold pins the headline metric is realized (sold vs predicted),
  // not asking. Asking_deal goes into a secondary meta line.
  const headlinePct = isSold && d.realized_deal_pct != null ? d.realized_deal_pct : d.deal_pct;
  const dealBadge = headlinePct != null
    ? `<span class="popup-deal" style="${dealStyle(headlinePct)}">${headlinePct > 0 ? '+' : ''}${headlinePct.toFixed(1)}%</span>`
    : '';
  const dealNote = headlinePct != null
    ? `<div class="popup-meta" style="font-size:11px;color:#888;margin-top:2px">${isSold ? 'sold vs predicted' : 'vs predicted'} ${fmtKr(d.predicted_price_kr)}</div>`
    : '';
  const statusBanner = isSold
    ? `<div class="popup-section" style="background:#f3f3f3;padding:6px 8px;border-radius:4px;margin-bottom:4px;border-top:0">
         <b>Slutpris: ${fmtKr(d.sold_price_kr)}</b>
         ${d.asking_price_kr ? ` <span style="color:#888;font-size:11px">(asked ${fmtKr(d.asking_price_kr)})</span>` : ''}
         ${d.sold_date ? `<div style="font-size:11px;color:#888;margin-top:2px">Resolved ${d.sold_date}</div>` : ''}
       </div>`
    : isWithdrawn
    ? `<div class="popup-section" style="background:#f3f3f3;padding:6px 8px;border-radius:4px;margin-bottom:4px;border-top:0;color:#666">
         <b>Withdrawn</b>
         ${d.last_seen ? `<span style="font-size:11px"> · last seen ${d.last_seen}</span>` : ''}
       </div>`
    : '';
  const brfStats = [
    brfStat(d.brf_belaning_kr_m2, 'belaning', 'Belåning', 'kr/m²',
            'Per-m² BRF debt. Stockholm p25=3 376 / p75=9 703. <3 400 healthy; >9 700 elevated fee-hike risk'),
    brfStat(d.brf_arsavgift_kr_m2, 'arsavgift', 'Årsavgift', 'kr/m²',
            'Annual BRF fee per m². Stockholm p25=711 / p75=930. <711 cheap; >930 premium'),
    brfStat(d.brf_n_lgh, 'nlgh', 'Antal lgh', '',
            'Föreningsstorlek — <65 noisy single-decision-maker risk; ≥233 large & stable'),
  ].filter(Boolean).join('');
  const brfSection = (d.forening || brfStats) ? `
    <div class="popup-section">
      ${d.forening ? `<div class="popup-brf-head"><b>BRF:</b> ${d.forening} · ${aktaBadge(d.brf_akta)}</div>` : ''}
      ${brfStats ? `<div class="popup-brf-stats">${brfStats}</div>` : ''}
    </div>` : '';
  return `
    ${likeBtn}
    ${photo}
    <div class="popup-section">
      <div class="popup-title">${d.address ?? ''}</div>
      <div class="popup-area">${d.area ?? ''}</div>
      ${statusBanner}
      <div class="popup-price-row">
        <span class="popup-price">${fmtKr(d.asking_price_kr)}</span>
        ${dealBadge}
      </div>
      ${dealNote}
    </div>
    <div class="popup-section">
      <div class="popup-meta">
        <b>${fmtM2(d.m2)}</b> · ${d.rooms ?? '–'} rum · ${fmtKr(d.kr_per_m2)}/m²<br>
        Byggår: <b>${d.byggar ?? '–'}</b> · Våning: <b>${vaning}</b>${
          d.min_to_odenplan != null ? `<br>Pendling till Odenplan: <b style="color:${commuteColor(d.min_to_odenplan)}">${d.min_to_odenplan} min</b>` : ''
        }${
          d.published_at && !isSold && !isWithdrawn
            ? `<br>Tid på marknaden: <b style="color:${freshnessColor(d.published_at)}">${freshnessLabel(d.published_at)}</b>` : ''
        }
      </div>
    </div>
    ${brfSection}
    <div class="popup-footer">
      <span>${d.visning ? 'Visning: ' + d.visning : ''}</span>
      <a class="popup-link" href="${d.href}" target="_blank" rel="noopener">Hemnet →</a>
    </div>
  `;
}

// --- sort: actives first (newest published_at first, nulls last), then sold, then withdrawn
const STATUS_RANK = { onsale: 0, kommande: 0, sold: 1, withdrawn: 2 };
const sorted = [...LISTINGS].sort((a, b) => {
  const ra = STATUS_RANK[a.status || 'onsale'] ?? 0;
  const rb = STATUS_RANK[b.status || 'onsale'] ?? 0;
  if (ra !== rb) return ra - rb;
  const pa = a.published_at || '';
  const pb = b.published_at || '';
  if (pa === pb) return 0;
  if (!pa) return 1;
  if (!pb) return -1;
  return pb.localeCompare(pa);
});

// --- create one marker + one list row per listing, connected ---------------
// Multiple listings often share a geocode (same building, different floors).
// Markers are rendered in REVERSE deal-priority order so the best deals end
// up on top of the stack when they overlap. Sidebar stays in descending order.
const markers = new Array(sorted.length).fill(null);
const rows    = [];   // index-aligned with `sorted`
const listEl  = document.getElementById('list');

function selectIndex(i, { fromMap = false } = {}) {
  rows.forEach(r => r.classList.remove('active'));
  if (i == null) return;
  const row = rows[i], marker = markers[i];
  if (row) {
    row.classList.add('active');
    row.scrollIntoView({ block: 'nearest' });
  }
  // Map is a geo-filter context (bounds determine which rows show), so
  // row selection must NOT move it. The bounds filter guarantees a
  // visible row's pin is already in view; openPopup with autoPan:false
  // just opens it where it is.
  if (marker) marker.openPopup();
}

// Pass 1: build sidebar rows in attractiveness order.
sorted.forEach((d, i) => {
  const li = document.createElement('li');
  li.className = 'row';
  // Sold rows show realized deal (sold vs predicted); active rows show asking
  // deal. Withdrawn rows have no useful price signal.
  const isSold = d.status === 'sold';
  const isWithdrawn = d.status === 'withdrawn';
  const labelPct = isSold && d.realized_deal_pct != null ? d.realized_deal_pct : d.deal_pct;
  let dealLabel;
  if (isWithdrawn) {
    dealLabel = `<span class="deal deal-na" style="padding:2px 6px;border-radius:3px;">borttagen</span>`;
  } else if (labelPct != null) {
    const prefix = isSold ? 'sold ' : '';
    dealLabel = `<span class="deal" style="${dealStyle(labelPct)};padding:2px 6px;border-radius:3px;">
         ${prefix}${labelPct > 0 ? '+' : ''}${labelPct.toFixed(1)}%
       </span>`;
  } else {
    dealLabel = `<span class="deal deal-na" style="padding:2px 6px;border-radius:3px;">n/a</span>`;
  }
  const priceShown = isSold ? d.sold_price_kr ?? d.asking_price_kr : d.asking_price_kr;
  const vaning = d.vaning != null
    ? `vån ${d.vaning}${d.vaning_total ? '/' + d.vaning_total : ''}${d.hiss ? ' (hiss)' : ''}` : '';
  const star = d.stadsdel_liquidity === 'high'
    ? `<span class="star" title="High-liquidity stadsdel — strong resale">★</span>` : '';
  const ptypeSvg = BOSTADSTYP_ICON[d.bostadstyp];
  const ptypeIcon = ptypeSvg
    ? `<span class="ptype" title="${d.bostadstyp}">${ptypeSvg}</span>` : '';
  if (isWithdrawn) li.style.opacity = '0.55';
  if (likedSet.has(d.href)) li.classList.add('liked');
  li.innerHTML = `
    <div class="top">${dealLabel}<span class="price">${fmtKr(priceShown)}</span></div>
    <div class="addr"><span class="liked-star" title="Sparad favorit">★</span>${ptypeIcon}${star}${d.address ?? ''}</div>
    <div class="area">${d.area ?? ''}</div>
    <div class="meta">${fmtM2(d.m2)} · ${d.rooms ?? '–'} rum · ${fmtKr(d.kr_per_m2)}/m² ${vaning ? '· ' + vaning : ''}${
      d.min_to_odenplan != null ? ` · <span style="color:${commuteColor(d.min_to_odenplan)}">${d.min_to_odenplan} min</span>` : ''
    }${
      d.published_at && !isSold && !isWithdrawn
        ? ` · <span style="color:${freshnessColor(d.published_at)}" title="Tid på marknaden">${freshnessLabel(d.published_at)}</span>`
        : ''
    }</div>
  `;
  li.addEventListener('click', () => selectIndex(i));
  listEl.appendChild(li);
  rows.push(li);
});

// Marker style by status. Active/kommande use the current deal-color scheme;
// sold pins keep the same hue but on the realized (sold-vs-predicted) axis,
// rendered as a colored ring on a pale fill so they read as "outcome" not
// "opportunity". Withdrawn pins are faded gray with a dashed ring.
function pinStyle(d) {
  let s;
  if (d.status === 'sold') {
    const r = d.realized_deal_pct;
    s = {
      radius: 8, weight: 2.5,
      color: r != null ? dealColor(r) : '#888',
      fillColor: '#e9e9e9', fillOpacity: 0.9
    };
  } else if (d.status === 'withdrawn') {
    s = {
      radius: 7, weight: 1.5,
      color: '#888', dashArray: '3,3',
      fillColor: '#bbb', fillOpacity: 0.45
    };
  } else {
    s = {
      radius: 9, weight: 2,
      color: d.status === 'kommande' ? '#222' : 'white',
      fillColor: dealColor(d.deal_pct), fillOpacity: 0.95
    };
  }
  // Tomträtt: solid black fill so these listings read as visually distinct
  // — the deal-color underneath would otherwise advertise a deal that the
  // model can't actually evaluate (ownership cost isn't priced in).
  if (d.is_tomratt) {
    s.fillColor = '#000';
    s.fillOpacity = 0.95;
  }
  // Liked overrides the border with gold so the marker pops without
  // hiding the (possibly black) fill. Seen state is rendered separately
  // as a transparent eye-emoji tooltip overlay (ensureSeenOverlay).
  if (likedSet.has(d.href)) {
    s.color = '#f59e0b';
    s.weight = Math.max(s.weight, 3);
  }
  return s;
}

// Pass 2: build markers in REVERSE order (worst deals first, best on top).
function syncLikeButton(btn, href) {
  const liked = likedSet.has(href);
  btn.classList.toggle('liked', liked);
  btn.textContent = liked ? '★' : '☆';
}

// Pin the eye emoji on the marker. Idempotent — calling twice is harmless.
// Position bottom-right by offsetting ~0.7 * radius along both axes (the
// 45° diagonal of the circle), so the eye sits at ~quarter-marker size
// in the corner regardless of which status radius we got.
function ensureSeenOverlay(marker) {
  if (marker._seenOverlay) return;
  const r = (marker.options && marker.options.radius) || 9;
  const off = Math.round(r * 0.7);
  marker.bindTooltip('👀', {
    permanent: true, direction: 'center',
    className: 'seen-marker', offset: [off, off],
  }).openTooltip();
  marker._seenOverlay = true;
}

function markSeen(href, marker) {
  if (seenSet.has(href)) return;
  seenSet.add(href);
  saveStore(STORE_SEEN, seenSet);
  if (marker) ensureSeenOverlay(marker);
}

for (let i = sorted.length - 1; i >= 0; i--) {
  const d = sorted[i];
  if (d.lat == null || d.lon == null) continue;
  const m = L.circleMarker([d.lat, d.lon], pinStyle(d)).addTo(map);
  m.bindPopup(popupHtml(d), { maxWidth: 280, autoPan: false });
  if (seenSet.has(d.href)) ensureSeenOverlay(m);
  m.on('popupopen', (e) => {
    selectIndex(i, { fromMap: true });
    // bindPopup caches HTML — re-sync the star icon and re-attach
    // handlers on every open.
    const popupEl = e.popup.getElement();
    if (!popupEl) return;
    const btn = popupEl.querySelector('.like-btn');
    if (btn) {
      syncLikeButton(btn, d.href);
      btn.onclick = (ev) => {
        ev.stopPropagation();
        if (likedSet.has(d.href)) likedSet.delete(d.href);
        else likedSet.add(d.href);
        saveStore(STORE_LIKED, likedSet);
        syncLikeButton(btn, d.href);
        m.setStyle(pinStyle(d));
        const row = rows[i];
        if (row) row.classList.toggle('liked', likedSet.has(d.href));
      };
    }
    // Mark as seen only when the user actually opens the Hemnet link —
    // skimming popups doesn't count as having reviewed the listing.
    const link = popupEl.querySelector('.popup-link');
    if (link) link.onclick = () => markSeen(d.href, m);
  });
  markers[i] = m;
}

const pinned = markers.filter(m => m).length;
const subEl  = document.getElementById('sub');

// Property-type filter groups — same 3-way split as the sidebar icons.
const TYPE_GROUPS = [
  { key: 'apt',   label: 'Lägenhet', icon: PROPERTY_ICON_SVG.building,
    matches: ['Lägenhet'] },
  { key: 'villa', label: 'Villa',    icon: PROPERTY_ICON_SVG.villa,
    matches: ['Villa'] },
  { key: 'row',   label: 'Radhus',   icon: PROPERTY_ICON_SVG.row,
    matches: ['Radhus', 'Parhus', 'Kedjehus', 'Par-/kedje-/radhus'] },
];
const groupOf = new Map();   // bostadstyp -> group key
TYPE_GROUPS.forEach(g => g.matches.forEach(t => groupOf.set(t, g.key)));

const STATUS_FILTERS = [
  { key: 'onsale',    label: 'Till salu', defaultOn: true  },
  { key: 'kommande',  label: 'Kommande',  defaultOn: true  },
  { key: 'sold',      label: 'Sålda',     defaultOn: false },
  { key: 'withdrawn', label: 'Borttagna', defaultOn: false },
];

// Region filter — inner/outer split matches score.py's INOM_TULLARNA set
// (carried on each row as `region` ∈ {inner, outer}).
const REGION_FILTERS = [
  { key: 'inner', label: 'Inom tullarna' },
  { key: 'outer', label: 'Utom tullarna' },
];

// Feature filter — Hemnet checkbox-tagged amenities from the list page
// (currently Balkong / Hiss / Uteplats; we only surface the ones that
// actually act as a search criterion). Off by default: clicking switches
// to "only show listings that have this tag."
const FEATURE_FILTERS = [
  { key: 'Uteplats', label: 'Uteplats' },
];

// Ownership filter — Bostadsrätt vs Tomträtt. Both active by default;
// untoggle Tomträtt to hide leasehold-land listings (their deal_pct
// is unreliable because the model can't see ground-rent burden).
const OWNERSHIP_FILTERS = [
  { key: 'br',       label: 'Bostadsrätt' },
  { key: 'tomtratt', label: 'Tomträtt'    },
];

// Filter selection persists in localStorage so the user doesn't have to
// re-pick after every refresh. Fall back to per-group defaults when
// nothing is saved yet. Unknown keys in the saved blob (e.g. a feature
// we removed) are harmless — they simply never get rendered as buttons.
const STORE_FILTERS = 'hemnet:filters:v1';
function loadFilters() {
  try { return JSON.parse(localStorage.getItem(STORE_FILTERS) || 'null'); }
  catch (_e) { return null; }
}
function saveFilters() {
  try {
    localStorage.setItem(STORE_FILTERS, JSON.stringify({
      groups:    [...selectedGroups],
      statuses:  [...selectedStatuses],
      regions:   [...selectedRegions],
      features:  [...selectedFeatures],
      ownership: [...selectedOwnership],
    }));
  } catch (_e) { /* quota / disabled — silently no-op */ }
}
const savedFilters = loadFilters();
const selectedGroups    = new Set(savedFilters?.groups    ?? TYPE_GROUPS.map(g => g.key));
const selectedStatuses  = new Set(savedFilters?.statuses  ?? STATUS_FILTERS.filter(s => s.defaultOn).map(s => s.key));
const selectedRegions   = new Set(savedFilters?.regions   ?? REGION_FILTERS.map(r => r.key));
const selectedFeatures  = new Set(savedFilters?.features  ?? []);
const selectedOwnership = new Set(savedFilters?.ownership ?? OWNERSHIP_FILTERS.map(o => o.key));

// Pre-compute counts.
const groupCounts = {}, statusCounts = {}, regionCounts = {}, featureCounts = {}, ownershipCounts = {};
sorted.forEach(d => {
  const g = groupOf.get(d.bostadstyp);
  if (g) groupCounts[g] = (groupCounts[g] || 0) + 1;
  const s = d.status || 'onsale';
  statusCounts[s] = (statusCounts[s] || 0) + 1;
  if (d.region) regionCounts[d.region] = (regionCounts[d.region] || 0) + 1;
  (d.tags || []).forEach(t => { featureCounts[t] = (featureCounts[t] || 0) + 1; });
  const ok = d.is_tomratt ? 'tomtratt' : 'br';
  ownershipCounts[ok] = (ownershipCounts[ok] || 0) + 1;
});

// Render two filter rows: property type (with icons) and status (text-only).
const filtersEl = document.getElementById('filters');
const typeRow      = document.createElement('div'); typeRow.className      = 'filter-group fg-type';
const statusRow    = document.createElement('div'); statusRow.className    = 'filter-group fg-status';
const regionRow    = document.createElement('div'); regionRow.className    = 'filter-group fg-region';
const featureRow   = document.createElement('div'); featureRow.className   = 'filter-group fg-feature';
const ownershipRow = document.createElement('div'); ownershipRow.className = 'filter-group fg-ownership';
filtersEl.appendChild(typeRow);
filtersEl.appendChild(statusRow);
filtersEl.appendChild(regionRow);
filtersEl.appendChild(featureRow);
filtersEl.appendChild(ownershipRow);

function makeFilterButton(label, count, selectedSet, key, iconHtml) {
  const btn = document.createElement('button');
  btn.className = selectedSet.has(key) ? 'filter-btn active' : 'filter-btn';
  btn.title = `Toggle ${label}`;
  btn.innerHTML = `${iconHtml || ''}<span>${label}</span><span class="count">${count}</span>`;
  btn.addEventListener('click', () => {
    if (selectedSet.has(key)) selectedSet.delete(key);
    else selectedSet.add(key);
    btn.classList.toggle('active');
    updateVisibility();
    saveFilters();
  });
  return btn;
}

TYPE_GROUPS.forEach(g => {
  if (!groupCounts[g.key]) return;
  // Icon-only for property type (label suppressed) — keeps the row compact.
  const btn = document.createElement('button');
  btn.className = selectedGroups.has(g.key) ? 'filter-btn active' : 'filter-btn';
  btn.title = `Toggle ${g.label}`;
  btn.innerHTML = `${g.icon}<span class="count">${groupCounts[g.key]}</span>`;
  btn.addEventListener('click', () => {
    if (selectedGroups.has(g.key)) selectedGroups.delete(g.key);
    else selectedGroups.add(g.key);
    btn.classList.toggle('active');
    updateVisibility();
    saveFilters();
  });
  typeRow.appendChild(btn);
});
STATUS_FILTERS.forEach(s => {
  if (!statusCounts[s.key]) return;
  statusRow.appendChild(makeFilterButton(s.label, statusCounts[s.key], selectedStatuses, s.key));
});
REGION_FILTERS.forEach(r => {
  if (!regionCounts[r.key]) return;
  regionRow.appendChild(makeFilterButton(r.label, regionCounts[r.key], selectedRegions, r.key));
});
FEATURE_FILTERS.forEach(f => {
  if (!featureCounts[f.key]) return;
  featureRow.appendChild(makeFilterButton(f.label, featureCounts[f.key], selectedFeatures, f.key));
});
OWNERSHIP_FILTERS.forEach(o => {
  if (!ownershipCounts[o.key]) return;
  ownershipRow.appendChild(makeFilterButton(o.label, ownershipCounts[o.key], selectedOwnership, o.key));
});

function passesTypeFilter(d) {
  const g = groupOf.get(d.bostadstyp);
  if (g) return selectedGroups.has(g);
  return selectedGroups.size === TYPE_GROUPS.length;
}
function passesStatusFilter(d) {
  return selectedStatuses.has(d.status || 'onsale');
}
function passesRegionFilter(d) {
  // Rows without a region tag (rare; older history records pre-region) pass through.
  return !d.region || selectedRegions.has(d.region);
}
function passesFeatureFilter(d) {
  // Empty selection = no feature filter applied (everything passes). Otherwise
  // the row must have every selected tag (AND, not OR — multiple amenities
  // typically combine as "I want all of these").
  if (selectedFeatures.size === 0) return true;
  const tags = d.tags || [];
  for (const f of selectedFeatures) if (!tags.includes(f)) return false;
  return true;
}
function passesOwnershipFilter(d) {
  return selectedOwnership.has(d.is_tomratt ? 'tomtratt' : 'br');
}

// Hide sidebar rows that fall outside the current map viewport or don't
// match the selected property-type filters. Map markers reflect only the
// type filter (panning shouldn't make pins disappear); sidebar reflects both.
function updateVisibility() {
  const bounds = map.getBounds();
  let visible = 0;
  for (let i = 0; i < sorted.length; i++) {
    const d = sorted[i], row = rows[i], marker = markers[i];
    const filterOk = passesTypeFilter(d) && passesStatusFilter(d) && passesRegionFilter(d)
                     && passesFeatureFilter(d) && passesOwnershipFilter(d);
    if (marker) {
      const onMap = map.hasLayer(marker);
      if (filterOk && !onMap) marker.addTo(map);
      else if (!filterOk && onMap) map.removeLayer(marker);
    }
    if (!row) continue;
    const hasCoords = d.lat != null && d.lon != null;
    const inBounds = !hasCoords || bounds.contains([d.lat, d.lon]);
    const show = inBounds && filterOk;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }
  subEl.textContent = `${visible} of ${sorted.length} in view · ${pinned} pinned · sorted by newest`;
}
map.on('moveend', () => {
  updateVisibility();
  const c = map.getCenter();
  try {
    localStorage.setItem(STORE_MAP, JSON.stringify({
      lat: +c.lat.toFixed(5), lon: +c.lng.toFixed(5), zoom: map.getZoom(),
    }));
  } catch (_e) {}
});
updateVisibility();

document.title = `Hemnet — ${sorted.length} onsale`;

// Mobile sidebar toggle. CSS hides #mobile-toggle on wider screens, so the
// listener is harmless there. Tapping flips body.show-sidebar; the sidebar
// translates in and the button label swaps map ↔ list.
const toggleBtn = document.getElementById('mobile-toggle');
toggleBtn.addEventListener('click', () => {
  const shown = document.body.classList.toggle('show-sidebar');
  toggleBtn.textContent = shown ? '✕ Map' : '≡ List';
  // After hiding the sidebar, give Leaflet a tick to notice the resize.
  if (!shown) setTimeout(() => map.invalidateSize(), 250);
});
// Also resync the map after orientation changes / window resizes.
window.addEventListener('resize', () => map.invalidateSize());
</script>
</body>
</html>
"""


def build(in_path_str: str, out_path_str: str | None = None, history_path_str: str | None = None):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else ROOT / "index.html"
    rows = [json.loads(l) for l in open(in_path)]
    pinned = [trim_row(r) for r in rows if r.get("lat") is not None]
    skipped = len(rows) - len(pinned)

    ghosts: list[dict] = []
    if history_path_str:
        live_ids = {listing_id(r["href"]) for r in rows if r.get("href")}
        ghosts = load_history_ghosts(Path(history_path_str), live_ids)
        pinned.extend(trim_row(g) for g in ghosts)

    payload = json.dumps(pinned, ensure_ascii=False, separators=(",", ":"))

    # T-bana overlay data. Run `python3 fetch_tbana.py` to regenerate.
    tbana_path = ROOT / "tbana.json"
    tbana_payload = tbana_path.read_text() if tbana_path.exists() else '{"lines":[],"stations":[]}'

    html = HTML_TEMPLATE.replace("__DATA__", payload).replace("__TBANA__", tbana_payload)
    out_path.write_text(html)
    size_kb = out_path.stat().st_size / 1024
    ghost_note = f", {len(ghosts)} ghosts from history" if ghosts else ""
    print(f"wrote {out_path}  ({len(pinned)} pinned, {skipped} skipped{ghost_note}, {size_kb:.1f} KB)")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out")
    p.add_argument("--history", help="data/history.jsonl — adds sold/withdrawn ghost pins")
    args = p.parse_args()
    build(args.input, args.out, args.history)


if __name__ == "__main__":
    main()
