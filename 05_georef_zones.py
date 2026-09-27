"""
Step 5: georeference the 1947 zoning classification raster and the mapped-extent
mask into a real-world CRS (EPSG:2278, NAD83 Texas South Central, US ft), using
ground control points (GCPs) collected on the full-resolution source JPEG
(11139001.jpg, 11173x8809).

Referee fixes in this version
------------------------------
* Uses the FROZEN 20-GCP set (config.GCPS_FROZEN_CSV), not the live CSV, so the
  warp is reproducible even if more GCPs get added later.
* Fits the polynomial in the SAME space GDAL actually warps in. Previously the
  code fit pixel->lon/lat and let GDAL/PROJ reproject lon/lat->EPSG:2278 during
  the warp -- the reported RMSE was therefore for a different transform than the
  one used. Now (config.FIT_IN_TARGET_CRS = True, the default) GCPs are assigned
  in EPSG:2278 directly (lon/lat -> 2278 up front) and gdal.Warp's dstSRS is also
  2278, so GCP_POLYNOMIAL/GCP_TPS solves pixel->2278 directly and the reported
  RMSE is exactly that model's error.
* Reports in-sample RMSE, leave-one-out (LOO) RMSE and max for order 1 and
  order 2 polynomials. TPS has no meaningful LOO (an interpolating spline has
  zero residual at every control point by construction); instead we report
  order-2-vs-TPS displacement-field statistics sampled across the extent.
* Reports the fraction of the analysed (cleaned) extent that falls outside the
  convex hull of the GCPs in pixel space -- i.e. how much of the map is
  being *extrapolated* rather than interpolated.
* Produces an additional TPS warp (zones_2278_tps.tif) for sensitivity.

GCP pixel convention
---------------------
The GCP CSV (pixel_x, pixel_y, lon, lat) gives FULL-RESOLUTION source-JPG pixel
coordinates in the ordinary top-down image sense (col, row from the top-left,
row increasing downward) -- NOT the flipped "map_y = -row*scale" convention
used internally by 02_extent.py / 03b_classify_fullres.py for their own
no-CRS pixel-space geotransforms.

zones_px_v2.tif and extent_px.tif were both produced by simple block-downsampling
of the full-res JPG with no flip: raster pixel (c, r) covers source-JPG pixels
(scale*c .. scale*c+scale, scale*r .. scale*r+scale). So a GCP's position in a
scaled raster's own (pixel, line) addressing is simply:

    raster_col = pixel_x / scale
    raster_row = pixel_y / scale

(beware the half-pixel: this is a coordinate, not an index -- we do not round it;
GDAL's GCP pixel/line are fractional raster-space coordinates where integer
values land on pixel CORNERS, matching gdal.GCP's own convention).

Outputs (in OUT_DIR, default: this folder):
    zones_2278.tif        zone classification raster, EPSG:2278, ~10 ft/px, NEAREST (order-2 poly)
    zones_2278_tps.tif     same, thin-plate-spline warp (sensitivity)
    extent_2278.tif        extent mask raster, EPSG:2278, ~40 ft/px, NEAREST
    georef_report.txt      GCP fit RMSE, LOO RMSE, hull fraction, per-GCP residuals
    houston1947.gpkg        layers: extent_1947 (polygon), zones_1947 (polygons by code)

Standalone usage:
    python 05_georef_zones.py
    python 05_georef_zones.py --gcps <csv> --out-dir <dir> --test   (synthetic self-test)
"""
import argparse
import json
import os
import sys

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

import config

DEFAULT_DATA_DIR = config.DATA_DIR
DST_EPSG = config.DST_EPSG

ZONES_RES_FT = config.ZONES_RES_FT
EXTENT_RES_FT = config.EXTENT_RES_FT

DISTRICT_LETTERS = "0ABCDEFGHIJ"  # index 0 = "none" / nodata


def read_gcps(csv_path):
    import csv as _csv
    rows = []
    with open(csv_path, newline="") as f:
        r = _csv.DictReader(f)
        for row in r:
            rows.append({
                "id": row.get("id", str(len(rows))),
                "pixel_x": float(row["pixel_x"]),
                "pixel_y": float(row["pixel_y"]),
                "lon": float(row["lon"]),
                "lat": float(row["lat"]),
            })
    if len(rows) < 3:
        raise RuntimeError(f"Need >=3 GCPs, found {len(rows)} in {csv_path}")
    return rows


def build_gdal_gcps(rows, scale, xy_2278=None):
    """xy_2278: if given, use these (already-projected) coordinates as the GCP's
    georeferenced x/y (config.FIT_IN_TARGET_CRS path). Otherwise use lon/lat."""
    gcps = []
    for i, row in enumerate(rows):
        col = row["pixel_x"] / scale
        line = row["pixel_y"] / scale
        if xy_2278 is not None:
            x, y = xy_2278[i]
            gcps.append(gdal.GCP(x, y, 0.0, col, line, "", str(i)))
        else:
            gcps.append(gdal.GCP(row["lon"], row["lat"], 0.0, col, line, "", str(i)))
    return gcps


def lonlat_to_2278(rows):
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(DST_EPSG)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    out = []
    for row in rows:
        x, y, _ = ct.TransformPoint(row["lon"], row["lat"])
        out.append((x, y))
    return out


def poly_design(u, v, order):
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    if order == 1:
        return np.stack([np.ones_like(u), u, v], axis=1)
    elif order == 2:
        return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)
    else:
        raise ValueError("order must be 1 or 2")


def fit_order(u, v, X, Y, order):
    A = poly_design(u, v, order)
    coefX, *_ = np.linalg.lstsq(A, X, rcond=None)
    coefY, *_ = np.linalg.lstsq(A, Y, rcond=None)
    predX, predY = A @ coefX, A @ coefY
    resid = np.sqrt((predX - X) ** 2 + (predY - Y) ** 2)
    return coefX, coefY, resid


def loo_rmse(u, v, X, Y, order):
    """Leave-one-out residual for each point, refitting on the other N-1 each time."""
    n = len(u)
    A = poly_design(u, v, order)
    resid = np.zeros(n)
    for k in range(n):
        m = np.ones(n, dtype=bool)
        m[k] = False
        cX, *_ = np.linalg.lstsq(A[m], X[m], rcond=None)
        cY, *_ = np.linalg.lstsq(A[m], Y[m], rcond=None)
        px, py = A[k] @ cX, A[k] @ cY
        resid[k] = np.hypot(px - X[k], py - Y[k])
    rmse = float(np.sqrt(np.mean(resid ** 2)))
    return rmse, float(resid.max()), resid


def tps_interp(u, v, X, Y, uq, vq):
    """Thin-plate-spline interpolation of (u,v)->(X,Y) evaluated at (uq,vq), using
    scipy's RBF (thin_plate kernel == GDAL's GCP_TPS)."""
    from scipy.interpolate import RBFInterpolator
    pts = np.stack([u, v], axis=1)
    rbf_x = RBFInterpolator(pts, X, kernel="thin_plate_spline")
    rbf_y = RBFInterpolator(pts, Y, kernel="thin_plate_spline")
    q = np.stack([uq, vq], axis=1)
    return rbf_x(q), rbf_y(q)


def convex_hull_fraction_outside(u, v, ext_mask, gt, scale):
    """Fraction of the cleaned extent mask (in the *zones* raster's own pixel grid,
    pre-georef) that lies outside the convex hull of the GCPs, in that same
    (downsampled) pixel space."""
    from scipy.spatial import ConvexHull, Delaunay
    pts = np.stack([u / scale, v / scale], axis=1)  # GCPs in the raster's own col/line units
    hull = ConvexHull(pts)
    tri = Delaunay(pts[hull.vertices])
    ys, xs = np.nonzero(ext_mask)
    if len(xs) > 200000:
        rng = np.random.default_rng(0)
        idx = rng.choice(len(xs), 200000, replace=False)
        xs, ys = xs[idx], ys[idx]
    inside = tri.find_simplex(np.stack([xs, ys], axis=1)) >= 0
    frac_outside = 1.0 - inside.mean()
    return float(frac_outside), hull.volume  # hull.volume = area in 2D


def assign_gcps_vrt(src_path, gcps, tmp_dir, tag, out_srs):
    vrt_path = os.path.join(tmp_dir, f"{tag}_gcp.vrt")
    opts = gdal.TranslateOptions(format="VRT", GCPs=gcps, outputSRS=out_srs)
    gdal.Translate(vrt_path, src_path, options=opts)
    return vrt_path


def warp_to_2278(vrt_path, out_path, res_ft, resample="near", nodata=0, order=None, method="poly"):
    transformer_opts = []
    if method == "poly":
        transformer_opts = ["SRC_METHOD=GCP_POLYNOMIAL", f"MAX_GCP_ORDER={order}"]
    elif method == "tps":
        transformer_opts = ["SRC_METHOD=GCP_TPS"]
    else:
        raise ValueError(method)
    opts = gdal.WarpOptions(
        dstSRS=f"EPSG:{DST_EPSG}",
        xRes=res_ft,
        yRes=res_ft,
        resampleAlg=resample,
        dstNodata=nodata,
        srcNodata=nodata,
        multithread=True,
        transformerOptions=transformer_opts,
        creationOptions=["COMPRESS=LZW"],
    )
    gdal.Warp(out_path, vrt_path, options=opts)


def polygonize_extent(extent_tif, gpkg_path, simplify_ft=20.0):
    ds = gdal.Open(extent_tif)
    band = ds.GetRasterBand(1)
    mask_band = band.GetMaskBand()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(DST_EPSG)

    mem_drv = ogr.GetDriverByName("Memory")
    mem_ds = mem_drv.CreateDataSource("mem_extent")
    mem_lyr = mem_ds.CreateLayer("extent_tmp", srs=srs, geom_type=ogr.wkbPolygon)
    mem_lyr.CreateField(ogr.FieldDefn("val", ogr.OFTInteger))
    gdal.Polygonize(band, mask_band, mem_lyr, 0, ["8CONNECTED=8"], callback=None)

    best_geom = None
    best_area = -1.0
    for feat in mem_lyr:
        if feat.GetField("val") == 0:
            continue
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        area = geom.GetArea()
        if area > best_area:
            best_area = area
            best_geom = geom.Clone()
    if best_geom is None:
        raise RuntimeError("No extent polygon found after polygonize")

    simplified = best_geom.SimplifyPreserveTopology(simplify_ft)

    gdrv = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        out_ds = ogr.Open(gpkg_path, update=1)
    else:
        out_ds = gdrv.CreateDataSource(gpkg_path)
    if out_ds.GetLayerByName("extent_1947") is not None:
        out_ds.DeleteLayer("extent_1947")
    out_lyr = out_ds.CreateLayer("extent_1947", srs=srs, geom_type=ogr.wkbPolygon)
    out_lyr.CreateField(ogr.FieldDefn("area_ft2", ogr.OFTReal))
    feat = ogr.Feature(out_lyr.GetLayerDefn())
    feat.SetGeometry(simplified)
    feat.SetField("area_ft2", simplified.GetArea())
    out_lyr.CreateFeature(feat)
    out_ds = None
    mem_ds = None
    return simplified.GetArea()


def polygonize_zones(zones_tif, gpkg_path):
    ds = gdal.Open(zones_tif)
    band = ds.GetRasterBand(1)
    mask_band = band.GetMaskBand()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(DST_EPSG)

    mem_drv = ogr.GetDriverByName("Memory")
    mem_ds = mem_drv.CreateDataSource("mem_zones")
    mem_lyr = mem_ds.CreateLayer("zones_tmp", srs=srs, geom_type=ogr.wkbPolygon)
    mem_lyr.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    gdal.Polygonize(band, mask_band, mem_lyr, 0, ["8CONNECTED=8"], callback=None)

    gdrv = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        out_ds = ogr.Open(gpkg_path, update=1)
    else:
        out_ds = gdrv.CreateDataSource(gpkg_path)
    if out_ds.GetLayerByName("zones_1947") is not None:
        out_ds.DeleteLayer("zones_1947")
    out_lyr = out_ds.CreateLayer("zones_1947", srs=srs, geom_type=ogr.wkbMultiPolygon)
    out_lyr.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    out_lyr.CreateField(ogr.FieldDefn("district", ogr.OFTString))

    codes = sorted(set(f.GetField("code") for f in mem_lyr if f.GetField("code") not in (0, None)))
    for code in codes:
        mem_lyr.SetAttributeFilter(f"code = {code}")
        geoms = [f.GetGeometryRef().Clone() for f in mem_lyr if f.GetGeometryRef() is not None]
        mem_lyr.SetAttributeFilter(None)
        if not geoms:
            continue
        union = geoms[0]
        for g in geoms[1:]:
            union = union.Union(g)
        multi = ogr.ForceToMultiPolygon(union)
        feat = ogr.Feature(out_lyr.GetLayerDefn())
        feat.SetGeometry(multi)
        feat.SetField("code", int(code))
        letter = DISTRICT_LETTERS[int(code)] if 0 <= int(code) < len(DISTRICT_LETTERS) else "?"
        feat.SetField("district", letter)
        out_lyr.CreateFeature(feat)
    out_ds = None
    mem_ds = None


def make_synthetic_gcps(path, n=8):
    """Plausible affine for downtown Houston used only for a code-path smoke test."""
    import csv as _csv
    rng = np.random.default_rng(0)
    base_lon, base_lat = -95.37, 29.76
    base_px, base_py = 5300, 3900
    deg_per_px = 0.00006
    rows = []
    for i in range(n):
        dx = rng.uniform(-2500, 2500)
        dy = rng.uniform(-2000, 2000)
        px = base_px + dx
        py = base_py + dy
        lon = base_lon + dx * deg_per_px
        lat = base_lat - dy * deg_per_px  # y inverted (image row down, lat up)
        rows.append({"id": str(i), "pixel_x": px, "pixel_y": py, "lon": lon, "lat": lat})
    with open(path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["id", "pixel_x", "pixel_y", "lon", "lat"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcps", default=None, help="GCP CSV path (default: config.GCPS_FROZEN_CSV)")
    ap.add_argument("--out-dir", default=None, help="output dir (default DEFAULT_DATA_DIR)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="dir with the zones/extent px inputs")
    ap.add_argument("--zones-scale", type=float, default=config.ZONES_SCALE)
    ap.add_argument("--extent-scale", type=float, default=config.EXTENT_SCALE)
    ap.add_argument("--test", action="store_true", help="run a synthetic self-test in a temp dir")
    args = ap.parse_args()

    if args.test:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="houston1947_test_")
        gcp_csv = os.path.join(tmp, "synthetic_gcps.csv")
        make_synthetic_gcps(gcp_csv, n=10)
        out_dir = tmp
        data_dir = args.data_dir
        print(f"[TEST MODE] synthetic GCPs -> {gcp_csv}")
        print(f"[TEST MODE] outputs -> {out_dir}")
    else:
        gcp_csv = args.gcps or config.GCPS_FROZEN_CSV
        out_dir = args.out_dir or args.data_dir
        data_dir = args.data_dir

    zones_src = config.ZONES_PX_TIF if not args.test else os.path.join(data_dir, "zones_px_v2.tif")
    extent_src = config.EXTENT_PX_TIF if not args.test else os.path.join(data_dir, "extent_px.tif")
    if not os.path.exists(zones_src):
        raise RuntimeError(f"missing {zones_src}")
    if not os.path.exists(extent_src):
        raise RuntimeError(f"missing {extent_src}")
    if not os.path.exists(gcp_csv):
        raise RuntimeError(f"missing GCP csv {gcp_csv}")

    os.makedirs(out_dir, exist_ok=True)

    rows = read_gcps(gcp_csv)
    n_gcp = len(rows)
    print(f"Loaded {n_gcp} GCPs from {gcp_csv}")

    xy_2278 = lonlat_to_2278(rows)
    u = np.array([r["pixel_x"] for r in rows], dtype=np.float64)
    v = np.array([r["pixel_y"] for r in rows], dtype=np.float64)
    if config.FIT_IN_TARGET_CRS:
        X = np.array([p[0] for p in xy_2278], dtype=np.float64)
        Y = np.array([p[1] for p in xy_2278], dtype=np.float64)
        fit_space = "EPSG:2278 (pixel -> target CRS directly; matches the warp)"
    else:
        X = np.array([r["lon"] for r in rows], dtype=np.float64)
        Y = np.array([r["lat"] for r in rows], dtype=np.float64)
        fit_space = "lon/lat EPSG:4326 (legacy; GDAL reprojects 4326->2278 after warping)"

    report = [f"GCP file: {gcp_csv}", f"N GCPs: {n_gcp}", f"Fit space: {fit_space}", ""]

    results = {}
    for order in (1, 2):
        coefX, coefY, resid = fit_order(u, v, X, Y, order)
        rmse = float(np.sqrt(np.mean(resid ** 2)))
        lrmse, lmax, lresid = loo_rmse(u, v, X, Y, order)
        results[order] = dict(rmse=rmse, resid=resid, loo_rmse=lrmse, loo_max=lmax, loo_resid=lresid)
        report.append(f"--- Polynomial order {order} ---")
        report.append(f"In-sample RMSE (ft): {rmse:.2f}   max resid: {resid.max():.2f}")
        report.append(f"Leave-one-out RMSE (ft): {lrmse:.2f}   LOO max: {lmax:.2f}")
        order_idx = np.argsort(-lresid)
        report.append("Worst 5 GCPs by LOO residual (id, pixel_x, pixel_y, lon, lat, in-sample_ft, LOO_ft):")
        for i in order_idx[:5]:
            r = rows[i]
            report.append(f"  ({r['id']}, {r['pixel_x']:.1f}, {r['pixel_y']:.1f}, {r['lon']:.6f}, {r['lat']:.6f}) "
                           f"-> in-sample {resid[i]:.2f} ft, LOO {lresid[i]:.2f} ft")
        report.append("")

    report.append("Full per-GCP residual table (order-2, in-sample and LOO):")
    for i, r in enumerate(rows):
        report.append(f"  {r['id']:>3}  px=({r['pixel_x']:.1f},{r['pixel_y']:.1f})  "
                       f"lon/lat=({r['lon']:.6f},{r['lat']:.6f})  "
                       f"in-sample={results[2]['resid'][i]:.2f}ft  LOO={results[2]['loo_resid'][i]:.2f}ft")
    report.append("")

    # TPS vs order-2 displacement field, sampled on a grid across the pixel extent
    report.append("--- Thin-plate spline (TPS) ---")
    report.append("LOO is undefined for TPS (an interpolating spline has zero residual at every "
                   "control point by construction). Instead: order-2-polynomial vs TPS displacement "
                   "field, sampled on a grid across the zones raster extent.")
    gu = np.linspace(u.min() - 500, u.max() + 500, 60)
    gv = np.linspace(v.min() - 500, v.max() + 500, 60)
    guu, gvv = np.meshgrid(gu, gv)
    quu, qvv = guu.ravel(), gvv.ravel()
    A2 = poly_design(quu, qvv, 2)
    coefX2, coefY2, _ = fit_order(u, v, X, Y, 2)
    px2, py2 = A2 @ coefX2, A2 @ coefY2
    tx, ty = tps_interp(u, v, X, Y, quu, qvv)
    disp = np.hypot(px2 - tx, py2 - ty)
    report.append(f"order2-vs-TPS displacement (ft) across sampled grid: "
                  f"mean={disp.mean():.1f}  median={np.median(disp):.1f}  "
                  f"p95={np.percentile(disp, 95):.1f}  max={disp.max():.1f}")
    report.append("")

    # convex hull fraction (need the cleaned extent mask on the ZONES raster's own pixel grid)
    zds_tmp = gdal.Open(zones_src)
    zones_native = zds_tmp.ReadAsArray()
    zds_tmp = None  # close promptly -- don't hold zones_px_* open across the rest of the run
    from scipy import ndimage as _ndi
    ext_src_ds = gdal.Open(extent_src)
    ext_native_full = ext_src_ds.ReadAsArray()
    ext_src_ds = None
    # resample the (possibly different-resolution) extent raster onto the zones grid, nearest
    zoom_y = zones_native.shape[0] / ext_native_full.shape[0]
    zoom_x = zones_native.shape[1] / ext_native_full.shape[1]
    ext_on_zones = _ndi.zoom(ext_native_full, (zoom_y, zoom_x), order=0)
    ext_on_zones = ext_on_zones[:zones_native.shape[0], :zones_native.shape[1]]
    hull_frac, hull_area_px2 = convex_hull_fraction_outside(u, v, ext_on_zones > 0, None, args.zones_scale)
    report.append("--- Convex hull coverage ---")
    report.append(f"Fraction of the cleaned extent OUTSIDE the GCPs' convex hull (pixel space): "
                  f"{100 * hull_frac:.1f}%")
    report.append("")

    # --- independent check-point evaluation (NOT used for fitting) ---
    checkpoints_csv = getattr(config, "CHECKPOINTS_CSV", None)
    if checkpoints_csv and os.path.exists(checkpoints_csv):
        chk_rows = read_gcps(checkpoints_csv)
        cu = np.array([r["pixel_x"] for r in chk_rows], dtype=np.float64)
        cv = np.array([r["pixel_y"] for r in chk_rows], dtype=np.float64)
        if config.FIT_IN_TARGET_CRS:
            cxy = lonlat_to_2278(chk_rows)
            cXt = np.array([p[0] for p in cxy]); cYt = np.array([p[1] for p in cxy])
        else:
            cXt = np.array([r["lon"] for r in chk_rows]); cYt = np.array([r["lat"] for r in chk_rows])
        report.append("--- Independent check-point evaluation (held out of the fit) ---")
        report.append(f"Check-point file: {checkpoints_csv}   N checkpoints: {len(chk_rows)}")
        for order in (1, 2):
            coefX, coefY, _ = fit_order(u, v, X, Y, order)
            A_chk = poly_design(cu, cv, order)
            predX, predY = A_chk @ coefX, A_chk @ coefY
            cresid = np.sqrt((predX - cXt) ** 2 + (predY - cYt) ** 2)
            crmse = float(np.sqrt(np.mean(cresid ** 2)))
            report.append(f"Order {order}: checkpoint RMSE (ft): {crmse:.2f}   max: {cresid.max():.2f}")
            order_idx = np.argsort(-cresid)
            for i in order_idx:
                r = chk_rows[i]
                report.append(f"    {r['id']:>4}  px=({r['pixel_x']:.0f},{r['pixel_y']:.0f})  error={cresid[i]:.2f} ft")
        report.append("")
    else:
        report.append("--- Independent check-point evaluation ---")
        report.append("(config.CHECKPOINTS_CSV not set or file missing -- skipped)")
        report.append("")

    report_path = os.path.join(out_dir, "georef_report.txt")
    with open(report_path, "w") as f:
        f.write("\n".join(report) + "\n")
    print("\n".join(report[:20]))
    print("...")
    print("wrote", report_path)

    # --- warps: order-2 primary, TPS alternative ---
    order = config.GEOREF_ORDER
    out_srs = f"EPSG:{DST_EPSG}" if config.FIT_IN_TARGET_CRS else "EPSG:4326"
    gcp_xy = xy_2278 if config.FIT_IN_TARGET_CRS else None

    gcps_zones = build_gdal_gcps(rows, args.zones_scale, xy_2278=gcp_xy)
    gcps_extent = build_gdal_gcps(rows, args.extent_scale, xy_2278=gcp_xy)

    zones_vrt = assign_gcps_vrt(zones_src, gcps_zones, out_dir, "zones", out_srs)
    extent_vrt = assign_gcps_vrt(extent_src, gcps_extent, out_dir, "extent", out_srs)

    zones_out = os.path.join(out_dir, "zones_2278.tif")
    extent_out = os.path.join(out_dir, "extent_2278.tif")
    warp_to_2278(zones_vrt, zones_out, ZONES_RES_FT, resample="near", nodata=0, order=order, method="poly")
    warp_to_2278(extent_vrt, extent_out, EXTENT_RES_FT, resample="near", nodata=0, order=order, method="poly")
    print("wrote", zones_out)
    print("wrote", extent_out)

    zones_tps_out = os.path.join(out_dir, "zones_2278_tps.tif")
    warp_to_2278(zones_vrt, zones_tps_out, ZONES_RES_FT, resample="near", nodata=0, method="tps")
    print("wrote", zones_tps_out)

    gpkg_path = os.path.join(out_dir, "houston1947.gpkg")
    extent_area = polygonize_extent(extent_out, gpkg_path, simplify_ft=20.0)
    print(f"wrote extent_1947 layer in {gpkg_path} (area {extent_area:.0f} ft2)")
    polygonize_zones(zones_out, gpkg_path)
    print(f"wrote zones_1947 layer in {gpkg_path}")

    print("DONE")


if __name__ == "__main__":
    main()
