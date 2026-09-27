# Statistics report (task 5)

## Observed strict/cumulative %, chance-corrected index, null, bootstrap CI

| Group | Denom (ac, excl. neutral) | Strict % | Strict 95% CI | Cumulative % | Cumulative 95% CI | Marginal baseline (strict) | Chance-corrected index (strict) | Null mean / p95 (strict) | Empirical p-value |
|---|---|---|---|---|---|---|---|---|---|
| Residential | 19,397 | 57.8% | [54.4, 60.9] | 79.1% | [77.0, 81.1] | 45.3% | 0.23 | 46.0% / 51.0% | 0.0000 |
| Commercial | 2,248 | 37.7% | [33.3, 41.5] | 77.9% | [74.1, 81.5] | 12.1% | 0.29 | 13.9% / 18.7% | 0.0000 |
| Industrial | 7,257 | 52.1% | [47.3, 56.9] | 99.9% | [99.9, 100.0] | 22.9% | 0.38 | 26.9% / 37.3% | 0.0000 |

## By district (strict / cumulative %, corrected denominator)

| District | Strict % | Cumulative % |
|---|---|---|
| A | 52.3% | 82.3% |
| B | 68.7% | 80.0% |
| C | 56.8% | 76.8% |
| D | 28.2% | 54.4% |
| E | 41.0% | 85.8% |
| F | 27.9% | 61.0% |
| G | 71.8% | 93.5% |
| H | 40.0% | 99.8% |
| I | 26.2% | 99.9% |
| J | 65.8% | 100.0% |

## Where the plan missed (share of each group's denominator landing in another 2026 group)

| 1947 group | Residential | Commercial | Industrial | Public/Park |
|---|---|---|---|---|
| Residential | 59.7% | 7.6% | 12.1% | 20.5% |
| Commercial | 30.7% | 37.7% | 22.0% | 9.5% |
| Industrial | 22.3% | 16.4% | 52.1% | 8.8% |

## Sensitivity: headline strict % per group across input/toggle variants

| Commercial_strict_pct | Industrial_strict_pct | Residential_strict_pct | note | variant |
|---|---|---|---|---|
| 37.7 | 52.1 | 57.8 |  | headline (zones v3g, extent v2, vacant=neutral, dedupe neutral_first) |
| 36.1 | 50.7 | 53.9 |  | vacant NOT treated as neutral |
| 37.5 | 52.6 | 57.8 |  | dedupe burn order reversed (neutral_last) |
| 38.4 | 52.3 | 57.8 |  | TPS warp instead of order-2 polynomial |
| 36.6 | 52.1 | 57.7 |  | zones v2 (zones_px_v2.tif, same GCPs/order-2 registration) |
| 37.4 | 52.1 | 57.8 |  | zones v3 (zones_px_v3.tif, same GCPs/order-2 registration) |
| 37.7 | 52.1 | 57.8 |  | zones v3g (zones_px_v3g.tif, same GCPs/order-2 registration) |
|  |  |  | both extent_px.tif (v1) and extent_px_v2.tif (v2) exist; the headline run already uses v2 per config.py's auto-detection (EXTENT_PX_VERSION=v2). A held-fixed v1-vs-v2 comparison would require re-polygonizing extent_1947 from each and is not run here to keep this pass's runtime bounded -- see extent_meta_v2.json for the v1-vs-v2 area delta (-9,245,760 full-res px2, i.e. v2 is smaller / tighter than v1). | v1 vs v2 extent extraction |

## Methodology notes

- Toroidal-shift null: 400 shifts, magnitude 3000-20000 ft, random direction, np.roll wrap (toroidal); the extent mask and 2026 parcels are held fixed, only the 1947 zone raster is shifted. Wrapping can pull in content from the opposite edge of the raster (an approximation noted in toroidal_null()'s docstring).
- Distance-decay: strict % averaged over 8 directions at each magnitude from 0 to 3000 ft in steps of 250 ft. See distance_decay.csv / distance_decay.png.
- Spatial block bootstrap: 600 reps, resampling 2000 ft square blocks (~200x200 px) with replacement across the 1073 blocks covering the extent's bounding grid.
