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
    "hiss forening photos lat lon visning predicted_price_kr deal_pct stadsdel_liquidity "
    "bostadstyp"
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
  #filters { display: flex; gap: 6px; margin-top: 8px; }
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
  .filter-btn .count { opacity: 0.7; }
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
  li.row .ptype { color: #777; margin-right: 5px; vertical-align: -2px; cursor: help; }
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
  .deal-na   { background: #eee;    color: #777; }
</style>
</head>
<body>
<aside id="sidebar">
  <header>
    <h1 id="title">On-sale listings</h1>
    <div class="sub" id="sub"></div>
    <div id="filters"></div>
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

// T-bana station overlay — small dots with name tooltips. Rendered before
// listing markers so deal pins stay on top.
const TBANA = __TBANA__;
TBANA.stations.forEach(s => {
  L.circleMarker([s.lat, s.lon], {
    radius: 3.5, color: '#fff', weight: 1.5,
    fillColor: '#222', fillOpacity: 1,
  }).bindTooltip(s.name, { direction: 'top', offset: [0, -4] }).addTo(map);
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

function popupHtml(d) {
  const photo = (d.photos && d.photos[0])
    ? `<img class="popup-photo" src="${d.photos[0]}" loading="lazy" alt="">` : '';
  const vaning = d.vaning != null
    ? `${d.vaning}${d.vaning_total ? ' av ' + d.vaning_total : ''}${d.hiss ? ', hiss' : ''}`
    : '–';
  const deal = d.deal_pct != null
    ? `<div class="popup-deal" style="${dealStyle(d.deal_pct)}">
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
    ? `<span class="deal" style="${dealStyle(d.deal_pct)};padding:2px 6px;border-radius:3px;">
         ${d.deal_pct > 0 ? '+' : ''}${d.deal_pct.toFixed(1)}%
       </span>`
    : `<span class="deal deal-na" style="padding:2px 6px;border-radius:3px;">n/a</span>`;
  const vaning = d.vaning != null
    ? `vån ${d.vaning}${d.vaning_total ? '/' + d.vaning_total : ''}${d.hiss ? ' (hiss)' : ''}` : '';
  const star = d.stadsdel_liquidity === 'high'
    ? `<span class="star" title="High-liquidity stadsdel — strong resale">★</span>` : '';
  const ptypeSvg = BOSTADSTYP_ICON[d.bostadstyp];
  const ptypeIcon = ptypeSvg
    ? `<span class="ptype" title="${d.bostadstyp}">${ptypeSvg}</span>` : '';
  li.innerHTML = `
    <div class="top">${dealLabel}<span class="price">${fmtKr(d.asking_price_kr)}</span></div>
    <div class="addr">${ptypeIcon}${star}${d.address ?? ''}</div>
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
// All groups active by default — clicking a button toggles its group off.
const selectedGroups = new Set(TYPE_GROUPS.map(g => g.key));

// Render filter buttons with per-group counts.
const filtersEl = document.getElementById('filters');
const groupCounts = {};
sorted.forEach(d => {
  const g = groupOf.get(d.bostadstyp);
  if (g) groupCounts[g] = (groupCounts[g] || 0) + 1;
});
TYPE_GROUPS.forEach(g => {
  if (!groupCounts[g.key]) return;
  const btn = document.createElement('button');
  btn.className = 'filter-btn active';
  btn.title = `Toggle ${g.label}`;
  btn.innerHTML = `${g.icon}<span class="count">${groupCounts[g.key]}</span>`;
  btn.addEventListener('click', () => {
    if (selectedGroups.has(g.key)) selectedGroups.delete(g.key);
    else selectedGroups.add(g.key);
    btn.classList.toggle('active');
    updateVisibility();
  });
  filtersEl.appendChild(btn);
});

function passesTypeFilter(d) {
  const g = groupOf.get(d.bostadstyp);
  if (g) return selectedGroups.has(g);
  // Unknown bostadstyp — show only when no filter is actively excluding (all on).
  return selectedGroups.size === TYPE_GROUPS.length;
}

// Hide sidebar rows that fall outside the current map viewport or don't
// match the selected property-type filters. Map markers reflect only the
// type filter (panning shouldn't make pins disappear); sidebar reflects both.
function updateVisibility() {
  const bounds = map.getBounds();
  let visible = 0;
  for (let i = 0; i < sorted.length; i++) {
    const d = sorted[i], row = rows[i], marker = markers[i];
    const typeOk = passesTypeFilter(d);
    if (marker) {
      const onMap = map.hasLayer(marker);
      if (typeOk && !onMap) marker.addTo(map);
      else if (!typeOk && onMap) map.removeLayer(marker);
    }
    if (!row) continue;
    const hasCoords = d.lat != null && d.lon != null;
    const inBounds = !hasCoords || bounds.contains([d.lat, d.lon]);
    const show = inBounds && typeOk;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }
  subEl.textContent = `${visible} of ${sorted.length} in view · ${pinned} pinned · sorted by deal score`;
}
map.on('moveend', updateVisibility);
updateVisibility();

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

    # T-bana overlay data. Run `python3 fetch_tbana.py` to regenerate.
    tbana_path = ROOT / "tbana.json"
    tbana_payload = tbana_path.read_text() if tbana_path.exists() else '{"lines":[],"stations":[]}'

    html = HTML_TEMPLATE.replace("__DATA__", payload).replace("__TBANA__", tbana_payload)
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
