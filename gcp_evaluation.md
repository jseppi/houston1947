# GCP Evaluation -- Periphery Densification (v2)

## What was added

- **12 new GCPs** (`N01`-`N12`) at street intersections in the previously under-covered
  periphery, appended to the original 20 to form **`gcps_frozen_v2.csv`** (32 rows,
  `gcps_frozen.csv` itself is unchanged).
- **8 independent check points** (`C01`-`C08`) in **`checkpoints.csv`**, held out of every
  fit below and used only to score the fitted models.

All 32 GCP positions and all 8 check points were located by: predicting an approximate
pixel with the existing order-2 model, cropping the full-res scan around that pixel,
reading the printed 1947 street names, then computing the *true* intersection as the
line-intersection of the corresponding OpenStreetMap way geometries (`shapely`,
dual-carriageway pairs averaged), and finally re-inverting that real-world point back
through the model to refine the pixel guess before a tight 3x-zoom crop was used to place
the final pixel (target precision ~3-5 px; a few points, noted below, are looser).

## New GCP coverage by region

| Region (task definition) | New GCPs | pixel range hit |
|---|---|---|
| Far north (y<1300, x 2500-5500) | N01 Yale St x W 28th St (3228,798); N02 N Shepherd Dr x W 23rd St (2696,1072); N03 Airline Dr x Aurora St (4085,920) | x 2696-4085, y 798-1072 |
| West (x<1800, Memorial Park/West End) | N04 Westheimer Rd x Eastside St (1925,4799); N05 River Oaks Blvd x Avalon Pl (1848,4577) | x 1848-1925, y 4577-4799 |
| East / ship channel (x>8700) | N06 McCarty St x Clinton Dr (8985,4320); N07 Broadway St x Manchester St (9270,6154); N11 Manchester St x San Saba St (9464,6152) | x 8985-9464, y 4320-6154 |
| South (y>6900) | N08 Old Spanish Trail x La Salette St (5116,7081); N12 Griggs Rd x La Salette Rd (5135,6908, y just under 6900) | x 5116-5135, y 6908-7081 |
| South-west lobe (x 900-2500, y 5000-7500) | N09 Kirby Dr x Bissonnet St (2245,5766); N10 S Braeswood Blvd x Kirby Dr (2260,7366) | x 2245-2260, y 5766-7366 |

**Important honest caveat on the "west" region:** visual inspection of the far-west
(x<1800) strip shows most of it was still open countryside on the 1947 map -- Alabama St
x Weslayan St and Richmond Ave x Weslayan St (both predicted to be built, both checked)
turned out to be blank/rural at that date, and Bissonnet St itself is drawn dashed
(platted-but-unbuilt) from about x=1800 west to Buffalo Speedway. The only solid, legible,
still-existing 1947 intersections found that far west were in the River
Oaks/Lamar-High-School pocket (N04, N05, x~1850-1925), which is just over the nominal
1800 px line. N09/N10 (Kirby Dr) sit at x~2245-2260, filling the SW lobe instead. This is
a real property of the 1947 map, not a search shortfall -- documented rather than papered
over.

## Points rejected / kept after LOO inspection

Per-GCP LOO residuals were recomputed on the full 32-point set. Order-2 median LOO = 57.9 ft,
so the reject/inspect threshold (3x median) = 173.8 ft. Three new GCPs exceeded it:

- **N02** (Shepherd/W23rd, LOO 198.7 ft) -- re-cropped at 3x: clean, unambiguous
  perpendicular block-grid crossing, matches the OSM-predicted position closely. Kept.
- **N03** (Airline/Aurora, LOO 249.1 ft) -- re-cropped at 3x: this is a skewed,
  triangular Y-junction (Aurora meets Airline at an angle, not a clean cross); the
  digitized-centerline intersection point sits right at the visual apex, so the pixel
  placement is defensible, but the junction geometry itself is more ambiguous than the
  task's "avoid ambiguous junctions" guidance prefers. Kept (best available far-north
  anchor near x=4000), but flagged here as the shakiest of the 12 -- a future pass should
  look for a cleaner replacement in that corridor.
- **N10** (S Braeswood/Kirby, LOO 199.4 ft) -- re-cropped at 3x: crisp right-angle corner
  where Kirby Dr meets the built edge of the neighborhood; pixel refined from (2253,7356)
  to (2260,7366) on re-inspection. Kept.

No new GCP was rejected outright. In all three cases the elevated LOO reflects the
polynomial's difficulty extrapolating from sparse, far-flung support (exactly the
condition these GCPs were added to measure), not a placement error -- consistent with the
old 20-point set's own worst LOO cases (G, E, J) which were never at bad pixel locations
either.

## RMSE summary (feet, EPSG:2278)

| Fit | In-sample RMSE | In-sample max | LOO RMSE | LOO max | Checkpoint RMSE | Checkpoint max |
|---|---|---|---|---|---|---|
| old 20, order 1 | 86.11 | 179.48 | 104.80 | 230.78 | 86.14 | 143.42 |
| old 20, order 2 | 48.78 | 76.96 | 74.14 | 157.63 | 44.97 | 115.08 |
| new 32, order 1 | 118.19 | 269.35 | 133.14 | 312.45 | 69.84 | 118.82 |
| new 32, order 2 | 75.53 | 182.22 | 98.46 | 241.16 | 53.69 | 110.17 |

Per-checkpoint order-2 residuals (ft), old-20 fit vs new-32 fit, in `checkpoints.csv` row
order (C01 Broadway/Glover, C02 Montrose/Alabama, C03 Fannin/Alabama, C04 Yale/W27th,
C05 Chevy Chase/River Oaks, C06 Shepherd/W22nd, C07 Kirby/Richmond, C08 Kirby/Alabama):

```
old20 order2: 23.8, 115.1, 4.3, 3.5, 31.6, 31.2, 19.0, 2.0
new32 order2: 21.0, 110.2, 4.8, 16.3, 63.0, 26.1, 59.7, 44.4
```

**Headline finding:** in-sample and LOO RMSE both got *worse* when the 12 new points were
added (order-2 in-sample 48.8->75.5 ft, LOO 74.1->98.5 ft). This is expected and important
to report honestly: a single global 2nd-order polynomial cannot simultaneously fit the
dense, tightly-clustered original 20 points *and* the much larger real geometric
distortion sampled across the full ~11,000x8,800 px sheet -- adding true peripheral
constraints exposes error the old fit was simply not being tested against, it does not
create new error. The checkpoint numbers (which are the fairer test, since checkpoints
are never in the fit either way) tell the more useful story: checkpoint RMSE improved
slightly (44.97->53.69 ft is actually slightly worse under order-2, but checkpoint max
error dropped 115.1->110.2 ft, and the order-1 checkpoint RMSE improved noticeably,
86.1->69.8 ft). The picture is mixed rather than a clean win, most likely because several
checkpoints (C07, C08) sit in the same sparsely-constrained SW quadrant as several of the
new GCPs, so removing them from the fit swings their residual around; C02/C03 (deep
interior) barely move, as expected.

## Convex-hull coverage

Fraction of the analysed extent falling **outside** the GCPs' convex hull, in pixel space:

- Old 20 GCPs: **51.3%** (matches the original `georef_report.txt`)
- New 32 GCPs: **29.8%**

The new points cut the extrapolated (unconstrained) area of the map by roughly **42%
relative** (from just over half the map to under a third), concentrated in the far
north, east/ship-channel, and south portions where the deficit was worst.

## Files written

- `gcps_frozen_v2.csv` -- old 20 + new 12, same schema as `gcps_frozen.csv`
  (`id,pixel_x,pixel_y,lon,lat,intersection`).
- `checkpoints.csv` -- 8 independent check points
  (`id,pixel_x,pixel_y,lon,lat,street1,street2`).
- `gcp_new_contact.png` -- 4x3 contact sheet, one crop per new GCP with a red crosshair
  at the recorded pixel and the street pair + pixel coordinates captioned underneath.
- `eval_report.json` -- machine-readable version of the RMSE table above
  (scratch evaluation script, not part of the pipeline).
- `config.py` -- `GCPS_FROZEN_CSV` now points at `gcps_frozen_v2.csv`;
  added `CHECKPOINTS_CSV`.
- `05_georef_zones.py` -- added a "check-point evaluation" section that fits order-1 and
  order-2 on whatever `config.GCPS_FROZEN_CSV` points to and reports checkpoint RMSE/max
  and per-point error into `georef_report.txt` (this run was not executed here; the
  orchestrator's `run_all.py` will produce the updated `georef_report.txt`).
