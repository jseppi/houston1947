"""
Step 2b: a CLASSIFIER-INDEPENDENT fix for 02_extent.py's extent mask.

WHY: extent_px.tif (v1) leaks the sheet's printed neatline (a ruled border
line well inside the actual torn/mounted paper edge) into the extent -- the
neatline is dark, so 02_extent.py's paper-vs-zoned test calls it "zoned",
and because it forms a closed loop running the full perimeter of the page,
binary_closing bridges it to nearby blank paper and the "keep largest
connected component" step then keeps the whole frame-following blob,
including big blank margins between the neatline and the real city
boundary (visible in extent_preview.png as the red outline hugging the
page edge at top/bottom/left/right instead of stopping at real content).
A later downstream attempt to fix this filtered the extent using the
CLASSIFIER's output density, which is exactly the kind of bias this task
asks to remove: it disproportionately strips classes with naturally
sparser/lighter fills (verified against zones_meta_v2.json: D and F have
markedly lower px2-per-block-area than A/B/H/J, so a density filter tuned
against average classified density would erode D/F fastest).

FIX: detect the actual neatline directly from the scanned pixels (never
from zones_px_*, never from any classifier output) as a row/column
darkness-profile feature near each of the 4 page edges, hard-zero
everything on/outside its INNER edge (+ margin) BEFORE any morphological
step, then do ONLY geometry-based cleanup (opening at the map's ~250ft
scale + keep the largest connected component) -- no density filter, no
hole-fill heuristics, nothing that looks at zone labels.

NEATLINE DETECTION (see module-level report for the full derivation): for
each edge, walk a 1-D darkness-fraction profile (fraction of pixels with
gray < DARK_T in a band of rows/cols near that edge, excluding the
title/legend boxes) inward from the edge. The profile has three
features in order: (1) a solid dark band 0-1.0 (the scan's black
photo-mount/mat board, with a ragged/torn edge -- NOT the neatline), (2) a
flat low baseline (blank cream paper, matching ordinary paper-texture
noise), (3) a distinct elevated bump (the printed double-ruled neatline
itself). We explicitly skip past band (1)+(2) and take the INNER edge of
bump (3) as the neatline position, then add a small margin further inward.
Verified visually against crops at all 4 edges (see report) -- the
detected boundary sits cleanly on the blank-paper side of the neatline
with no real map content between it and the true developed-area boundary,
including at the west edge and the ship-channel tip in the east where the
task flagged the city boundary comes close to the frame.
"""
import os
import json
import numpy as np
from osgeo import gdal, ogr
from scipy import ndimage as ndi

gdal.UseExceptions()

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "11139001.jpg")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

FULL_W, FULL_H = 11173, 8809
SCALE = 8  # identical 1/8-overview convention as 02_extent.py
DS_W, DS_H = 1397, 1102

# Title and legend boxes -- identical full-res boxes to 02_extent.py
TITLE_BOX_FULL = (9350, 1800, 11100, 2500)
LEGEND_BOX_FULL = (9300, 3450, 11120, 4250)

# ---- neatline detection ----
NEATLINE_DARK_T = 180       # "dark ink" threshold on raw full-res gray
NEATLINE_FRAC_T = 0.1       # darkness-fraction threshold used to walk the profile
NEATLINE_SUSTAIN = 15       # samples that must stay below thresh to confirm the inner edge
NEATLINE_SEARCH_LIMIT = 600  # px from edge to search (neatline is well within this on all sides)
NEATLINE_MARGIN_FULL = 15   # extra margin inward from the detected inner edge, per task spec

# ---- geometry-only cleanup ----
PX_PER_FT_FULL = 455.0 / 3000.0  # same map-scale constant used in 02/03 scripts
OPENING_RADIUS_FT = 250.0
ACRE_FULL_PX2 = 43560.0 * (PX_PER_FT_FULL ** 2)


def to_ds(box):
    x0, y0, x1, y1 = box
    return (int(x0 / SCALE), int(y0 / SCALE), int(x1 / SCALE), int(y1 / SCALE))


def read_gray_full():
    """Independent of the classifier pipeline: reads straight from the
    source JPEG (reuses 03b's cache_v2/gray_full.npy if present purely as a
    speed-up -- it is plain pixel data with no classifier dependency -- but
    works standalone if that cache doesn't exist)."""
    cache = f"{OUT_DIR}/cache_v2/gray_full.npy"
    import os
    if os.path.exists(cache):
        return np.load(cache)
    ds = gdal.Open(SRC)
    bands = [ds.GetRasterBand(i).ReadAsArray() for i in (1, 2, 3)]
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    gray = np.clip(0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2], 0, 255)
    return gray.astype(np.uint8)


def read_overview_rgb():
    ds = gdal.Open(SRC)
    bands = []
    for i in range(1, 4):
        b = ds.GetRasterBand(i)
        ov = b.GetOverview(b.GetOverviewCount() - 1)
        bands.append(ov.ReadAsArray())
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    h, w = rgb.shape[:2]
    return rgb, w, h


def find_neatline_inner_edge(profile, start, direction, thresh, sustain, search_limit):
    """Walk `profile` from `start` stepping by `direction` (+1/-1).
    Phase 1: skip the initial high band (mat board / torn scan edge) until
    it drops below `thresh`.
    Phase 2: continue through the low baseline (blank paper) until it rises
    above `thresh` again -- this is the neatline's OUTER edge.
    Phase 3: continue until the profile drops below `thresh` and STAYS
    below it for `sustain` consecutive samples -- this is the neatline's
    INNER edge (the value we want)."""
    idx, n = start, 0
    while profile[idx] >= thresh and n < search_limit:
        idx += direction; n += 1
    while profile[idx] < thresh and n < search_limit:
        idx += direction; n += 1
    neatline_outer = idx
    while n < search_limit:
        if profile[idx] < thresh:
            ok = True
            for k in range(1, sustain):
                j = idx + direction * k
                if j < 0 or j >= len(profile) or profile[j] >= thresh:
                    ok = False
                    break
            if ok:
                break
        idx += direction; n += 1
    return neatline_outer, idx


def detect_neatline(gray_full):
    H, W = gray_full.shape
    row_mask = np.ones(H, dtype=bool)
    row_mask[TITLE_BOX_FULL[1]:TITLE_BOX_FULL[3]] = False
    row_mask[LEGEND_BOX_FULL[1]:LEGEND_BOX_FULL[3]] = False
    col_mask = np.ones(W, dtype=bool)
    col_mask[TITLE_BOX_FULL[0]:TITLE_BOX_FULL[2]] = False
    col_mask[LEGEND_BOX_FULL[0]:LEGEND_BOX_FULL[2]] = False

    dark = gray_full < NEATLINE_DARK_T
    row_prof = dark[:, col_mask].mean(axis=1)
    col_prof = dark[row_mask, :].mean(axis=0)

    top_outer, top_inner = find_neatline_inner_edge(
        row_prof, 0, 1, NEATLINE_FRAC_T, NEATLINE_SUSTAIN, NEATLINE_SEARCH_LIMIT)
    bottom_outer, bottom_inner = find_neatline_inner_edge(
        row_prof, H - 1, -1, NEATLINE_FRAC_T, NEATLINE_SUSTAIN, NEATLINE_SEARCH_LIMIT)
    left_outer, left_inner = find_neatline_inner_edge(
        col_prof, 0, 1, NEATLINE_FRAC_T, NEATLINE_SUSTAIN, NEATLINE_SEARCH_LIMIT)
    right_outer, right_inner = find_neatline_inner_edge(
        col_prof, W - 1, -1, NEATLINE_FRAC_T, NEATLINE_SUSTAIN, NEATLINE_SEARCH_LIMIT)

    bounds_full = {
        "top": top_inner + NEATLINE_MARGIN_FULL,
        "bottom": bottom_inner - NEATLINE_MARGIN_FULL,
        "left": left_inner + NEATLINE_MARGIN_FULL,
        "right": right_inner - NEATLINE_MARGIN_FULL,
    }
    detail = {
        "top": {"outer_px": int(top_outer), "inner_px": int(top_inner)},
        "bottom": {"outer_px": int(bottom_outer), "inner_px": int(bottom_inner)},
        "left": {"outer_px": int(left_outer), "inner_px": int(left_inner)},
        "right": {"outer_px": int(right_outer), "inner_px": int(right_inner)},
    }
    return bounds_full, detail


def main():
    gray_full = read_gray_full()
    print("full-res gray shape:", gray_full.shape)

    bounds_full, neatline_detail = detect_neatline(gray_full)
    print("neatline detail (full-res px):", json.dumps(neatline_detail, indent=2))
    print("final inner-boundary + margin (full-res px):", bounds_full)

    # ---- paper vs zoned test, IDENTICAL to 02_extent.py (this part is
    # already classifier-independent -- it only looks at scan brightness /
    # local texture, never at zones_px_*) ----
    rgb, w, h = read_overview_rgb()
    global DS_W, DS_H
    DS_W, DS_H = w, h
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    gray = (0.299 * r + 0.587 * g + 0.114 * b)

    win = 5
    local_mean = ndi.uniform_filter(gray, size=win)
    local_sqmean = ndi.uniform_filter(gray * gray, size=win)
    local_std = np.sqrt(np.clip(local_sqmean - local_mean ** 2, 0, None))
    BRIGHT_T = 212.0
    STD_T = 4.0
    paper = (gray > BRIGHT_T) & (local_std < STD_T)
    zoned = ~paper

    # ---- exclude title/legend boxes (same as 02_extent.py) ----
    tb = to_ds(TITLE_BOX_FULL)
    lb = to_ds(LEGEND_BOX_FULL)
    zoned[tb[1]:tb[3], tb[0]:tb[2]] = False
    zoned[lb[1]:lb[3], lb[0]:lb[2]] = False

    # ---- NEW: hard-zero everything on/outside the detected neatline,
    # BEFORE any morphology, so the frame can never seed or bridge into the
    # kept connected component (this is the actual fix for the frame leak) ----
    top_ds = max(0, int(bounds_full["top"] / SCALE))
    bottom_ds = min(DS_H, int(bounds_full["bottom"] / SCALE))
    left_ds = max(0, int(bounds_full["left"] / SCALE))
    right_ds = min(DS_W, int(bounds_full["right"] / SCALE))
    print(f"ds-scale neatline crop box: top={top_ds} bottom={bottom_ds} "
          f"left={left_ds} right={right_ds} (grid is {DS_W}x{DS_H})")
    zoned[:top_ds, :] = False
    zoned[bottom_ds:, :] = False
    zoned[:, :left_ds] = False
    zoned[:, right_ds:] = False

    # ---- ONLY geometry-based cleanup from here: opening at the map's
    # ~250ft scale, then keep the largest connected component. No hole
    # filling, no classifier/zone-label input of any kind. ----
    opening_radius_full_px = OPENING_RADIUS_FT * PX_PER_FT_FULL
    opening_radius_ds_px = max(1, int(round(opening_radius_full_px / SCALE)))
    print(f"opening radius: {opening_radius_full_px:.1f} full-res px "
          f"-> {opening_radius_ds_px} px at 1/{SCALE} scale")
    struct = np.ones((3, 3), dtype=bool) if opening_radius_ds_px <= 1 else None
    if struct is not None:
        zoned_opened = ndi.binary_opening(zoned, structure=struct, iterations=1)
    else:
        from skimage.morphology import disk, binary_opening as sk_binary_opening
        zoned_opened = sk_binary_opening(zoned, disk(opening_radius_ds_px))

    lbl, n = ndi.label(zoned_opened, structure=np.ones((3, 3)))
    if n == 0:
        raise RuntimeError("No connected components found - thresholds need tuning")
    sizes = ndi.sum(np.ones_like(lbl), lbl, index=np.arange(1, n + 1))
    biggest = 1 + int(np.argmax(sizes))
    extent = lbl == biggest
    print(f"n_components={n}, largest={sizes.max():.0f} ds-px2 "
          f"({100*sizes.max()/zoned_opened.sum():.1f}% of opened zoned area)")

    mask_u8 = (extent.astype(np.uint8)) * 255

    # ---- write extent_px_v2.tif ----
    drv = gdal.GetDriverByName("GTiff")
    out_tif = f"{OUT_DIR}/extent_px_v2.tif"
    ds_out = drv.Create(out_tif, DS_W, DS_H, 1, gdal.GDT_Byte, options=["COMPRESS=LZW"])
    ds_out.SetGeoTransform((0, SCALE, 0, 0, 0, -SCALE))
    ds_out.GetRasterBand(1).WriteArray(mask_u8)
    ds_out.GetRasterBand(1).SetNoDataValue(0)
    ds_out.FlushCache()
    ds_out = None
    print("wrote", out_tif)

    # ---- polygonize ----
    src_ds = gdal.Open(out_tif)
    src_band = src_ds.GetRasterBand(1)
    mask_band = src_band.GetMaskBand()
    gpkg_path = f"{OUT_DIR}/extent_px_v2.gpkg"
    gdrv = ogr.GetDriverByName("GPKG")
    import os
    if os.path.exists(gpkg_path):
        gdrv.DeleteDataSource(gpkg_path)
    vds = gdrv.CreateDataSource(gpkg_path)
    layer = vds.CreateLayer("extent_1947_v2", srs=None, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("val", ogr.OFTInteger))
    gdal.Polygonize(src_band, mask_band, layer, 0, ["8CONNECTED=8"], callback=None)
    layer.SetAttributeFilter("val = 0")
    for feat in layer:
        layer.DeleteFeature(feat.GetFID())
    layer.SetAttributeFilter(None)
    vds.ExecuteSQL("VACUUM")
    vds = None
    print("wrote", gpkg_path)

    # ---- preview PNG: downsampled grayscale with extent outline + the
    # detected neatline boundary drawn in a second colour for comparison ----
    edge = extent.astype(np.uint8) - ndi.binary_erosion(extent, iterations=2).astype(np.uint8)
    prev = np.stack([gray, gray, gray], axis=-1).astype(np.uint8)
    prev[edge.astype(bool)] = [255, 0, 0]
    # draw the raw neatline-derived crop box in blue for reference
    box_edge = np.zeros_like(extent)
    box_edge[top_ds, left_ds:right_ds] = True
    box_edge[bottom_ds - 1, left_ds:right_ds] = True
    box_edge[top_ds:bottom_ds, left_ds] = True
    box_edge[top_ds:bottom_ds, right_ds - 1] = True
    prev[box_edge] = [0, 0, 255]
    mem = gdal.GetDriverByName("MEM").Create("", DS_W, DS_H, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(prev[..., i])
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/extent_preview_v2.png", mem)
    print("wrote", f"{OUT_DIR}/extent_preview_v2.png")

    # ---- compare vs v1 ----
    v1_area_full_px2 = None
    v1_path = f"{OUT_DIR}/extent_meta.json"
    import os
    if os.path.exists(v1_path):
        with open(v1_path) as f:
            v1_area_full_px2 = json.load(f).get("extent_area_full_px2")

    area_full_px2 = int(extent.sum()) * SCALE * SCALE
    area_acres = area_full_px2 / ACRE_FULL_PX2

    meta = {
        "scale": SCALE,
        "downsampled_size": [DS_W, DS_H],
        "full_size": [FULL_W, FULL_H],
        "geotransform": [0, SCALE, 0, 0, 0, -SCALE],
        "pixel_convention": "map_x = col*SCALE (full-res col units); map_y = -row*SCALE",
        "title_box_full_px": TITLE_BOX_FULL,
        "legend_box_full_px": LEGEND_BOX_FULL,
        "neatline_detection": {
            "method": "row/col darkness-fraction (gray<180) profile near each "
                      "page edge, walked inward past the mat-board band to the "
                      "inner edge of the printed neatline bump; see module docstring",
            "dark_threshold": NEATLINE_DARK_T,
            "frac_threshold": NEATLINE_FRAC_T,
            "sustain_samples": NEATLINE_SUSTAIN,
            "margin_full_px": NEATLINE_MARGIN_FULL,
            "detail_full_px": neatline_detail,
            "final_bounds_full_px": bounds_full,
        },
        "opening_radius_ft": OPENING_RADIUS_FT,
        "opening_radius_full_px": opening_radius_full_px,
        "opening_radius_ds_px": opening_radius_ds_px,
        "px_per_ft_full": PX_PER_FT_FULL,
        "extent_pixel_count_ds": int(extent.sum()),
        "extent_area_full_px2": area_full_px2,
        "extent_area_acres": area_acres,
        "extent_area_full_px2_v1_for_comparison": v1_area_full_px2,
        "extent_area_full_px2_delta_v2_minus_v1": (
            area_full_px2 - v1_area_full_px2 if v1_area_full_px2 else None
        ),
        "no_classifier_output_used": True,
    }
    with open(f"{OUT_DIR}/extent_meta_v2.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))
    print("wrote", f"{OUT_DIR}/extent_meta_v2.json")


if __name__ == "__main__":
    main()
