# Houston's 1947 zoning plan vs. 2026 land use

How much of Houston's never-adopted **Zoning District Map of 15 December 1947** matches the city's land use today?
This repository digitises the 1947 map automatically from a scan, georeferences it, overlays 2026 parcel land use,
and measures the agreement with confidence intervals, spatial null models and a design-based accuracy assessment.

- **Interactive story page:** https://jseppi.github.io/houston1947/ (source in [`docs/`](docs/))
- **Results:** [`results.md`](results.md)
- **Full method (conference-paper style):** [`methodology.md`](methodology.md)

| 1947 group | Strict match with 2026 use | 95% CI | No-relationship baseline |
|---|---|---|---|
| Residential | 58% | 54–61 | 45% |
| Commercial | 38% | 33–42 | 12% |
| Industrial | 52% | 47–57 | 23% |

## Pipeline

| Step | Script | Key outputs |
|---|---|---|
| Study area | `02b_extent_v2.py` | `extent_px_v2.tif` |
| Classify districts A–J from screen textures | `03b_classify_fullres.py` → `03c_classify_v3.py` → `03d_g_filter.py` | `zones_px_v3g.tif` |
| Accuracy sample (blind) | `04c_accuracy_v3.py`, `judging_protocol.md` | `accuracy_v3_sample.csv`, `accuracy_v3_judged.txt` |
| Georeference (32 GCPs, order-2) | `05_georef_zones.py`, `gcp_model_compare.py` | `zones_2278.tif`, `georef_report.txt` |
| 2026 parcels | `06_fetch_parcels.py --buffer 3000`, `prep_parcel_fields.py` | `parcels_2026` layer |
| Overlay, statistics | `overlay_lib.py`, `stats.py` | `stats_report.md`, `stats_summary.csv`, `distance_decay.*` |
| Map accuracy, corrected estimates | `accuracy_v3.py` | `accuracy_v3_report.md` |
| Figure | `09_compare_map.py` | `compare_1947_2026.png` |

**Web map.** `10_web_layers.py` builds four PMTiles archives: the scan, the 1947 districts, 2026 land use, and
agreement. It also writes the page's data files to `docs/data/`. The archives are hosted on Cloudflare R2 rather than
committed. `deploy/r2_setup.sh` creates the bucket, sets CORS and uploads them. The page itself is plain static
HTML/JS in `docs/`, served by GitHub Pages; the basemap is OpenFreeMap.

`run_all.py` runs georeferencing → overlay → statistics → figure and writes `run_manifest.json`, which records input
hashes and package versions. Paths and parameters are in `config.py`. Earlier superseded versions are kept for the
record: `02_extent.py`, `03_classify.py`, `04_accuracy_sample.py`, `07_overlay.py`, `07b_overlay_raster.py` and
`08_sensitivity.py`.

**Environment.** Everything runs in the Python bundled with QGIS 3.40 (GDAL 3.10, NumPy 1.26). The scripts also use
scikit-learn, SciPy, scikit-image and Shapely 2. Keep NumPy at 1.26, because NumPy 2 breaks the QGIS GDAL bindings.

**Not in the repo.**
- `cache_v2/`: intermediate arrays, about 1.8 GB.
- `houston1947.gpkg`: the full working GeoPackage including 266,542 parcels, 118 MB.

Rebuild the working GeoPackage with `05_georef_zones.py` and then `06_fetch_parcels.py --buffer 3000`. The 1947 zone
and extent layers alone are in `houston1947_zones.gpkg`.

## Data sources and attribution

- **1947 map scan** (`11139001.jpg`; catalogue record in `11139001_metadata.txt`): *Zoning district map: Houston,
  Texas. December 15, 1947*, City of Houston, 1947.
  - From the **David Rumsey Map Collection**, David Rumsey Map Center, Stanford Libraries, list no. 11139.001
    (https://www.davidrumsey.com).
  - Rumsey collection images are distributed under a Creative Commons Attribution-NonCommercial-ShareAlike licence
    (CC BY-NC-SA). The scan, and the images and rasters derived from it in this repository, are shared on those terms.
    Check the collection's current terms before reuse.
- **2026 land use:** City of Houston, *HoustonMap/Landuse* map service (from Harris County Appraisal District data),
  retrieved 26 September 2026.
- **Ground control:** street centrelines © OpenStreetMap contributors (ODbL).
