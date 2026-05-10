"""Render a static index.html showing on-sale listings as Leaflet pins.

Usage:
    python3 build_map.py data/onsale-2026-05-10.enriched.geo.jsonl
    open index.html

Embeds the listing data inline so the file works with file:// (no server needed).
Re-run after each on-sale refresh to regenerate.
"""
import argparse, json
from pathlib import Path

ROOT = Path(__file__).parent

POPUP_FIELDS = (
    "href address area asking_price_kr m2 rooms kr_per_m2 byggar vaning vaning_total "
    "hiss forening photos lat lon visning predicted_price_kr deal_pct stadsdel_liquidity"
).split()


def trim_row(r: dict) -> dict:
    out = {k: r.get(k) for k in POPUP_FIELDS if r.get(k) is not None}
    # Keep only the first 5 photos to keep the embedded payload reasonable.
    if "photos" in out:
        out["photos"] = out["photos"][:5]
    return out


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
  #sidebar header {
    position: sticky; top: 0; background: white; padding: 10px 14px;
    border-bottom: 1px solid #eee; z-index: 5;
  }
  #sidebar header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  #sidebar header .sub { font-size: 11px; color: #888; margin-top: 2px; }
  #sidebar header .legend { font-size: 11px; color: #555; margin-top: 8px; line-height: 1.6; }
  .leg-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                vertical-align: middle; margin-right: 4px;
                border: 1.5px solid white; box-shadow: 0 0 0 1px rgba(0,0,0,0.25); }
  ul#list { list-style: none; padding: 0; margin: 0; }
  li.row { padding: 10px 14px; border-bottom: 1px solid #eee; cursor: pointer; transition: background .12s; }
  li.row:hover { background: #f0f4fa; }
  li.row.active { background: #e3edff; }
  li.row .top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  li.row .deal { font-weight: 700; font-size: 13px; flex-shrink: 0; }
  li.row .price { font-size: 13px; color: #666; }
  li.row .addr { font-weight: 600; font-size: 13px; margin-top: 2px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  li.row .star { color: #e0a012; margin-right: 4px; cursor: help; }
  li.row .area { font-size: 11px; color: #888; }
  li.row .meta { font-size: 11px; color: #555; margin-top: 3px; }
  .popup-photo { width: 240px; height: 160px; object-fit: cover; border-radius: 4px; display: block; }
  .popup-title { font-weight: 600; margin: 6px 0 2px; }
  .popup-area { color: #666; font-size: 12px; margin-bottom: 6px; }
  .popup-price { font-size: 16px; font-weight: 600; color: #d34; }
  .popup-meta { font-size: 12px; color: #555; margin: 4px 0; }
  .popup-meta b { color: #222; }
  .popup-link { display: inline-block; margin-top: 6px; font-size: 12px; }
  .popup-deal { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; margin-top: 4px; }
  .deal-good { background: #d1f5d3; color: #1e6e26; }
  .deal-mid  { background: #fff5cc; color: #8a6d00; }
  .deal-bad  { background: #ffd6d6; color: #a02020; }
  .deal-na   { background: #eee;    color: #777; }
</style>
</head>
<body>
<aside id="sidebar">
  <header>
    <h1 id="title">On-sale listings</h1>
    <div class="sub" id="sub"></div>
    <div class="legend">
      <div><span class="leg-swatch" style="background:#2da028"></span>≥+10% under model · likely deal</div>
      <div><span class="leg-swatch" style="background:#e0b020"></span>within ±10% · noise band</div>
      <div><span class="leg-swatch" style="background:#d33030"></span>≥10% over model · priced high</div>
      <div><span class="leg-swatch" style="background:#999"></span>no prediction</div>
      <div style="margin-top:4px;"><span style="color:#e0a012">★</span> high-liquidity stadsdel (strong resale)</div>
    </div>
  </header>
  <ul id="list"></ul>
</aside>
<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const LISTINGS = __DATA__;

const map = L.map('map', { zoomSnap: 0.5 }).setView([59.330, 18.067], 13);
// Esri ArcGIS — one of the few tile providers that serve file:// origins
// without a Referer. If you see a blank map, run `python3 -m http.server`
// in the project root and open http://localhost:8000/ instead.
L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: '&copy; Esri, OpenStreetMap contributors' }
).addTo(map);

const fmtKr = n => n == null ? '–' : n.toLocaleString('sv-SE') + ' kr';
const fmtM2 = n => n == null ? '–' : Math.round(n) + ' m²';

function dealColor(pct) {
  if (pct == null) return '#999';
  if (pct >=  10) return '#2da028';   // green: ≥10% below model = good deal
  if (pct <= -10) return '#d33030';   // red:   ≥10% above model = priced high
  return '#e0b020';                    // yellow: within ±10%
}
function dealClass(pct) {
  if (pct == null) return '';
  if (pct >=  10) return 'deal-good';
  if (pct <= -10) return 'deal-bad';
  return 'deal-mid';
}

function popupHtml(d) {
  const photo = (d.photos && d.photos[0])
    ? `<img class="popup-photo" src="${d.photos[0]}" loading="lazy" alt="">` : '';
  const vaning = d.vaning != null
    ? `${d.vaning}${d.vaning_total ? ' av ' + d.vaning_total : ''}${d.hiss ? ', hiss' : ''}`
    : '–';
  const deal = d.deal_pct != null
    ? `<div class="popup-deal ${dealClass(d.deal_pct)}">
         ${d.deal_pct > 0 ? '+' : ''}${d.deal_pct.toFixed(1)}% vs predicted ${fmtKr(d.predicted_price_kr)}
       </div>` : '';
  return `
    ${photo}
    <div class="popup-title">${d.address ?? ''}</div>
    <div class="popup-area">${d.area ?? ''}</div>
    <div class="popup-price">${fmtKr(d.asking_price_kr)}</div>
    ${deal}
    <div class="popup-meta">
      <b>${fmtM2(d.m2)}</b> · ${d.rooms ?? '–'} rum · ${fmtKr(d.kr_per_m2)}/m²<br>
      Byggår: <b>${d.byggar ?? '–'}</b> · Våning: <b>${vaning}</b><br>
      ${d.forening ? `BRF: ${d.forening}<br>` : ''}
      ${d.visning ? `Visning: ${d.visning}` : ''}
    </div>
    <a class="popup-link" href="${d.href}" target="_blank" rel="noopener">Visa på Hemnet →</a>
  `;
}

// --- sort by deal_pct (descending; nulls last) -----------------------------
const sorted = [...LISTINGS].sort((a, b) => {
  if (a.deal_pct == null && b.deal_pct == null) return 0;
  if (a.deal_pct == null) return 1;
  if (b.deal_pct == null) return -1;
  return b.deal_pct - a.deal_pct;
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
    if (!fromMap) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    else          row.scrollIntoView({ block: 'nearest' });
  }
  if (marker) {
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 14), { animate: true });
    marker.openPopup();
  }
}

// Pass 1: build sidebar rows in attractiveness order.
sorted.forEach((d, i) => {
  const li = document.createElement('li');
  li.className = 'row';
  const dealLabel = d.deal_pct != null
    ? `<span class="deal ${dealClass(d.deal_pct)}" style="padding:2px 6px;border-radius:3px;">
         ${d.deal_pct > 0 ? '+' : ''}${d.deal_pct.toFixed(1)}%
       </span>`
    : `<span class="deal deal-na" style="padding:2px 6px;border-radius:3px;">n/a</span>`;
  const vaning = d.vaning != null
    ? `vån ${d.vaning}${d.vaning_total ? '/' + d.vaning_total : ''}${d.hiss ? ' (hiss)' : ''}` : '';
  const star = d.stadsdel_liquidity === 'high'
    ? `<span class="star" title="High-liquidity stadsdel — strong resale">★</span>` : '';
  li.innerHTML = `
    <div class="top">${dealLabel}<span class="price">${fmtKr(d.asking_price_kr)}</span></div>
    <div class="addr">${star}${d.address ?? ''}</div>
    <div class="area">${d.area ?? ''}</div>
    <div class="meta">${fmtM2(d.m2)} · ${d.rooms ?? '–'} rum · ${fmtKr(d.kr_per_m2)}/m² ${vaning ? '· ' + vaning : ''}</div>
  `;
  li.addEventListener('click', () => selectIndex(i));
  listEl.appendChild(li);
  rows.push(li);
});

// Pass 2: build markers in REVERSE order (worst deals first, best on top).
for (let i = sorted.length - 1; i >= 0; i--) {
  const d = sorted[i];
  if (d.lat == null || d.lon == null) continue;
  const m = L.circleMarker([d.lat, d.lon], {
    radius: 9, weight: 2, color: 'white',
    fillColor: dealColor(d.deal_pct), fillOpacity: 0.95
  }).addTo(map);
  m.bindPopup(popupHtml(d), { maxWidth: 280 });
  m.on('popupopen', () => selectIndex(i, { fromMap: true }));
  markers[i] = m;
}

const pinned = markers.filter(m => m).length;
document.getElementById('sub').textContent =
  `${sorted.length} listings · ${pinned} pinned · sorted by deal score`;
document.title = `Hemnet — ${sorted.length} onsale`;
</script>
</body>
</html>
"""


def build(in_path_str: str, out_path_str: str | None = None):
    in_path = Path(in_path_str)
    out_path = Path(out_path_str) if out_path_str else ROOT / "index.html"
    rows = [json.loads(l) for l in open(in_path)]
    pinned = [trim_row(r) for r in rows if r.get("lat") is not None]
    skipped = len(rows) - len(pinned)
    payload = json.dumps(pinned, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__DATA__", payload)
    out_path.write_text(html)
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path}  ({len(pinned)} pinned, {skipped} skipped, {size_kb:.1f} KB)")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out")
    args = p.parse_args()
    build(args.input, args.out)


if __name__ == "__main__":
    main()
