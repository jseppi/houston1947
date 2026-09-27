# Methodology: Measuring the Correspondence Between Houston's 1947 Zoning Plan and 2026 Land Use

## 1. Research design

We ask to what extent the land-use pattern prescribed by the City of Houston's *Zoning District Map* of 15 December
1947 corresponds to the pattern of land use observed in 2026. Houston did not adopt the 1947 plan and has no zoning
ordinance. The comparison therefore measures *de facto* correspondence between a planned and a realised land-use
pattern, not compliance with regulation.

The unit of analysis is land area. The study area is restricted to the territory mapped on the 1947 sheet.

The design has five components:

1. **Digitise the 1947 districts:** automatic, texture-based classification of a scanned paper map.
2. **Georeference** the classified map to a modern projected coordinate system using ground control points.
3. **Represent 2026 land use** with parcel-level appraisal land-use categories.
4. **Overlay and measure:** spatially overlay the two layers and compute area-based agreement measures.
5. **Inference and validation:**
   - chance baselines and spatial permutation nulls;
   - spatial bootstrap confidence intervals;
   - a design-based accuracy assessment of the digitised 1947 map, used both to report map accuracy and to produce
     misclassification-corrected estimates.

All processing was scripted in Python 3.12 with GDAL/OGR 3.10, NumPy 1.26, SciPy, scikit-learn and Shapely 2.0, as
bundled with QGIS 3.40. A single driver script (`run_all.py`) reproduces every reported number from frozen inputs and
writes a manifest recording SHA-256 hashes of all inputs and scripts, package versions and the parcel-retrieval
timestamp (`run_manifest.json`).

## 2. Data

### 2.1 The 1947 Zoning District Map

**Source:** a colour scan of the printed *Zoning District Map, Houston, Texas, December 15, 1947*, file `11139001.jpg`.
- Size: 11,173 × 8,809 px, RGB, JPEG.
- Scale: about 0.1517 px per foot, measured from the printed scale bar (7 ticks spanning 0–3,000 ft). One pixel is
  about 6.6 ft on the ground.

**Symbology:** the map is monochrome. Its ten districts are distinguished by printed screen patterns (Table 1). The
legend describes a *cumulative* ordinance: each district permits all uses of the preceding districts plus additional
uses.

**Table 1. 1947 districts, legend definitions, and screen patterns.**

| Code | Name | Permitted uses (legend) | Screen pattern | Group |
|---|---|---|---|---|
| A | First Dwelling | One-family dwellings, churches, schools, libraries | Fine dark stipple (lightest fill) | Residential |
| B | Second Dwelling | A + two-family dwellings, garage apartments | Descending lines with bow-tie marks | Residential |
| C | First Apartment | B + apartments, boarding houses, hospitals, private schools | Coarse lattice of steep rising bars | Residential |
| D | Second Apartment | C + stores on ground floor | Vertical "ladder" lines | Residential |
| E | Local Business | D + retail stores, offices, filling stations, restaurants | Dense dark square crosshatch | Commercial |
| F | Intermediate Business | E | Dark diamond crosshatch | Commercial |
| G | Central Business | F | Solid black | Commercial |
| H | First Light Industrial | G + light industry not offensive by smoke, noise, etc. | Fine uniform 45° rising lines | Industrial |
| I | Second Light Industrial | H | Dark ground, thin light descending lines | Industrial |
| J | Heavy Industrial | All uses (eleven subject to approval of location) | Dark ground, regular grid of white dots | Industrial |

### 2.2 2026 land use

**Source:** Harris County Appraisal District (HCAD) tax-year 2026 appraisal land-use codes, as grouped by the City of
Houston (Enterprise GIS; layer "Land Use (Grouped)": *"Land Use derived by City of Houston staff based on appraisal
district land use codes"*). They were served through the City's *Land Use* map service
(`https://mycity2.houstontx.gov/gisweb01/rest/services/HoustonMap/Landuse/MapServer/0`). The native coordinate
system is NAD83 Texas South Central, US survey feet (EPSG:2278).

**Reference date:** Texas appraisal districts value property as of 1 January of the tax year. The land-use codes
therefore describe each parcel as recorded by HCAD for valuation on 1 January 2026. They are administrative records,
not a field survey, and may lag recent changes on the ground.

**Coverage check:** the service mixes counties and years:
- Harris County parcels are tax year 2026;
- Montgomery County parcels are tax year 2025;
- Fort Bend and Waller parcels carry no tax year and no land-use group.

The study area lies wholly within Harris County. Of the 313,197 parcels intersecting its bounding box, 313,137 are
tax year 2026; the other 60 (0.02%) have no tax year.

**Retrieval:** we retrieved every parcel intersecting the 1947 study area buffered by 3,000 ft, so that later
refinements of the registration remained covered. This gave 266,542 parcels, retrieved 26 September 2026 through the
service's paged query interface.

**Attributes kept:**
- the grouped land-use category `GROUP_DSCR`, with 11 classes: Single-Family Residential, Multi-Family Residential,
  Commercial, Office, Industrial, Public & Institutional, Transportation & Utility, Park & Open Spaces, Undeveloped,
  Agriculture Production, Unknown;
- the detailed HCAD land-use description `LANDUSE_DSCR`.

The numeric group code (`GROUP_CD`) is not unique to one category: code 2 is used for both Commercial and Multi-Family
Residential. Categories were therefore keyed on the description field.

**Ground control:** coordinates of street intersections were taken from OpenStreetMap way geometries (Section 4).

## 3. Digitising the 1947 districts

### 3.1 Study-area delineation

**What the extent is:** the mapped city area, which is irregular and bounded by the 1947 city limits.

**How it was delineated** (script `02b_extent_v2.py`), on a 1:8 downsampled grayscale image:
- **Blank paper:** pixels that are both bright (gray > 212) and locally smooth (standard deviation < 4 in a 5-px window).
- **Sheet neatline:** detected from edge-darkness profiles. Everything on or outside the inner neatline plus a 15 px
  margin was excluded, as were the title and legend boxes.
- **Cleanup:** a morphological opening with a radius equivalent to 250 ft removed thin artefacts, and the largest
  connected component was kept.

**Independence from the classifier:** the delineation uses no output of the district classifier. An earlier version
used a density filter on classified pixels to remove the neatline frame. A referee check showed it removed
disproportionate area from districts D and F, so it was replaced.

**Result:** the study area covers 53.6 M scan px², about 53,500 acres at the map's nominal scale.

### 3.2 Texture classification

**Why full resolution:** districts are encoded as periodic screens whose periods are only a few pixels. An initial
classifier run on a 1:4 downsampled image destroyed these textures and was abandoned; it labelled about 69% of the
area as district A. All decisions below are made at full resolution (script `03b_classify_fullres.py`).

**1. Street and background mask.** Street, bayou and paper pixels were defined as bright *and* locally smooth
(9-px-window standard deviation), then closed to bridge printed street names and opened to remove bright dots
belonging to screens. The connected components of the complement form "blocks".

**2. Patch features.**
- **Patches:** 48 × 48 px, on a 16-px grid, whose street fraction is below a threshold.
- **Frequency features:** mean-subtracted and Hann-windowed; the 2-D FFT magnitude is summarised into
  8 orientation × 4 radial-frequency bins (32 features, normalised), plus the dominant orientation and frequency.
- **Tone features:** mean gray, standard deviation, dark fraction and bright-dot fraction.
- **Properties:** the frequency features are translation-invariant but orientation-sensitive. This is essential,
  because several districts differ only in hatch direction (C, H, I).

**3. Training data.** Patches from the interiors of the ten legend swatches, with small brightness and contrast
jitter.

**4. Classifier.** Random forest (Breiman 2001): 400 trees, maximum depth 20, minimum leaf size 2.
- **Self-training:** one round. Map patches predicted with posterior ≥ 0.75, capped at 800 per class, were added to
  the training set and the model refitted.
- **Why only one round, with floor and cap:** a two-round variant without a confidence floor drifted, inflating
  class G from 257 to 46,783 patches.

**5. Aggregation.**
- Blocks up to about 60 acres receive the area-weighted majority vote of their patches.
- Larger connected regions receive a mode-filtered per-patch label instead. These arise where the street mask fails
  to separate blocks, notably most of the inner city.
- The result is written on a 1:2 grid (about 13 ft per pixel).

**6. Central Business District rule** (scripts `03c_classify_v3.py`, `03d_g_filter.py`).

*Problem:* the patch classifier systematically missed district G. Downtown blocks are too small for a 48-px patch to
fall wholly inside one, and in situ the solid fill shows more tonal noise than the legend swatch.

*Seed rule:* a pixel is a G seed if, in a 15-px window, all three hold:
- mean gray < 95;
- standard deviation < 20;
- fewer than 3% of pixels brighter than 150.

The thresholds were calibrated on measured block interiors of G, J and H/I. G interiors: mean 68–78, SD 10–16, bright
fraction ≤ 0.008. J and H/I interiors: SD ≥ 22, bright fraction ≥ 0.03.

*Growth:* seeds were grown by 8-connected geodesic dilation, up to 40 px, through pixels satisfying a relaxed version
of the rule. Because growth needs a connected chain of qualifying pixels, it stops at street edges even where the
street mask had failed.

*Post-filter:* a G component reverts to its previous label if either holds:
- it is larger than 600 half-resolution px² (twice the largest downtown block);
- it contains more than 20 bright-dot blobs per 1,000 px, the signature of J's dot screen.

This removed two false G regions inside faint-dot J fields and left 62 G components in the central business district.

## 4. Georeferencing

### 4.1 Ground control

We used 32 ground control points (GCPs) at street intersections that exist today with unchanged alignment.
- **Positions:** each pixel position was placed at the centreline intersection on a 3× enlarged crop of the scan,
  precise to about ±3 px (about ±20 ft).
- **Coordinates:** each modern coordinate is the intersection of the two OpenStreetMap centrelines. For dual
  carriageways we used the midpoint of the intersections.
- **Coverage:**
  - The initial 20 GCPs covered the core of the map but left 51% of the study area outside their convex hull.
  - Twelve peripheral GCPs were added (north, east/ship channel, south, south-west), cutting that share to 30%.
  - The far west could not be anchored further: many modern intersections there were unbuilt in 1947.

A further **8 independent check points** were collected by the same procedure and never used in fitting.

### 4.2 Transformation model

Candidate models were fitted directly from scan pixels to EPSG:2278, the same space in which the warp is applied
(Table 2):
- first- and second-order polynomials;
- thin-plate splines (TPS; Bookstein 1989), with smoothing parameter s from 0 to 10⁻², on pixel coordinates scaled by
  10⁻⁴.

**Table 2. Registration error (ft).**

| GCP set | Model | LOO RMSE | LOO max | Check-point RMSE | Check-point max |
|---|---|---|---|---|---|
| 20 core | Polynomial 1 | 104.8 | 230.8 | 86.1 | 143.4 |
| 20 core | Polynomial 2 | 74.1 | 157.6 | 45.0 | 115.1 |
| 32 all | Polynomial 1 | 133.1 | 312.4 | 69.8 | 118.8 |
| 32 all | **Polynomial 2 (adopted)** | **98.5** | 241.2 | **53.7** | 110.2 |
| 32 all | TPS (s = 0) | 88.4 | 177.2 | 66.3 | 120.9 |
| 32 all | TPS (s = 10⁻²) | 91.2 | 200.6 | 54.6 | 115.8 |

**Choice of model:** we adopted the second-order polynomial on all 32 GCPs.
- It gives the lowest check-point error among models fitted to the full set.
- It covers the largest share of the study area within the GCP hull.
- Its in-sample RMSE is 75.5 ft.

**Why error rose with more GCPs:** in-sample and LOO error increase from 20 to 32 GCPs. This reflects real distortion
at the sheet margins, which the core-only set was never tested against; it was not introduced by the new points.

**Sensitivity:** TPS was retained as an alternative warp (Section 7).

**Output:** the classified map was warped with nearest-neighbour resampling to a 10 ft grid in EPSG:2278.

### 4.3 Registration diagnostics

A **distance-decay test** gives independent support for the registration. We shifted the warped 1947 layer rigidly
by 250–3,000 ft in eight directions and recomputed agreement. Agreement is maximal at zero displacement and declines
monotonically for all three groups (Section 6.4).

## 5. Overlay and agreement measures

### 5.1 Parcel processing

**Stacked parcels.**
- Condominium and other stacked units share identical footprints: 923 clusters, containing 25,505 parcels.
- Within each exact-duplicate cluster (identical WKB geometry), the most frequent category was retained.
- Ties went to a non-neutral category, then alphabetically.

**Rasterisation.**
- Parcels were burned onto the 10 ft analysis grid by centre-point rule, in a fixed order: neutral categories first,
  so developed uses prevail where footprints overlap.
- Grid cells covered by no parcel (streets, freeways, bayous) are *right-of-way*. They are excluded from all
  denominators and reported separately.

### 5.2 Neutral land

"Neutral" parcels are counted neither for nor against the plan and are excluded from denominators, because their
current use carries no information about conformance. Neutral comprises:
- Undeveloped and Unknown parcels;
- parcels with null category;
- all parcels whose detailed description begins with "Vacant". HCAD codes vacant exempt land under Public &
  Institutional or Transportation & Utility, which would otherwise count as conforming use.

### 5.3 Crosswalk

Each 1947 district *d* was mapped to two sets of 2026 categories (Table 3):
- **strict set S(d):** the uses the district was *intended* for;
- **cumulative set C(d):** all uses the district *permitted* under the cumulative ordinance.

**Table 3. Crosswalk from 1947 districts to 2026 land-use categories.**

| District | Strict set S(d) | Additional categories in cumulative set C(d) |
|---|---|---|
| A | Single-Family | Public & Institutional, Park & Open Spaces |
| B, C | Single-Family, Multi-Family | Public & Institutional, Park & Open Spaces |
| D | Single-Family, Multi-Family | Public & Institutional, Park & Open Spaces, Commercial |
| E, F, G | Commercial, Office | Single-Family, Multi-Family, Public & Institutional, Park & Open Spaces |
| H, I | Industrial, Transportation & Utility | All except Agriculture |
| J | Industrial, Transportation & Utility | All |

The sets follow the legend text in Table 1; the ordinance text itself was not consulted.

### 5.4 Measures

For a 1947 group *g* (Residential A–D, Commercial E–G, Industrial H–J), let *a(d, u)* be the parcel area in district
*d* with 2026 category *u*. Let *N* be the neutral categories.

**Strict agreement:**

  *Strict(g)* = Σ_{d∈g} Σ_{u∈S(d)} a(d,u) / Σ_{d∈g} Σ_{u∉N} a(d,u)

**Cumulative agreement** is defined analogously with C(d).

**Group-level measures** apply each member district's own sets. They are therefore area-weighted averages of the
district measures, weighted by conformance-relevant area.

**Complementary "leakage" measure:** the share of each group's denominator now in each *other* 2026 use group
(residential, commercial, industrial, public/park), on the same denominator.

## 6. Inference

### 6.1 Independence baseline and chance-corrected index

**Baseline.** The expected agreement if 1947 zoning and 2026 land use were statistically independent is:

  *E(g)* = Σ_{d∈g} w_d Σ_{u∈S(d)} π_u

where:
- π_u is the share of category *u* in all non-neutral parcel area of the study area;
- w_d is district *d*'s share of the group's area.

**Index.** We report κ-type chance-corrected agreement, (Strict − E) / (1 − E) (cf. Cohen 1960).

**Limitation.** The independence baseline ignores spatial autocorrelation. It is therefore complemented by a spatial
null (6.2).

### 6.2 Toroidal-shift null

**Procedure** (Lotwick and Silverman 1982):
- The 1947 zone raster was translated with wrap-around by 400 random vectors, 3,000–20,000 ft long, in random
  directions.
- The study-area mask and the 2026 parcels were held fixed.
- Agreement was recomputed for each translation.

**Why it's the better null:** it preserves the internal spatial structure of both layers and destroys only their
registration.

**What is reported:** the null mean, its 95th percentile, and an empirical one-sided p-value.

**Caveat:** wrap-around introduces some edge artefacts.

### 6.3 Spatial block bootstrap

Confidence intervals for the observed measures come from a spatial block bootstrap (Künsch 1989):
- the study area's grid is tiled into 1,073 square blocks of 2,000 ft;
- blocks are resampled with replacement over 600 replicates;
- the 2.5th and 97.5th percentiles are reported.

**Caveat:** agreement is still declining at 3,000 ft (Section 6.4), so spatial dependence extends beyond the block
size. Blocks are therefore not fully independent, and these intervals may be somewhat optimistic.

### 6.4 Distance decay

Mean strict agreement over eight shift directions, as a function of rigid displacement, is computed for each group.
It serves two purposes:
- a registration diagnostic (the peak should be at zero);
- a description of the spatial scale of correspondence.

## 7. Accuracy assessment of the 1947 map

### 7.1 Sampling design

The digitised 1947 map was assessed following the good-practice recommendations of Olofsson et al. (2014).
- **Stratification:** by *mapped* group, with 90 points each in Residential, Commercial and Industrial (n = 270). Points
  were drawn uniformly at random within each stratum inside the study area, with a fixed seed.
- **Stratum weights:** W_h are the mapped area shares: Residential 0.663, Commercial 0.083, Industrial 0.254.
- **Why equal allocation:** it deliberately oversamples the rare Commercial stratum, to support its user's accuracy.

### 7.2 Reference labelling

**Response design:**
- Each point was rendered as a 180 × 180 px full-resolution tile, about 1,190 ft square, with the point marked.
  Tiles appeared in random order, identified only by an index, with legend swatches alongside.
- The predicted class was never shown.
- The rater assigned the district whose screen fills the block under the mark, following a written protocol that
  distinguishes each screen (`judging_protocol.md`; Table 1).
- A mark on a street, bayou or blank paper was labelled "no zone" (N); such a point is a commission error of the map.

**Rater:** labelling was performed by a single rater, the analyst, using an AI vision model.

**Repeatability:** measured by re-labelling a shuffled 30-tile subset of an earlier sample.
- District-level agreement: 83%.
- Group-level agreement: 90%, Cohen's κ = 0.85.
- Every disagreement involved a diagonal-hatch district (B, C, H, I).

**Earlier samples (disclosed):**
- A 60-point sample of an earlier classifier version, labelled *with* predictions visible. Not blind; not used.
- An 80-point blind sample of the same earlier version.

Neither enters the estimates below.

### 7.3 Estimators

**Map accuracy.** Area-weighted error-matrix proportions p̂_ij = W_i n_ij / n_i. From these:
- overall accuracy;
- user's and producer's accuracy;
- error-adjusted group areas;

with standard errors from the stratified estimators given by Olofsson et al. (2014). Confidence intervals are
95% (±1.96 SE).

**Misclassification-corrected agreement.** For each sample point, the 2026 category was read at its location after
mapping the point through the adopted registration. For each *true* (reference) group *g*, we estimated the ratio

  R(g) = P(reference group g, non-neutral parcel, use ∈ S(reference district)) / P(reference group g, non-neutral parcel)

- **Estimator:** the stratified combined ratio estimator (Cochran 1977), with linearised variance.
- **What it avoids:** the bias that map misclassification introduces into the wall-to-wall overlay; it does not rely
  on the map's labels.
- **Cost:** its precision is limited by sample size, because only points on non-neutral parcels contribute:
  79 residential, 27 commercial, 60 industrial.

## 8. Results summary

The headline results are in Table 4.

**Table 4. Agreement between 1947 zoning and 2026 land use.**

| 1947 group | Denominator (ac) | Strict (95% CI) | Corrected strict (95% CI) | Baseline E | Chance-corr. index | Toroidal null mean / p95 | Cumulative (baseline) |
|---|---|---|---|---|---|---|---|
| Residential | 19,397 | 57.8% (54.4–60.9) | 60.4% (48.5–72.2) | 45.3% | 0.23 | 46.0% / 51.0% | 79.1% (62.6%) |
| Commercial | 2,248 | 37.7% (33.3–41.5) | 37.3% (14.2–60.4) | 12.1% | 0.29 | 13.9% / 18.7% | 77.9% (76.9%) |
| Industrial | 7,257 | 52.1% (47.3–56.9) | 52.9% (39.2–66.6) | 22.9% | 0.38 | 26.9% / 37.3% | 99.9% (99.9%) |

**Significance.** Observed strict agreement exceeds all 400 toroidal shifts for every group (p < 0.0025).

**Cumulative agreement** is informative only for the residential group. The commercial and industrial districts
permitted almost every use, so their cumulative agreement equals chance.

**Map accuracy** at the group level:

| Group | User's accuracy | Producer's accuracy |
|---|---|---|
| Overall | 0.864 ± 0.042 | |
| Residential | 0.922 ± 0.056 | 0.925 ± 0.025 |
| Industrial | 0.856 ± 0.073 | 0.839 ± 0.084 |
| Commercial | 0.422 ± 0.103 | 0.668 ± 0.271 |

- **Main error:** commission into E (Local Business) from D and H patterns.
- **Effect on the headline:** despite the low commercial user's accuracy, the corrected and wall-to-wall commercial
  estimates agree closely (37.3% vs 37.7%), so the map-based figure is not materially biased. The corrected figure is
  much less precise.

**Distance decay.** Agreement is maximal at zero displacement for all groups. At 3,000 ft displacement it falls to:
- residential: 49.0% (from 57.8%);
- commercial: 17.0% (from 37.7%);
- industrial: 36.1% (from 52.1%).

## 9. Sensitivity analyses

Headline strict agreement was recomputed under alternative processing choices (Table 5).

**Table 5. Sensitivity of strict agreement to processing choices.**

| Variant | Residential | Commercial | Industrial |
|---|---|---|---|
| Headline | 57.8 | 37.7 | 52.1 |
| Vacant parcels *not* treated as neutral | 53.9 | 36.1 | 50.7 |
| Duplicate burn order reversed (neutral last) | 57.8 | 37.5 | 52.6 |
| Thin-plate-spline warp | 57.8 | 38.4 | 52.3 |
| Earlier classifier (v2; no Central Business rule) | 57.7 | 36.6 | 52.1 |
| v3 classifier before G post-filter | 57.8 | 37.4 | 52.1 |

**Result:** no choice moves any group by more than 4 percentage points. The largest effect comes from how vacant
exempt land is treated.

**Registration displacements:** displacements comparable to the check-point error (≤ 250 ft) change agreement by about
1 point for residential and industrial and 5 points for commercial (Section 8).

## 10. Limitations

1. **Single rater.** Reference labels were produced by one rater (an AI vision model directed by the analyst), with
   intra-rater κ = 0.85 at the group level. Inter-rater reliability was not assessed. The hardest distinctions are
   among the diagonal-hatch screens, and C/H confusion crosses the residential/industrial boundary.
2. **Commercial precision.** The Commercial group is small (8% of mapped area), and its misclassification-corrected
   estimate has a wide interval (±23 points).
3. **Registration.**
   - 30% of the study area lies outside the GCP convex hull, mainly in the west, where the 1947 map shows few
     surviving streets, so the polynomial extrapolates there.
   - Check-point error (54 ft RMSE, 110 ft maximum) is small relative to typical block size (about 250–300 ft), but
     block-level comparisons near district boundaries remain uncertain.
4. **Land-use proxy.** HCAD appraisal categories are a proxy for land use, not a zoning classification. They classify
   whole parcels by predominant use and cannot represent vertical mixing.
5. **Crosswalk source.** The crosswalk derives from the map legend, not from the full ordinance text.
6. **Extent version.** The effect of the extent revision (v1 to v2, −14.7% area, removing the neatline frame) is
   reported as an area difference only; the overlay was not re-run on the v1 extent.
7. **Toroidal wrap.** The toroidal null wraps raster edges, which slightly misrepresents structure near the boundary
   of the bounding grid.

## 11. Reproducibility

**Pipeline** (`CODE/houston1947/`):

| Script | Step |
|---|---|
| `02b_extent_v2.py` | Study area |
| `03b_classify_fullres.py` → `03c_classify_v3.py` → `03d_g_filter.py` | Classification |
| `04c_accuracy_v3.py` | Accuracy sample |
| `05_georef_zones.py` | Registration and warp; registration diagnostics in `georef_report.txt` |
| `06_fetch_parcels.py`, `prep_parcel_fields.py` | Parcels |
| `stats.py` | Overlay and inference |
| `accuracy_v3.py` | Map accuracy and corrected estimates |
| `09_compare_map.py` | Figure |

**Configuration:** `config.py` holds all paths and parameters.

**Frozen inputs:** `gcps_frozen_v2.csv` and `checkpoints.csv`, with hashes in `run_manifest.json`.

**Reference labels and sample:** `accuracy_v3_judged.txt` (labels), `accuracy_v3_sample.csv` (sample), `rater_repeat_*`
(repeatability data).

## Data sources

City of Houston (1947). *Zoning district map: Houston, Texas. December 15, 1947* [map, 1:19,000]. Scan: David Rumsey Map Collection, David Rumsey Map Center, Stanford Libraries, list no. 11139.001 (CC BY-NC-SA).

City of Houston, Enterprise GIS (2026). *Land Use (Grouped)*, HoustonMap/Landuse map service, derived from Harris County Appraisal District tax-year 2026 appraisal land-use codes (valuation date 1 January 2026). https://mycity2.houstontx.gov/gisweb01/rest/services/HoustonMap/Landuse/MapServer/0 (retrieved 26 September 2026).

OpenStreetMap contributors (2026). Street centreline geometries used for ground control points. https://www.openstreetmap.org (ODbL).

## References

Bookstein, F. L. (1989). Principal warps: Thin-plate splines and the decomposition of deformations. *IEEE Transactions on Pattern Analysis and Machine Intelligence*, 11(6), 567–585.

Breiman, L. (2001). Random forests. *Machine Learning*, 45(1), 5–32.

Cochran, W. G. (1977). *Sampling Techniques* (3rd ed.). Wiley.

Cohen, J. (1960). A coefficient of agreement for nominal scales. *Educational and Psychological Measurement*, 20(1), 37–46.

Künsch, H. R. (1989). The jackknife and the bootstrap for general stationary observations. *The Annals of Statistics*, 17(3), 1217–1241.

Lotwick, H. W., & Silverman, B. W. (1982). Methods for analysing spatial processes of several types of points. *Journal of the Royal Statistical Society, Series B*, 44(3), 406–413.

Olofsson, P., Foody, G. M., Herold, M., Stehman, S. V., Woodcock, C. E., & Wulder, M. A. (2014). Good practices for estimating area and assessing accuracy of land change. *Remote Sensing of Environment*, 148, 42–57.
