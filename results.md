# How much of Houston's 1947 zoning plan was "implemented"? (final, post-review)

This compares the Dec 15 1947 *Zoning District Map, Houston, Texas* with 2026 parcel land use, within the 1947 map's
mapped area (≈53,500 ac). The 2026 land use is Harris County Appraisal District tax-year 2026 land-use codes
(valuation date 1 January 2026), as grouped by the City of Houston and retrieved 26 September 2026. The plan was never adopted, so "implemented" means *de facto correspondence*.
The full method is in `methodology.md`.

## Headline: share of 1947-zoned parcel land whose 2026 use matches the plan

Undeveloped, Unknown and Vacant parcels and right-of-way are excluded from the denominator.

| 1947 group | Strict match (map) | 95% CI | Strict match, corrected for map errors | 95% CI | No-relationship baseline | Chance-corrected index |
|---|---|---|---|---|---|---|
| Residential | **58%** | 54–61 | 60% | 49–72 | 45% | 0.23 |
| Commercial | **38%** | 33–42 | 37% | 14–60 | 12% | 0.29 |
| Industrial | **52%** | 47–57 | 53% | 39–67 | 23% | 0.38 |

- **Map-based CIs** come from a spatial block bootstrap (2,000 ft blocks, 600 replicates).
- **"Corrected for map errors"** uses a design-based ratio estimator: the point's *true* 1947 district is judged blind
  from the scan, on 270 stratified sample points. It has wider CIs but carries no classification bias.
- **Significance:** every group beats all 400 toroidal random shifts of the 1947 layer (p < 0.0025).
  The null means are 46%, 14% and 27%.

## Permitted-use (cumulative) match by group

This counts any 2026 use that the cumulative 1947 ordinance allowed in the district, not only its intended use.
Same denominator as above.

| 1947 group | Cumulative match (map) | 95% CI | Corrected for map errors | No-relationship baseline | Chance-corrected index | Spatial null mean / p95 | p-value vs null |
|---|---|---|---|---|---|---|---|
| Residential | **79%** | 77–81 | 78% | 63% | 0.44 | 63% / 69% | < 0.0025 |
| Commercial | **78%** | 74–82 | 86% | 77% | 0.04 | 79% / 88% | 0.65 |
| Industrial | **~100%** | 99.9–100 | 100% | ~100% | −0.08 | ~100% / 100% | 0.71 |

- **Residential:** the permitted-use match beats chance clearly (79% vs 63%). Most of the difference from the strict
  figure is public/institutional and park land, which the plan allowed in residential districts.
- **Commercial and industrial:** the permitted-use match is **no better than chance** (p = 0.65 and 0.71). Those
  districts permitted almost every use, so nearly all their land "conforms" wherever the district lines are drawn.
  For these groups only the strict measure carries information.
- The corrected column uses the same design-based estimator as above; its confidence intervals are in
  `accuracy_v3_report.md`.

**Reading:**
- A majority of residential-zoned land is residential today. Most of the rest is public/institutional or park use,
  which the plan permitted there.
- About half of the industrial-zoned land is industrial or transport/utility today, roughly twice chance.
- About three-eighths of the business-zoned land is commercial or office today, about three times chance.
- **Where the plan missed** (shares of each group's denominator):

  | 1947 group | Became residential | Became commercial | Became industrial | Became public/park |
  |---|---|---|---|---|
  | Residential | — | 8% | 12% | 20% |
  | Commercial | 31% | — | 22% | 10% |
  | Industrial | 22% | 16% | — | 9% |

## Data quality

| Item | Value |
|---|---|
| 1947 map accuracy (group level, area-weighted) | Overall 86% ± 4%. User's / producer's accuracy: Residential 92/93%, Industrial 86/84%, Commercial 42/67% |
| Georeferencing | 32 GCPs, order-2 polynomial. RMSE: 54 ft on 8 held-out check points; leave-one-out 98 ft. 30% of the area lies outside the GCP hull |
| Sensitivity (headline range across variants) | Residential 54–58%, Commercial 36–38%, Industrial 51–53% |

Sensitivity variants: TPS warp, zone map version, vacant handling, and parcel de-duplication order.

**Files:**
- `stats_report.md`, `stats_summary.csv`, `accuracy_v3_report.md`, `georef_report.txt`, `gcp_model_compare.csv`,
  `distance_decay.png`, `compare_1947_2026.png`
- `houston1947.gpkg` (all layers)
- `run_all.py`, which reproduces the numbers from frozen inputs (see `run_manifest.json`)
