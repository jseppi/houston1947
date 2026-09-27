"""
Step 2: extract the mapped/zoned-area extent (vs blank cream paper) from the
1947 Houston zoning map JPEG, in PIXEL space (no georeferencing).

Convention: all pixel coordinates are (col, row) of the FULL-RESOLUTION JPEG
(11173 x 8809), with row 0 at the top. Rasters/vectors produced here use the
GDAL geotransform (0, SCALE, 0, 0, 0, -SCALE) so that:
    map_x =  col * SCALE   (increases to the right, same as image columns)
    map_y = -row * SCALE   (negative of the row, so "up" in map space = up in image)
This is a standard no-CRS "pixel" raster-to-vector convention (y = -row) which
keeps polygonized geometries orientated the normal (non-flipped) way for GIS
tools while still letting you recover row = -y / SCALE, col = x / SCALE.

Processing is done at 1/8 scale using the JPEG's embedded overview
(1397 x 1102) to keep memory low. SCALE = 8 (documented in extent_meta.json).
"""
import os
import json
import numpy as np
from osgeo import gdal
from scipy import ndimage as ndi

gdal.UseExceptions()

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "11139001.jpg")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

FULL_W, FULL_H = 11173, 8809
SCALE = 8  # use the embedded 1/8 overview
DS_W, DS_H = 1397, 1102  # actual overview size (11173/8=1396.6, 8809/8=1101.1 rounded up by GDAL)

# Title and legend boxes given in FULL-RES pixel coords (x0,y0,x1,y1)
TITLE_BOX_FULL = (9350, 1800, 11100, 2500)
LEGEND_BOX_FULL = (9300, 3450, 11120, 4250)
# Also exclude a little extra margin around the outer sheet border (full-res px)
BORDER_MARGIN_FULL = 40


def to_ds(box):
    x0, y0, x1, y1 = box
    return (int(x0 / SCALE), int(y0 / SCALE), int(x1 / SCALE), int(y1 / SCALE))


def read_overview_rgb():
    ds = gdal.Open(SRC)
    bands = []
    for i in range(1, 4):
        b = ds.GetRasterBand(i)
        ov_count = b.GetOverviewCount()
        # pick the overview whose size matches DS_W/DS_H most closely (last, smallest, is usually 1/8)
        ov = b.GetOverview(ov_count - 1)
        arr = ov.ReadAsArray()
        bands.append(arr)
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    h, w = rgb.shape[:2]
    return rgb, w, h


def main():
    rgb, w, h = read_overview_rgb()
    print("overview size:", w, h)
    global DS_W, DS_H
    DS_W, DS_H = w, h

    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    gray = (0.299 * r + 0.587 * g + 0.114 * b)

    # Cream paper: bright AND locally smooth (blank paper has only faint street lines so
    # local std stays low at this downsampled scale; zoned area has hatch/pattern fills that
    # push brightness down and/or local std up even where the fill itself looks "light").
    win = 5  # ~40 full-res px
    local_mean = ndi.uniform_filter(gray, size=win)
    local_sqmean = ndi.uniform_filter(gray * gray, size=win)
    local_std = np.sqrt(np.clip(local_sqmean - local_mean ** 2, 0, None))

    BRIGHT_T = 212.0
    STD_T = 4.0
    bright = gray > BRIGHT_T
    smooth = local_std < STD_T
    paper = bright & smooth

    zoned = ~paper

    # Explicitly blank out title & legend boxes, and the outer border margin (sheet neatline)
    # BEFORE morphology, so they can't get fused into the mapped-area component and don't
    # survive as separate islands either.
    struct = np.ones((3, 3), dtype=bool)
    tb = to_ds(TITLE_BOX_FULL)
    lb = to_ds(LEGEND_BOX_FULL)
    zoned[tb[1]:tb[3], tb[0]:tb[2]] = False
    zoned[lb[1]:lb[3], lb[0]:lb[2]] = False
    m = max(1, int(BORDER_MARGIN_FULL / SCALE))
    zoned[:m, :] = False
    zoned[-m:, :] = False
    zoned[:, :m] = False
    zoned[:, -m:] = False

    # Remove salt-and-pepper speckle (dust/foxing on blank paper, stray marks) with a small
    # opening, then a modest closing to bridge thin bright streets / bayous cutting through
    # zoned blocks. NOTE: iterations are deliberately small here (1 and 2) -- the printed
    # sheet's double-ruled neatline forms a complete rectangle enclosing the whole page, so an
    # aggressive closing + a blanket binary_fill_holes would treat the wide blank margins
    # between the neatline and the irregular city boundary as "enclosed holes" and wrongly
    # merge them into the extent (verified during development: this exact failure mode
    # inflated the mask to ~98% of the page). We deliberately do NOT call binary_fill_holes on
    # the whole mask for the same reason.
    zoned_clean = ndi.binary_opening(zoned, structure=struct, iterations=1)
    zoned_closed = ndi.binary_closing(zoned_clean, structure=struct, iterations=2)

    # Keep only the largest connected component (the mapped city area)
    lbl, n = ndi.label(zoned_closed, structure=np.ones((3, 3)))
    if n == 0:
        raise RuntimeError("No connected components found - thresholds need tuning")
    sizes = ndi.sum(np.ones_like(lbl), lbl, index=np.arange(1, n + 1))
    biggest = 1 + int(np.argmax(sizes))
    extent = lbl == biggest

    # Selective hole fill: fill only SMALL enclosed background pockets (bridged street grids,
    # small park/bayou slivers not fully closed above). Large enclosed background pockets are
    # genuine blank-paper areas *inside the neatline but outside the developed city* (e.g. the
    # big rectangular blank areas north and east of downtown) and must stay excluded, so they
    # are left alone based on a pixel-count cap (in downsampled px).
    HOLE_FILL_CAP_DS_PX = 8000
    bg = ~extent
    bg_lbl, bg_n = ndi.label(bg, structure=np.ones((3, 3)))
    border_ids = set(np.unique(np.concatenate(
        [bg_lbl[0, :], bg_lbl[-1, :], bg_lbl[:, 0], bg_lbl[:, -1]])))
    border_ids.discard(0)
    bg_sizes = ndi.sum(np.ones_like(bg_lbl), bg_lbl, index=np.arange(1, bg_n + 1))
    fill = np.zeros_like(extent)
    for i in range(1, bg_n + 1):
        if i in border_ids:
            continue
        if bg_sizes[i - 1] <= HOLE_FILL_CAP_DS_PX:
            fill |= (bg_lbl == i)
    extent = extent | fill

    mask_u8 = (extent.astype(np.uint8)) * 255

    # --- write extent_px.tif ---
    drv = gdal.GetDriverByName("GTiff")
    out_tif = f"{OUT_DIR}/extent_px.tif"
    ds_out = drv.Create(out_tif, DS_W, DS_H, 1, gdal.GDT_Byte, options=["COMPRESS=LZW"])
    ds_out.SetGeoTransform((0, SCALE, 0, 0, 0, -SCALE))
    ds_out.GetRasterBand(1).WriteArray(mask_u8)
    ds_out.GetRasterBand(1).SetNoDataValue(0)
    ds_out.FlushCache()
    ds_out = None
    print("wrote", out_tif)

    # --- polygonize to extent_px.gpkg / extent_1947 ---
    src_ds = gdal.Open(out_tif)
    src_band = src_ds.GetRasterBand(1)
    mask_band = src_band.GetMaskBand()

    gpkg_path = f"{OUT_DIR}/extent_px.gpkg"
    from osgeo import ogr
    gdrv = ogr.GetDriverByName("GPKG")
    import os
    if os.path.exists(gpkg_path):
        gdrv.DeleteDataSource(gpkg_path)
    vds = gdrv.CreateDataSource(gpkg_path)
    layer = vds.CreateLayer("extent_1947", srs=None, geom_type=ogr.wkbPolygon)
    fld = ogr.FieldDefn("val", ogr.OFTInteger)
    layer.CreateField(fld)
    gdal.Polygonize(src_band, mask_band, layer, 0, ["8CONNECTED=8"], callback=None)

    # Keep only polygons with val==255 (the extent, not the 0-background), dissolve into one
    # multipolygon-ish set by deleting background features.
    layer.SetAttributeFilter("val = 0")
    for feat in layer:
        layer.DeleteFeature(feat.GetFID())
    layer.SetAttributeFilter(None)
    vds.ExecuteSQL("VACUUM")
    vds = None
    print("wrote", gpkg_path)

    # --- preview PNG: downsampled grayscale with extent outline ---
    edge = extent.astype(np.uint8) - ndi.binary_erosion(extent, iterations=2).astype(np.uint8)
    prev = np.stack([gray, gray, gray], axis=-1).astype(np.uint8)
    prev[edge.astype(bool)] = [255, 0, 0]
    from osgeo import gdal as gdal2
    pdrv = gdal2.GetDriverByName("PNG")
    mem = gdal2.GetDriverByName("MEM").Create("", DS_W, DS_H, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(prev[..., i])
    pdrv.CreateCopy(f"{OUT_DIR}/extent_preview.png", mem)
    print("wrote", f"{OUT_DIR}/extent_preview.png")

    meta = {
        "scale": SCALE,
        "downsampled_size": [DS_W, DS_H],
        "full_size": [FULL_W, FULL_H],
        "geotransform": [0, SCALE, 0, 0, 0, -SCALE],
        "pixel_convention": "map_x = col*SCALE (full-res col units); map_y = -row*SCALE",
        "title_box_full_px": TITLE_BOX_FULL,
        "legend_box_full_px": LEGEND_BOX_FULL,
        "extent_pixel_count_ds": int(extent.sum()),
        "extent_area_full_px2": int(extent.sum()) * SCALE * SCALE,
    }
    with open(f"{OUT_DIR}/extent_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print("wrote", f"{OUT_DIR}/extent_meta.json")


if __name__ == "__main__":
    main()
