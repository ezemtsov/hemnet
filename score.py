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

# Features used in the regression. Names match the keys we read from each row.
NUMERIC_FEATURES = ["log_m2", "byggar_decade", "vaning_eff", "hiss_int", "log_avgift"]


def normalize_stadsdel(area: str | None) -> str:
    """Reduce Hemnet's messy `area` strings to a stable stadsdel bucket.

    'Vasastan, Stockholms kommun'           -> 'Vasastan'
    'Vasastan/Norrmalm, Stockholms kommun'  -> 'Vasastan'
    'Östermalm - Gärdet, Stockholms kommun' -> 'Östermalm'
    'Södermalm Katarina, Stockholms kommun' -> 'Södermalm'
    """
    if not area:
        return "Unknown"
    head = area.split(",")[0].strip()
    head = re.split(r"[/\-–]", head)[0].strip()
    head = head.split()[0] if head else "Unknown"
    return head or "Unknown"


def featurize(row: dict, stadsdel_medians: dict | None = None) -> dict | None:
    """Return a dict of features for one row, or None if essentials missing.

    `stadsdel_medians` (when provided) supplies fallback values for missing
    numeric features so on-sale listings with gaps still score.
    """
    m2 = row.get("boarea_m2") or row.get("m2")
    if not m2 or m2 <= 0:
        return None
    stadsdel = normalize_stadsdel(row.get("area"))

    byggar = row.get("byggar")
    vaning = row.get("vaning")
    avgift = row.get("avgift_kr_mon") or row.get("fee_kr")
    hiss = row.get("hiss")

    if stadsdel_medians:
        s = stadsdel_medians.get(stadsdel) or stadsdel_medians.get("__global__") or {}
        byggar = byggar if byggar is not None else s.get("byggar")
        vaning = vaning if vaning is not None else s.get("vaning")
        avgift = avgift if avgift is not None else s.get("avgift")
        hiss = hiss if hiss is not None else s.get("hiss", False)

    return {
        "stadsdel": stadsdel,
        "log_m2": math.log(float(m2)),
        "byggar_decade": (int(byggar) // 10 * 10) if byggar else None,
        "vaning_eff": int(vaning) if vaning is not None else None,
        "hiss_int": 1 if hiss else 0,
        "log_avgift": math.log(float(avgift) + 1) if avgift else None,
    }


def fold_rare_stadsdelar(rows_with_features: list[dict], min_count: int = 8) -> set[str]:
    """Return the set of stadsdel labels with at least `min_count` sold rows.
    Anything below that threshold becomes 'Other' (too sparse to fit reliably).
    """
    counts: dict[str, int] = {}
    for r in rows_with_features:
        counts[r["stadsdel"]] = counts.get(r["stadsdel"], 0) + 1
    return {s for s, c in counts.items() if c >= min_count}


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
    # global fallback across all rows
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


def build_design_matrix(feature_rows: list[dict], stadsdel_levels: list[str], medians: dict):
    """Construct (X, valid_mask) where X is a numpy float matrix.

    Columns: [intercept, *NUMERIC_FEATURES, *stadsdel_dummies (drop reference)].
    Reference stadsdel = first level (alphabetical) → its coefficient is folded
    into the intercept. Missing numeric values are imputed from medians.
    """
    n = len(feature_rows)
    n_num = len(NUMERIC_FEATURES)
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
        avg = math.exp(r["log_avgift"]) - 1 if r["log_avgift"] is not None else (s.get("avgift") or fb_avgift)
        X[i, 1] = r["log_m2"]
        X[i, 2] = bd
        X[i, 3] = ve
        X[i, 4] = r["hiss_int"]
        X[i, 5] = math.log(avg + 1)
        # one-hot stadsdel (drop reference = stadsdel_levels[0])
        for j, level in enumerate(stadsdel_levels[1:], start=1 + n_num):
            if r["stadsdel"] == level:
                X[i, j] = 1.0
                break
    return X


def fit_and_predict(sold_features: list[dict], sold_log_y: np.ndarray,
                    target_features: list[dict], stadsdel_levels: list[str], medians: dict
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Solve OLS on sold, return (predicted_log_target, beta_coefficients)."""
    X_train = build_design_matrix(sold_features, stadsdel_levels, medians)
    X_pred  = build_design_matrix(target_features, stadsdel_levels, medians)
    beta, *_ = np.linalg.lstsq(X_train, sold_log_y, rcond=None)
    return X_pred @ beta, beta


def evaluate_holdout(sold_features: list[dict], log_y: np.ndarray,
                     stadsdel_levels: list[str], medians: dict, *, seed: int = 0) -> dict:
    """80/20 holdout MAPE in linear price space."""
    n = len(sold_features)
    rng = random.Random(seed)
    idx = list(range(n)); rng.shuffle(idx)
    cut = int(n * 0.8)
    train_idx, test_idx = idx[:cut], idx[cut:]
    train_feats = [sold_features[i] for i in train_idx]
    test_feats  = [sold_features[i] for i in test_idx]
    pred_log, _ = fit_and_predict(train_feats, log_y[train_idx], test_feats, stadsdel_levels, medians)
    pred_price = np.exp(pred_log)
    actual_price = np.exp(log_y[test_idx])
    pct_err = np.abs(pred_price - actual_price) / actual_price
    return {
        "n_train": cut, "n_test": n - cut,
        "mape_pct": float(np.mean(pct_err) * 100),
        "median_ape_pct": float(np.median(pct_err) * 100),
        "p90_ape_pct": float(np.quantile(pct_err, 0.9) * 100),
    }


def run(sold_path: str, onsale_path: str, out_path: str | None = None):
    sold_rows = [json.loads(l) for l in open(sold_path)]
    onsale_rows = [json.loads(l) for l in open(onsale_path)]

    # --- featurize sold (training set) -------------------------------------
    sold_feat_pairs = []  # (row, features) pairs
    for r in sold_rows:
        if r.get("price_kr") is None or r["price_kr"] <= 0:
            continue
        f = featurize(r, stadsdel_medians=None)
        if f is None:
            continue
        sold_feat_pairs.append((r, f))

    # Per-stadsdel medians for imputation (fit on sold only)
    medians = compute_medians([f for _, f in sold_feat_pairs])

    # Fold rare stadsdelar
    keep = fold_rare_stadsdelar([f for _, f in sold_feat_pairs])
    for _, f in sold_feat_pairs:
        if f["stadsdel"] not in keep:
            f["stadsdel"] = "Other"
    stadsdel_levels = sorted({f["stadsdel"] for _, f in sold_feat_pairs})

    sold_features = [f for _, f in sold_feat_pairs]
    log_y = np.array([math.log(r["price_kr"]) for r, _ in sold_feat_pairs])

    # --- holdout evaluation -----------------------------------------------
    metrics = evaluate_holdout(sold_features, log_y, stadsdel_levels, medians)
    print(f"Holdout MAPE: {metrics['mape_pct']:.1f}%   median APE: {metrics['median_ape_pct']:.1f}%   p90 APE: {metrics['p90_ape_pct']:.1f}%   "
          f"(n_train={metrics['n_train']} n_test={metrics['n_test']})")

    # --- featurize on-sale (prediction set) -------------------------------
    onsale_feat_pairs = []
    for r in onsale_rows:
        f = featurize(r, stadsdel_medians=medians)
        if f is None:
            continue
        if f["stadsdel"] not in keep:
            f["stadsdel"] = "Other"
        onsale_feat_pairs.append((r, f))
    onsale_features = [f for _, f in onsale_feat_pairs]

    # --- fit on full sold, predict on-sale --------------------------------
    pred_log, beta = fit_and_predict(sold_features, log_y, onsale_features, stadsdel_levels, medians)
    predicted_price = np.exp(pred_log)

    # --- liquidity tiers from sold (used downstream by build_map.py) ------
    liquidity = compute_liquidity_tiers(sold_rows)

    # --- write scored output ----------------------------------------------
    out_path_p = Path(out_path) if out_path else Path(onsale_path).with_suffix(".scored.jsonl")
    scored = []
    for (row, f), p in zip(onsale_feat_pairs, predicted_price):
        asking = row.get("asking_price_kr")
        deal_pct = (p - asking) / asking * 100 if asking else None
        row["predicted_price_kr"] = int(round(float(p)))
        row["deal_pct"] = round(float(deal_pct), 1) if deal_pct is not None else None
        # tier is for the on-sale listing's own stadsdel; use the pre-fold
        # normalized name so sub-areas map back to their parent tier when
        # the parent itself is high-liquidity.
        own_s = normalize_stadsdel(row.get("area"))
        row["stadsdel_liquidity"] = liquidity.get(own_s, "low")
        scored.append(row)

    with open(out_path_p, "w") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(scored)} -> {out_path_p}")

    # --- top-K report -----------------------------------------------------
    rated = [r for r in scored if r.get("deal_pct") is not None]
    rated.sort(key=lambda r: r["deal_pct"], reverse=True)
    print("\nTop 10 candidate deals (highest predicted-vs-asking gap):")
    for r in rated[:10]:
        print(f"  {r['deal_pct']:+5.1f}%  ask {r['asking_price_kr']:>9,}  pred {r['predicted_price_kr']:>9,}"
              f"  {r['m2']:>4} m²  {r['address']}, {r['area']}")
    print("\nBottom 5 (priced above model — likely premium / well-renovated / something the model misses):")
    for r in rated[-5:]:
        print(f"  {r['deal_pct']:+5.1f}%  ask {r['asking_price_kr']:>9,}  pred {r['predicted_price_kr']:>9,}"
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
