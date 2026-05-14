"""Ablation experiments for the hedonic scorer.

Compares the impact of:
  - stadsdel encoder granularity (coarse vs fine)
  - ownership-type features (is_house, is_tomratt, is_aganderatt, is_andel)
  - training-set scope (all property types vs apartments only)

Same train/test split (same seed) is used for all configs so MAPE is comparable.

    python3 experiment.py --sold data/sold-2026-05-14.enriched.jsonl
"""
import argparse, json, math
from pathlib import Path
import numpy as np

import score


BASELINE_NUM = ["log_m2", "byggar_decade", "vaning_eff", "hiss_int", "log_avgift"]
OWNERSHIP_NUM = BASELINE_NUM + ["is_house", "is_tomratt", "is_aganderatt", "is_andel"]


def featurize_set(sold_rows, *, stadsdel_fn, apartments_only):
    """Featurize sold rows with given encoder; optionally filter to apartments."""
    pairs = []
    for r in sold_rows:
        if r.get("price_kr") is None or r["price_kr"] <= 0:
            continue
        if apartments_only and r.get("bostadstyp") != "Lägenhet":
            continue
        f = score.featurize(r, stadsdel_medians=None, stadsdel_fn=stadsdel_fn)
        if f is None:
            continue
        pairs.append((r, f))
    return pairs


def run_config(label, sold_rows, *, stadsdel_fn, apartments_only,
               numeric_features, fold_fine_to_coarse, seed=0):
    pairs = featurize_set(sold_rows, stadsdel_fn=stadsdel_fn, apartments_only=apartments_only)
    feats = [f for _, f in pairs]
    areas = [r.get("area") for r, _ in pairs]

    if fold_fine_to_coarse:
        score.fold_fine_buckets(feats, areas)

    keep = score.fold_rare_stadsdelar(feats)
    for f in feats:
        if f["stadsdel"] not in keep:
            f["stadsdel"] = "Other"

    levels = sorted({f["stadsdel"] for f in feats})
    medians = score.compute_medians(feats)
    log_y = np.array([math.log(r["price_kr"]) for r, _ in pairs])

    m = score.evaluate_holdout(feats, log_y, levels, medians,
                               seed=seed, numeric_features=numeric_features)

    print(f"{label:42s}  n={len(feats):5d}  buckets={len(levels):4d}  "
          f"MAPE={m['mape_pct']:5.2f}%  median={m['median_ape_pct']:5.2f}%  "
          f"p90={m['p90_ape_pct']:5.2f}%")
    return m


def run_split_config(label, sold_rows, *, stadsdel_fn, apartments_only,
                     numeric_features, fold_fine_to_coarse, seed=0):
    """Train inner/outer models separately; report per-region MAPE + weighted overall."""
    inner = [r for r in sold_rows if score.region_of(r.get("area")) == "inner"]
    outer = [r for r in sold_rows if score.region_of(r.get("area")) == "outer"]
    n_in = sum(1 for r in inner if r.get("price_kr") and r.get("price_kr") > 0
               and (not apartments_only or r.get("bostadstyp") == "Lägenhet"))
    n_out = sum(1 for r in outer if r.get("price_kr") and r.get("price_kr") > 0
                and (not apartments_only or r.get("bostadstyp") == "Lägenhet"))

    m_in = run_config(f"{label} [inner]", inner, stadsdel_fn=stadsdel_fn,
                      apartments_only=apartments_only, numeric_features=numeric_features,
                      fold_fine_to_coarse=fold_fine_to_coarse, seed=seed)
    m_out = run_config(f"{label} [outer]", outer, stadsdel_fn=stadsdel_fn,
                       apartments_only=apartments_only, numeric_features=numeric_features,
                       fold_fine_to_coarse=fold_fine_to_coarse, seed=seed)

    # Sample-weighted combined MAPE (weight by test-set size, which is 20% of each n)
    n_total = m_in["n_test"] + m_out["n_test"]
    w_in = m_in["n_test"] / n_total
    w_out = m_out["n_test"] / n_total
    combined = w_in * m_in["mape_pct"] + w_out * m_out["mape_pct"]
    print(f"{label + ' [combined weighted]':42s}  n={n_in + n_out:5d}  "
          f"split={n_in}/{n_out}  combined MAPE={combined:5.2f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sold", required=True, help="sold listings JSONL")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    sold_rows = [json.loads(l) for l in Path(args.sold).read_text().splitlines()]
    print(f"Loaded {len(sold_rows)} sold rows from {args.sold}\n")

    configs = [
        # label,                       encoder,                      apt_only, numeric,       fold_fine
        ("A  coarse, no ownership, all",     score.normalize_stadsdel, False, BASELINE_NUM,   False),
        ("B  coarse, +ownership, all",       score.normalize_stadsdel, False, OWNERSHIP_NUM,  False),
        ("C  fine,   +ownership, all",       score.stadsdel_fine,      False, OWNERSHIP_NUM,  True),
        ("D  coarse, no ownership, apt-only",score.normalize_stadsdel, True,  BASELINE_NUM,   False),
        ("E  coarse, +ownership, apt-only",  score.normalize_stadsdel, True,  OWNERSHIP_NUM,  False),
        ("F  fine,   +ownership, apt-only",  score.stadsdel_fine,      True,  OWNERSHIP_NUM,  True),
    ]
    print(f"{'config':42s}  {'n':>5s}  {'buckets':>7s}  {'MAPE':>6s}  {'median':>6s}  {'p90':>6s}")
    print("-" * 100)
    for label, enc, apt, num, fold in configs:
        run_config(label, sold_rows, stadsdel_fn=enc, apartments_only=apt,
                   numeric_features=num, fold_fine_to_coarse=fold, seed=args.seed)

    print("\n--- Split-model: train inner/outer separately ---")
    run_split_config("G  split, fine, +own, apt-only",
                     sold_rows, stadsdel_fn=score.stadsdel_fine, apartments_only=True,
                     numeric_features=OWNERSHIP_NUM, fold_fine_to_coarse=True, seed=args.seed)
    run_split_config("H  split, coarse, +own, apt-only",
                     sold_rows, stadsdel_fn=score.normalize_stadsdel, apartments_only=True,
                     numeric_features=OWNERSHIP_NUM, fold_fine_to_coarse=False, seed=args.seed)


if __name__ == "__main__":
    main()
