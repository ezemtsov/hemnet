"""Hedonic regression scorer for on-sale Hemnet listings.

Fits log(slutpris) on the sold dataset, predicts expected slutpris for each
on-sale listing, ranks by (predicted − asking) / asking — the "deal %".

Usage:
    python3 score.py \\
        --sold   data/sold-2026-05-10.enriched.jsonl \\
        --onsale data/onsale-2026-05-10.enriched.geo.jsonl
    # writes data/onsale-2026-05-10.enriched.geo.scored.jsonl

Methodology: see README "Scoring methodology".
"""
import argparse, json, math, re, random
from pathlib import Path
import numpy as np

# Features used in production. is_house is omitted: train_region_model
# filters to bostadstyp == "Lägenhet", so it would be a zero-variance column.
# experiment.py adds it back when sweeping all-property configs.
NUMERIC_FEATURES = ["log_m2", "byggar_decade", "vaning_eff", "hiss_int", "log_avgift",
                    "is_tomratt", "is_aganderatt", "is_andel"]

# Directional words that aren't stadsdelar on their own — keep them with the next word
# so "Lilla Essingen" stays distinct from a hypothetical "Lilla Anything else".
_DIRECTIONAL = {"lilla", "stora", "västra", "östra", "norra", "norr", "södra", "söder",
                "gamla", "centrala", "nedre", "övre"}

HOUSE_TYPES = {"Villa", "Radhus", "Parhus", "Kedjehus", "Par-/kedje-/radhus"}

# Coarse-stadsdel labels considered "inom tullarna" (matches Hemnet's location_id
# 898741 once collapsed through `normalize_stadsdel`). Listings here get the
# inner model; everything else gets the outer model. The split exists because
# experiments showed inner ≈ 8% MAPE vs outer ≈ 16% — they're effectively two
# different markets and a single hedonic fit compromises both.
INOM_TULLARNA = {
    # Coarse stadsdel parents
    "Södermalm", "Kungsholmen", "Vasastan", "Östermalm", "Norrmalm",
    "Gamla Stan", "Hagastaden", "Birkastan", "Gärdet", "Hjorthagen",
    # Kungsholmen sub-areas (normalize_stadsdel produces these when the listing
    # tags the sub-area rather than the parent).
    "Stadshagen", "Marieberg", "Västra Kungsholmen", "Fredhäll", "Fridhemsplan",
    "Hornsbergs", "Lindhagen", "Thorildsplan", "Norr Mälarstrand",
    # Östermalm sub-areas
    "Norra Djurgårdsstaden", "Nedre Gärdet",
    # Södermalm sub-areas
    "Sofia", "Hornstull", "Katarina", "Maria", "Skanstull", "Högalid", "Reimersholme",
}


def region_of(area: str | None) -> str:
    return "inner" if normalize_stadsdel(area) in INOM_TULLARNA else "outer"


# At Stockholm latitude (~59°N), 1° lat ≈ 111 km and 1° lon ≈ 55 km.
# Scale lon so squared-Euclidean distance approximates real distance in km.
_LAT_KM = 111.0
_LON_KM_STHLM = 55.0


def build_latlon_stadsdel_resolver(sold_rows: list[dict], k: int = 5):
    """Return a function (lat, lon) -> stadsdel-bucket-name using k-NN majority
    vote over geocoded sold rows. This fixes the train/predict label mismatch:
    Hemnet tags the same geographic place differently in sold vs onsale strings
    (e.g. sold "Spånga - Tensta" → Spånga, onsale "Tensta" → Tensta), so an
    onsale row that lat/lon-lands among Spånga-labeled sold rows gets the
    Spånga bucket — matching what the model was trained on."""
    pts: list[tuple[float, float, str]] = []
    for r in sold_rows:
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None or lon is None:
            continue
        sd = normalize_stadsdel(r.get("area"))
        if sd != "Unknown":
            pts.append((float(lat), float(lon), sd))

    def resolve(lat: float | None, lon: float | None) -> str | None:
        if lat is None or lon is None or not pts:
            return None
        from collections import Counter
        dists = [(((la - lat) * _LAT_KM) ** 2 + ((lo - lon) * _LON_KM_STHLM) ** 2, sd)
                 for la, lo, sd in pts]
        dists.sort(key=lambda t: t[0])
        return Counter(sd for _, sd in dists[:k]).most_common(1)[0][0]

    return resolve


def normalize_stadsdel(area: str | None) -> str:
    """Reduce Hemnet's messy `area` strings to a stable coarse stadsdel bucket.

    'Vasastan, Stockholms kommun'           -> 'Vasastan'
    'Vasastan/Norrmalm, Stockholms kommun'  -> 'Vasastan'
    'Östermalm - Gärdet, Stockholms kommun' -> 'Östermalm'
    'Södermalm Katarina, Stockholms kommun' -> 'Södermalm'
    'Lilla Essingen, Stockholms kommun'     -> 'Lilla Essingen'  (directional prefix kept)
    'HÄSSELBY, Stockholms kommun'           -> 'Hässelby'        (case-normalized)
    """
    if not area:
        return "Unknown"
    head = area.split(",")[0].split(".")[0].strip()
    head = re.split(r"[/\-–]", head)[0].strip()
    if not head:
        return "Unknown"
    words = head.split()
    if not words:
        return "Unknown"
    if len(words) >= 2 and words[0].lower() in _DIRECTIONAL:
        return f"{words[0].capitalize()} {words[1].capitalize()}"
    return words[0].capitalize()


def stadsdel_fine(area: str | None) -> str:
    """Fine-grain encoder: full area string (before comma), title-cased.

    Use with `fold_rare_stadsdelar` to collapse low-count buckets back to the
    coarse `normalize_stadsdel` parent. Intended for the granularity experiment.
    """
    if not area:
        return "Unknown"
    head = area.split(",")[0].split(".")[0].strip()
    return " ".join(w.capitalize() for w in head.split()) if head else "Unknown"


def featurize(row: dict, stadsdel_medians: dict | None = None,
              stadsdel_fn=normalize_stadsdel,
              resolved_stadsdel: str | None = None) -> dict | None:
    """Return a dict of features for one row, or None if essentials missing.

    `stadsdel_medians` (when provided) supplies fallback values for missing
    numeric features so on-sale listings with gaps still score.
    `stadsdel_fn` is the encoder used to bucket `area` — defaults to coarse.
    `resolved_stadsdel` (optional) overrides the area-based bucket — callers
    use this at predict time to inject a lat/lon-based k-NN result so the
    bucket matches sold-side labels even when Hemnet's area string differs.
    """
    m2 = row.get("boarea_m2") or row.get("m2")
    if not m2 or m2 <= 0:
        return None
    stadsdel = resolved_stadsdel if resolved_stadsdel else stadsdel_fn(row.get("area"))

    bostadstyp = row.get("bostadstyp") or ""
    upplat = row.get("upplatelseform") or ""
    is_house = 1 if bostadstyp in HOUSE_TYPES else 0
    is_tomratt = 1 if upplat == "Tomträtt" else 0
    is_aganderatt = 1 if upplat == "Äganderätt" else 0
    is_andel = 1 if upplat == "Andel i bostadsförening" else 0

    byggar = row.get("byggar")
    vaning = row.get("vaning")
    avgift = row.get("avgift_kr_mon") or row.get("fee_kr")
    hiss = row.get("hiss")

    # Houses on Äganderätt genuinely have no monthly fee — don't impute one.
    avgift_truly_none = (avgift is None and is_aganderatt)

    if stadsdel_medians:
        s = stadsdel_medians.get(stadsdel) or stadsdel_medians.get("__global__") or {}
        byggar = byggar if byggar is not None else s.get("byggar")
        vaning = vaning if vaning is not None else s.get("vaning")
        hiss = hiss if hiss is not None else s.get("hiss", False)
        if avgift is None and not avgift_truly_none:
            avgift = s.get("avgift")

    return {
        "stadsdel": stadsdel,
        "log_m2": math.log(float(m2)),
        "byggar_decade": (int(byggar) // 10 * 10) if byggar else None,
        "vaning_eff": int(vaning) if vaning is not None else None,
        "hiss_int": 1 if hiss else 0,
        "log_avgift": math.log(float(avgift) + 1) if avgift else (0.0 if avgift_truly_none else None),
        "is_house": is_house,
        "is_tomratt": is_tomratt,
        "is_aganderatt": is_aganderatt,
        "is_andel": is_andel,
    }


def fold_rare_stadsdelar(rows_with_features: list[dict], min_count: int = 4) -> set[str]:
    """Return the set of stadsdel labels with at least `min_count` sold rows.
    Anything below that threshold becomes 'Other' (too sparse to fit reliably).

    Threshold note: 4 is a compromise. Higher thresholds (8+) push small
    suburb buckets into 'Other', which is then biased high because it
    averages over heterogeneous folds; lower thresholds keep more buckets
    but their coefficients are noisier. For this dataset 4 catches Vårberg/
    Sätra/Fagersjö (5-6 rows each) at the cost of slightly higher variance.
    """
    counts: dict[str, int] = {}
    for r in rows_with_features:
        counts[r["stadsdel"]] = counts.get(r["stadsdel"], 0) + 1
    return {s for s, c in counts.items() if c >= min_count}


def fold_fine_buckets(feats: list[dict], areas: list[str | None], min_count: int = 8) -> None:
    """In-place: fold fine-grain buckets with <min_count rows to their coarse
    parent (via `normalize_stadsdel`). Used when training with `stadsdel_fine`
    so very sparse sub-areas don't dominate the design matrix."""
    counts: dict[str, int] = {}
    for f in feats:
        counts[f["stadsdel"]] = counts.get(f["stadsdel"], 0) + 1
    rare = {s for s, c in counts.items() if c < min_count}
    for i, f in enumerate(feats):
        if f["stadsdel"] in rare:
            f["stadsdel"] = normalize_stadsdel(areas[i])


def compute_liquidity_tiers(sold_rows: list[dict]) -> dict[str, str]:
    """Tag each stadsdel as high / medium / low resale liquidity from sold-data signals.

    Definition matches the user's preference (saved in memory):
    - high: n ≥ 20 AND (% sold over asking ≥ 65% OR n ≥ 60)  → ⭐ in sidebar
    - medium: n ≥ 10 AND % over asking ≥ 50%
    - low: everything else (or insufficient sample)
    """
    by_s: dict[str, list[int]] = {}
    for r in sold_rows:
        diff = r.get("price_diff_pct")
        if diff is None:
            continue
        s = normalize_stadsdel(r.get("area"))
        by_s.setdefault(s, []).append(diff)
    tiers: dict[str, str] = {}
    for s, diffs in by_s.items():
        n = len(diffs)
        pct_over = sum(1 for d in diffs if d > 0) / n * 100
        if n >= 20 and (pct_over >= 65 or n >= 60):
            tiers[s] = "high"
        elif n >= 10 and pct_over >= 50:
            tiers[s] = "medium"
        else:
            tiers[s] = "low"
    return tiers


def compute_medians(rows: list[dict]) -> dict:
    """Per-stadsdel medians for byggar/vaning/avgift/hiss + a global fallback."""
    by_stadsdel: dict = {}
    for r in rows:
        s = r["stadsdel"]
        by_stadsdel.setdefault(s, {"byggar": [], "vaning": [], "avgift": [], "hiss": []})
        if r["byggar_decade"] is not None: by_stadsdel[s]["byggar"].append(r["byggar_decade"])
        if r["vaning_eff"] is not None:    by_stadsdel[s]["vaning"].append(r["vaning_eff"])
        if r["log_avgift"] is not None:    by_stadsdel[s]["avgift"].append(math.exp(r["log_avgift"]) - 1)
        by_stadsdel[s]["hiss"].append(r["hiss_int"])

    out: dict = {}
    for s, d in by_stadsdel.items():
        out[s] = {
            "byggar": int(np.median(d["byggar"])) if d["byggar"] else None,
            "vaning": int(np.median(d["vaning"])) if d["vaning"] else None,
            "avgift": float(np.median(d["avgift"])) if d["avgift"] else None,
            "hiss":   bool(np.mean(d["hiss"]) > 0.5),
        }
    flat = {"byggar": [], "vaning": [], "avgift": [], "hiss": []}
    for d in by_stadsdel.values():
        for k in flat: flat[k].extend(d[k])
    out["__global__"] = {
        "byggar": int(np.median(flat["byggar"])) if flat["byggar"] else None,
        "vaning": int(np.median(flat["vaning"])) if flat["vaning"] else None,
        "avgift": float(np.median(flat["avgift"])) if flat["avgift"] else None,
        "hiss":   bool(np.mean(flat["hiss"]) > 0.5),
    }
    return out


def build_design_matrix(feature_rows: list[dict], stadsdel_levels: list[str], medians: dict,
                        numeric_features: list[str] = NUMERIC_FEATURES):
    """Construct X, a numpy float matrix.

    Columns: [intercept, *numeric_features, *stadsdel_dummies (drop reference)].
    Reference stadsdel = first level (alphabetical) → its coefficient is folded
    into the intercept. Missing numeric values are imputed from medians.
    """
    n = len(feature_rows)
    n_num = len(numeric_features)
    n_dum = max(0, len(stadsdel_levels) - 1)
    X = np.zeros((n, 1 + n_num + n_dum))
    X[:, 0] = 1.0  # intercept
    fallback = medians.get("__global__") or {}
    fb_byggar = fallback.get("byggar") or 1970
    fb_vaning = fallback.get("vaning") or 2
    fb_avgift = fallback.get("avgift") or 4500
    for i, r in enumerate(feature_rows):
        s = medians.get(r["stadsdel"]) or fallback
        bd = r["byggar_decade"] if r["byggar_decade"] is not None else (s.get("byggar") or fb_byggar)
        ve = r["vaning_eff"]    if r["vaning_eff"]    is not None else (s.get("vaning") or fb_vaning)
        # Use the row's log_avgift as-is when present (including the legitimate 0.0
        # for fee-less houses); only fall back to median when truly missing.
        la = r["log_avgift"] if r["log_avgift"] is not None else math.log((s.get("avgift") or fb_avgift) + 1)
        feat_vals = {
            "log_m2": r["log_m2"],
            "byggar_decade": bd,
            "vaning_eff": ve,
            "hiss_int": r["hiss_int"],
            "log_avgift": la,
            "is_house": r.get("is_house", 0),
            "is_tomratt": r.get("is_tomratt", 0),
            "is_aganderatt": r.get("is_aganderatt", 0),
            "is_andel": r.get("is_andel", 0),
        }
        for k, name in enumerate(numeric_features, start=1):
            X[i, k] = feat_vals.get(name, 0.0)
        # one-hot stadsdel (drop reference = stadsdel_levels[0])
        for j, level in enumerate(stadsdel_levels[1:], start=1 + n_num):
            if r["stadsdel"] == level:
                X[i, j] = 1.0
                break
    return X


def fit_and_predict(sold_features: list[dict], sold_log_y: np.ndarray,
                    target_features: list[dict], stadsdel_levels: list[str], medians: dict,
                    numeric_features: list[str] = NUMERIC_FEATURES,
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Solve OLS on sold, return (predicted_log_target, beta_coefficients)."""
    X_train = build_design_matrix(sold_features, stadsdel_levels, medians, numeric_features)
    X_pred  = build_design_matrix(target_features, stadsdel_levels, medians, numeric_features)
    beta, *_ = np.linalg.lstsq(X_train, sold_log_y, rcond=None)
    return X_pred @ beta, beta


def evaluate_holdout(sold_features: list[dict], log_y: np.ndarray,
                     stadsdel_levels: list[str], medians: dict, *, seed: int = 0,
                     numeric_features: list[str] = NUMERIC_FEATURES) -> dict:
    """80/20 holdout MAPE in linear price space."""
    n = len(sold_features)
    rng = random.Random(seed)
    idx = list(range(n)); rng.shuffle(idx)
    cut = int(n * 0.8)
    train_idx, test_idx = idx[:cut], idx[cut:]
    train_feats = [sold_features[i] for i in train_idx]
    test_feats  = [sold_features[i] for i in test_idx]
    pred_log, _ = fit_and_predict(train_feats, log_y[train_idx], test_feats,
                                  stadsdel_levels, medians, numeric_features)
    pred_price = np.exp(pred_log)
    actual_price = np.exp(log_y[test_idx])
    pct_err = np.abs(pred_price - actual_price) / actual_price
    return {
        "n_train": cut, "n_test": n - cut,
        "mape_pct": float(np.mean(pct_err) * 100),
        "median_ape_pct": float(np.median(pct_err) * 100),
        "p90_ape_pct": float(np.quantile(pct_err, 0.9) * 100),
    }


def train_region_model(sold_rows: list[dict], *, stadsdel_fn, fold_fine_to_coarse: bool,
                       bostadstyp_filter: frozenset[str] = frozenset({"Lägenhet"})):
    """Train one model on a subset of sold rows (filtered by bostadstyp).

    Returns a bundle with the fitted beta plus everything needed at predict
    time (encoder, medians, kept levels)."""
    pairs = []
    for r in sold_rows:
        if r.get("price_kr") is None or r["price_kr"] <= 0:
            continue
        if r.get("bostadstyp") not in bostadstyp_filter:
            continue
        f = featurize(r, stadsdel_medians=None, stadsdel_fn=stadsdel_fn)
        if f is None:
            continue
        pairs.append((r, f))

    feats = [f for _, f in pairs]
    areas = [r.get("area") for r, _ in pairs]

    if fold_fine_to_coarse:
        fold_fine_buckets(feats, areas)

    keep = fold_rare_stadsdelar(feats)
    for f in feats:
        if f["stadsdel"] not in keep:
            f["stadsdel"] = "Other"

    levels = sorted({f["stadsdel"] for f in feats})
    medians = compute_medians(feats)
    log_y = np.array([math.log(r["price_kr"]) for r, _ in pairs])

    metrics = evaluate_holdout(feats, log_y, levels, medians)

    X = build_design_matrix(feats, levels, medians, NUMERIC_FEATURES)
    beta, *_ = np.linalg.lstsq(X, log_y, rcond=None)

    return {
        "stadsdel_fn": stadsdel_fn,
        "fold_fine_to_coarse": fold_fine_to_coarse,
        "levels": levels,
        "keep": keep,
        "medians": medians,
        "beta": beta,
        "metrics": metrics,
        "n_train": len(feats),
    }


def predict_one(model: dict, row: dict, resolved_stadsdel: str | None = None) -> float | None:
    """Predict the slutpris for a single onsale row via the given region model."""
    f = featurize(row, stadsdel_medians=model["medians"],
                  stadsdel_fn=model["stadsdel_fn"],
                  resolved_stadsdel=resolved_stadsdel)
    if f is None:
        return None
    # Apply same bucket-folding the trainer used: rare → coarse parent → Other.
    if f["stadsdel"] not in model["keep"]:
        if model["fold_fine_to_coarse"]:
            coarse = normalize_stadsdel(row.get("area"))
            f["stadsdel"] = coarse if coarse in model["keep"] else "Other"
        else:
            f["stadsdel"] = "Other"
    X = build_design_matrix([f], model["levels"], model["medians"], NUMERIC_FEATURES)
    pred_log = float((X @ model["beta"])[0])
    return math.exp(pred_log)


# Property-category buckets used for routing onsale predictions to the
# right model. Apartments keep the inner/outer regional sub-split; villas
# and the row-house family each get one flat model since they're
# essentially outer-only in Stockholm kommun.
APT_TYPES = frozenset({"Lägenhet"})
VILLA_TYPES = frozenset({"Villa"})
ROW_TYPES = frozenset({"Radhus", "Parhus", "Kedjehus", "Par-/kedje-/radhus"})


def model_key_for(row: dict, region: str) -> str | None:
    bt = row.get("bostadstyp")
    if bt in APT_TYPES:
        return f"apt_{region}"
    if bt in VILLA_TYPES:
        return "villa"
    if bt in ROW_TYPES:
        return "row"
    return None


def run(sold_path: str, onsale_path: str, out_path: str | None = None):
    sold_rows = [json.loads(l) for l in open(sold_path)]
    onsale_rows = [json.loads(l) for l in open(onsale_path)]

    # --- train one model per (category, region) bucket --------------------
    # Apartments are split by region (inner/outer); villas and the row-house
    # family each get one flat model — they're essentially outer-only in
    # Stockholm and the n is too small to sub-split further.
    inner_sold = [r for r in sold_rows if region_of(r.get("area")) == "inner"]
    outer_sold = [r for r in sold_rows if region_of(r.get("area")) == "outer"]
    models: dict[str, dict] = {
        "apt_inner": train_region_model(inner_sold, stadsdel_fn=normalize_stadsdel,
                                        fold_fine_to_coarse=False,
                                        bostadstyp_filter=APT_TYPES),
        "apt_outer": train_region_model(outer_sold, stadsdel_fn=stadsdel_fine,
                                        fold_fine_to_coarse=True,
                                        bostadstyp_filter=APT_TYPES),
        "villa":     train_region_model(sold_rows, stadsdel_fn=stadsdel_fine,
                                        fold_fine_to_coarse=True,
                                        bostadstyp_filter=VILLA_TYPES),
        "row":       train_region_model(sold_rows, stadsdel_fn=stadsdel_fine,
                                        fold_fine_to_coarse=True,
                                        bostadstyp_filter=ROW_TYPES),
    }

    for label, m in models.items():
        mt = m["metrics"]
        print(f"{label:10s}  MAPE {mt['mape_pct']:5.1f}%   "
              f"median {mt['median_ape_pct']:5.1f}%   p90 {mt['p90_ape_pct']:5.1f}%   "
              f"(n_train={mt['n_train']:4d} n_test={mt['n_test']:3d}, levels={len(m['levels']):2d})")

    # --- liquidity tiers from sold (used downstream by build_map.py) ------
    liquidity = compute_liquidity_tiers(sold_rows)

    # --- lat/lon k-NN resolver: fixes Hemnet's train/predict label mismatch
    # (e.g. sold "Spånga - Tensta" → Spånga vs onsale "Tensta" → Tensta).
    resolver = build_latlon_stadsdel_resolver(sold_rows, k=5)

    # --- score onsale by routing to the right (category, region) model ----
    scored = []
    for row in onsale_rows:
        resolved_sd = resolver(row.get("lat"), row.get("lon"))
        if resolved_sd is not None:
            region = "inner" if resolved_sd in INOM_TULLARNA else "outer"
        else:
            region = region_of(row.get("area"))
        key = model_key_for(row, region)
        if key is None:
            continue  # unknown bostadstyp — skip rather than mispredict
        model = models[key]
        pred = predict_one(model, row, resolved_stadsdel=resolved_sd)
        if pred is None:
            continue
        asking = row.get("asking_price_kr")
        deal_pct = (pred - asking) / asking * 100 if asking else None
        row["predicted_price_kr"] = int(round(pred))
        row["deal_pct"] = round(deal_pct, 1) if deal_pct is not None else None
        row["region"] = region
        row["model_key"] = key
        row["model_mape_pct"] = round(model["metrics"]["mape_pct"], 1)
        row["stadsdel_resolved"] = resolved_sd
        own_s = normalize_stadsdel(row.get("area"))
        row["stadsdel_liquidity"] = liquidity.get(own_s, "low")
        scored.append(row)

    out_path_p = Path(out_path) if out_path else Path(onsale_path).with_suffix(".scored.jsonl")
    with open(out_path_p, "w") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(scored)} -> {out_path_p}")

    # --- top-K report -----------------------------------------------------
    rated = [r for r in scored if r.get("deal_pct") is not None]
    rated.sort(key=lambda r: r["deal_pct"], reverse=True)
    print("\nTop 10 candidate deals (highest predicted-vs-asking gap):")
    for r in rated[:10]:
        tag = "★" if r["region"] == "inner" else " "
        print(f"  {tag} {r['deal_pct']:+5.1f}%  ask {r['asking_price_kr']:>9,}  pred {r['predicted_price_kr']:>9,}"
              f"  {r['m2']:>4} m²  {r['address']}, {r['area']}")
    print("\nBottom 5 (priced above model — likely premium / well-renovated / something the model misses):")
    for r in rated[-5:]:
        tag = "★" if r["region"] == "inner" else " "
        print(f"  {tag} {r['deal_pct']:+5.1f}%  ask {r['asking_price_kr']:>9,}  pred {r['predicted_price_kr']:>9,}"
              f"  {r['m2']:>4} m²  {r['address']}, {r['area']}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sold", required=True, help="sold listings JSONL (training set)")
    p.add_argument("--onsale", required=True, help="on-sale listings JSONL (prediction set)")
    p.add_argument("--out", help="output scored JSONL (default: <onsale>.scored.jsonl)")
    args = p.parse_args()
    run(args.sold, args.onsale, args.out)


if __name__ == "__main__":
    main()
