"""
Step 3: classify zoning districts (A..J) from texture/tone, in PIXEL space.

Convention: identical to 02_extent.py -- full-res JPEG is 11173x8809, row 0 at
top. This script works on the JPEG's embedded 1/4-scale overview
(2794 x 2203, SCALE = 4) and writes outputs on the geotransform
(0, SCALE, 0, 0, 0, -SCALE), i.e. map_x = col*SCALE, map_y = -row*SCALE.

Pipeline:
  1. Read 1/4-scale RGB overview -> grayscale.
  2. Median-filter to suppress thin bright streets (denoise).
  3. Local brightness normalisation (divide by large-window background) to
     correct for paper aging / uneven exposure across the sheet.
  4. Multi-window local mean/std texture features (~15/31/61 full-res px).
  5. Gabor-filter energy at 0/45/90/135 deg to capture hatch orientation.
  6. Train RandomForest on legend-swatch interior pixels (A..J, codes 1..10).
  7. Predict inside the extent polygon only; force very bright pixels
     (streets/bayous/paper) and outside-extent pixels to class 0.
  8. Majority (mode) filter, then GDAL sieve to remove sub-0.5-acre specks.
  9. Write zones_px.tif, polygonize + dissolve by district -> zones_px.gpkg,
     and a colour-coded zones_preview.png next to the source image.
"""
import os
import json
import numpy as np
from osgeo import gdal, ogr
from scipy import ndimage as ndi
from skimage.filters import gabor_kernel
from skimage.filters.rank import majority
from skimage.morphology import square
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import shapely
from shapely import geometry as shg
from shapely.ops import unary_union

gdal.UseExceptions()

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "11139001.jpg")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CROSSWALK = f"{OUT_DIR}/crosswalk.json"
EXTENT_TIF = f"{OUT_DIR}/extent_px.tif"

FULL_W, FULL_H = 11173, 8809
SCALE = 4          # 1/4-scale overview
DS_W, DS_H = 2794, 2203

DISTRICTS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
CODE_OF = {d: i + 1 for i, d in enumerate(DISTRICTS)}

# px/ft measured from the scale bar (x~9350-9970, y~2560-2650 full-res): 7 ticks
# (0,500,...,3000 ft) span full-res x = 9489.5 .. 9944.5 -> 455 px / 3000 ft.
PX_PER_FT_FULL = 455.0 / 3000.0  # ~0.1517 full-res px per ft
ACRE_FULL_PX2 = 43560.0 * (PX_PER_FT_FULL ** 2)  # ~1003.7 full-res px^2 / acre
SIEVE_ACRES = 0.5
SIEVE_THRESH_DS_PX = max(8, int(round(SIEVE_ACRES * ACRE_FULL_PX2 / (SCALE ** 2))))

BRIGHT_UNCLASSIFIED_T = 210.0  # RAW denoised-gray above this -> forced class 0 (paper/streets)


def read_overview_rgb():
    ds = gdal.Open(SRC)
    bands = []
    for i in range(1, 4):
        b = ds.GetRasterBand(i)
        ov = b.GetOverview(1)  # index 1 == 1/4 scale (2794x2203)
        bands.append(ov.ReadAsArray())
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    return rgb


def read_extent_mask_at_scale():
    """Resample extent_px.tif (written at 1/8 scale) onto our 1/4-scale grid."""
    ds = gdal.Translate("", EXTENT_TIF, format="MEM", width=DS_W, height=DS_H,
                         resampleAlg="nearest")
    arr = ds.GetRasterBand(1).ReadAsArray()
    return arr > 0


def compute_features(gray):
    print("  median filter (street removal)...")
    denoised = ndi.median_filter(gray, size=5)

    print("  local background normalisation...")
    # Large window: correct slow paper-tone drift (aging/exposure) without washing out
    # district-to-district brightness differences (those operate at block scale, much
    # smaller than this window).
    bg = ndi.uniform_filter(denoised, size=301)
    norm = denoised / np.clip(bg, 1.0, None) * 200.0
    norm = np.clip(norm, 0, 255).astype(np.float32)

    feats = [denoised, norm]

    print("  multi-window local mean/std...")
    for full_win in (15, 31, 61):
        w = max(3, int(round(full_win / SCALE)))
        if w % 2 == 0:
            w += 1
        lm = ndi.uniform_filter(norm, size=w)
        lsq = ndi.uniform_filter(norm * norm, size=w)
        lstd = np.sqrt(np.clip(lsq - lm ** 2, 0, None))
        feats.append(lm)
        feats.append(lstd)

    print("  Gabor orientation energy (0/45/90/135 deg)...")
    # hatch line spacing in the print is roughly 3-6 full-res px -> ~1-1.5 ds px;
    # frequency chosen so the kernel responds to that pitch.
    freq = 0.25
    for angle_deg in (0, 45, 90, 135):
        theta = np.deg2rad(angle_deg)
        kernel = gabor_kernel(freq, theta=theta, sigma_x=3, sigma_y=3)
        resp = ndi.convolve(norm - norm.mean(), np.real(kernel), mode="reflect")
        respi = ndi.convolve(norm - norm.mean(), np.imag(kernel), mode="reflect")
        energy = np.sqrt(resp ** 2 + respi ** 2)
        energy = ndi.uniform_filter(energy, size=9)  # smooth to a local-energy map
        feats.append(energy)

    F = np.stack(feats, axis=-1).astype(np.float32)
    return F, denoised, norm


def main():
    with open(CROSSWALK) as f:
        crosswalk = json.load(f)
    swatch_px_full = crosswalk["legend_swatch_px"]

    rgb = read_overview_rgb()
    gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.float32)
    print("overview size", gray.shape)

    F, denoised, norm = compute_features(gray)
    n_feat = F.shape[-1]
    print("feature stack:", F.shape)

    extent = read_extent_mask_at_scale()
    print("extent px (ds):", extent.sum())

    # --- training samples from legend swatch interiors ---
    X_train, y_train = [], []
    half = 5  # ds px half-window inside each ~16x14 ds swatch (avoids borders)
    for d in DISTRICTS:
        cx_full, cy_full = swatch_px_full[d]
        cx, cy = int(round(cx_full / SCALE)), int(round(cy_full / SCALE))
        patch = F[cy - half:cy + half + 1, cx - half:cx + half + 1, :]
        patch = patch.reshape(-1, n_feat)
        X_train.append(patch)
        y_train.append(np.full(patch.shape[0], CODE_OF[d], dtype=np.int32))
    X_train = np.concatenate(X_train, axis=0)
    y_train = np.concatenate(y_train, axis=0)
    print("training samples:", X_train.shape, "classes:", np.unique(y_train))

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)

    clf = RandomForestClassifier(n_estimators=300, max_depth=18, min_samples_leaf=3,
                                  class_weight="balanced_subsample", n_jobs=-1,
                                  random_state=0)
    clf.fit(X_train_s, y_train)
    print("train accuracy (resubstitution):", clf.score(X_train_s, y_train))

    # --- predict inside extent, tile by row-block to bound memory ---
    labels = np.zeros((DS_H, DS_W), dtype=np.uint8)
    rows_per_tile = 300
    for r0 in range(0, DS_H, rows_per_tile):
        r1 = min(DS_H, r0 + rows_per_tile)
        block_mask = extent[r0:r1, :]
        if not block_mask.any():
            continue
        block_feat = F[r0:r1, :, :][block_mask]
        block_feat_s = scaler.transform(block_feat)
        pred = clf.predict(block_feat_s)
        tmp = np.zeros((r1 - r0, DS_W), dtype=np.uint8)
        tmp[block_mask] = pred.astype(np.uint8)
        labels[r0:r1, :] = tmp
        print(f"  predicted rows {r0}:{r1}")

    # outside extent -> 0
    labels[~extent] = 0
    # very bright pixels (unhatched paper, streets, bayous) -> 0 even if inside extent
    labels[denoised > BRIGHT_UNCLASSIFIED_T] = 0

    print("pre-postprocess class counts:", {int(c): int((labels == c).sum()) for c in np.unique(labels)})

    # --- majority (mode) filter to clean speckle ---
    print("majority filter...")
    labels_mode = majority(labels, footprint=square(5))
    # keep 0 authoritative outside extent / bright areas (don't let neighbors bleed a
    # district label into true paper/street pixels)
    labels_mode[~extent] = 0
    labels_mode[denoised > BRIGHT_UNCLASSIFIED_T] = 0

    # --- sieve tiny regions (~0.5 acre) ---
    print(f"sieve threshold = {SIEVE_THRESH_DS_PX} ds px "
          f"(~{SIEVE_ACRES} acre at {PX_PER_FT_FULL:.4f} full-res px/ft)")
    drv_mem = gdal.GetDriverByName("MEM")
    mem_ds = drv_mem.Create("", DS_W, DS_H, 1, gdal.GDT_Byte)
    mem_ds.GetRasterBand(1).WriteArray(labels_mode)
    gdal.SieveFilter(mem_ds.GetRasterBand(1), None, mem_ds.GetRasterBand(1),
                      SIEVE_THRESH_DS_PX, 4)
    sieved = mem_ds.GetRasterBand(1).ReadAsArray()
    sieved[~extent] = 0

    print("final class counts:", {int(c): int((sieved == c).sum()) for c in np.unique(sieved)})

    # --- write zones_px.tif ---
    out_tif = f"{OUT_DIR}/zones_px.tif"
    drv = gdal.GetDriverByName("GTiff")
    ds_out = drv.Create(out_tif, DS_W, DS_H, 1, gdal.GDT_Byte, options=["COMPRESS=LZW"])
    ds_out.SetGeoTransform((0, SCALE, 0, 0, 0, -SCALE))
    ds_out.GetRasterBand(1).WriteArray(sieved)
    ds_out.GetRasterBand(1).SetNoDataValue(0)
    ds_out.FlushCache()
    ds_out = None
    print("wrote", out_tif)

    # --- polygonize + dissolve by district ---
    src_ds = gdal.Open(out_tif)
    src_band = src_ds.GetRasterBand(1)
    gdrv = ogr.GetDriverByName("GPKG")
    import os
    gpkg_path = f"{OUT_DIR}/zones_px.gpkg"
    if os.path.exists(gpkg_path):
        gdrv.DeleteDataSource(gpkg_path)
    vds = gdrv.CreateDataSource(gpkg_path)

    tmp_layer_name = "zones_raw_tmp"
    tmp_layer = vds.CreateLayer(tmp_layer_name, srs=None, geom_type=ogr.wkbPolygon)
    tmp_layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    gdal.Polygonize(src_band, src_band.GetMaskBand(), tmp_layer, 0, ["8CONNECTED=8"])

    # collect geometries per code using shapely, skip code 0
    geoms_by_code = {}
    tmp_layer.ResetReading()
    for feat in tmp_layer:
        code = feat.GetField("code")
        if code == 0:
            continue
        geom = shg.shape(json.loads(feat.GetGeometryRef().ExportToJson()))
        geoms_by_code.setdefault(code, []).append(geom)

    out_layer = vds.CreateLayer("zones_1947", srs=None, geom_type=ogr.wkbMultiPolygon)
    out_layer.CreateField(ogr.FieldDefn("district", ogr.OFTString))
    out_layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    code_to_letter = {v: k for k, v in CODE_OF.items()}
    for code, geoms in sorted(geoms_by_code.items()):
        merged = unary_union(geoms)
        f = ogr.Feature(out_layer.GetLayerDefn())
        f.SetField("district", code_to_letter[code])
        f.SetField("code", int(code))
        f.SetGeometry(ogr.CreateGeometryFromWkb(merged.wkb))
        out_layer.CreateFeature(f)

    vds.ExecuteSQL(f"DROP TABLE {tmp_layer_name}")
    vds = None
    print("wrote", gpkg_path)

    # --- colour-coded preview ---
    palette = {
        0: (245, 245, 240),   # unclassified / paper / streets
        1: (255, 255, 190),   # A first dwelling
        2: (255, 220, 130),   # B second dwelling
        3: (255, 170, 90),    # C first apartment
        4: (230, 120, 60),    # D second apartment
        5: (240, 90, 160),    # E local business
        6: (190, 40, 140),    # F intermediate business
        7: (120, 0, 90),      # G central business
        8: (90, 140, 220),    # H first light industrial
        9: (40, 90, 190),     # I second light industrial
        10: (10, 30, 120),    # J heavy industrial
    }
    color_img = np.zeros((DS_H, DS_W, 3), dtype=np.uint8)
    for code, rgb_c in palette.items():
        color_img[sieved == code] = rgb_c

    orig_gray3 = np.stack([gray, gray, gray], axis=-1).astype(np.uint8)
    combo = np.concatenate([orig_gray3, color_img], axis=1)
    mem = drv_mem.Create("", combo.shape[1], combo.shape[0], 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(combo[..., i])
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/zones_preview.png", mem)
    print("wrote", f"{OUT_DIR}/zones_preview.png")

    # --- area report ---
    # Denominator is the EXTENT (mapped area) only, not the whole page -- most of the page
    # outside the extent polygon is blank paper / title / legend and was never a candidate
    # for classification in the first place, so including it would understate how much of
    # the actual map got classified.
    total_full_px2 = {}
    for code in range(0, 11):
        cnt_ds = int((sieved == code).sum())
        total_full_px2[code] = cnt_ds * (SCALE ** 2)
    extent_full_px2 = int(extent.sum()) * (SCALE ** 2)
    unclassified_in_extent_full_px2 = total_full_px2[0]  # sieved[~extent] was set to 0 too,
    # but total_full_px2[0] only counts pixels==0 -- recompute the in-extent-only unclassified
    # count explicitly:
    unclassified_in_extent_full_px2 = int(((sieved == 0) & extent).sum()) * (SCALE ** 2)
    area_report = {}
    for code in range(1, 11):
        letter = code_to_letter[code]
        px2 = total_full_px2[code]
        area_report[letter] = {
            "full_res_px2": px2,
            "pct_of_extent": 100.0 * px2 / extent_full_px2 if extent_full_px2 else 0.0,
        }
    meta = {
        "scale": SCALE,
        "downsampled_size": [DS_W, DS_H],
        "full_size": [FULL_W, FULL_H],
        "geotransform": [0, SCALE, 0, 0, 0, -SCALE],
        "px_per_ft_full_res": PX_PER_FT_FULL,
        "acre_full_res_px2": ACRE_FULL_PX2,
        "sieve_threshold_ds_px": SIEVE_THRESH_DS_PX,
        "sieve_threshold_acres": SIEVE_ACRES,
        "bright_unclassified_threshold": BRIGHT_UNCLASSIFIED_T,
        "extent_full_res_px2": extent_full_px2,
        "unclassified_within_extent_full_res_px2": unclassified_in_extent_full_px2,
        "unclassified_within_extent_pct": 100.0 * unclassified_in_extent_full_px2 / extent_full_px2 if extent_full_px2 else 0.0,
        "area_by_district": area_report,
        "feature_importances": dict(zip(
            ["gray_denoised", "norm_gray",
             "mean15", "std15", "mean31", "std31", "mean61", "std61",
             "gabor0", "gabor45", "gabor90", "gabor135"],
            [float(x) for x in clf.feature_importances_]
        )),
    }
    with open(f"{OUT_DIR}/zones_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
