"""
Step 10: build the web map tiles + JSON/GeoJSON data files that back the static
GitHub Pages story page (docs/index.html, docs/style.css, docs/app.js -- built by
a concurrent process, NOT touched here).

Standalone, re-runnable:  python 10_web_layers.py

Hosting model: tiles are served as single-file PMTiles (from Cloudflare R2), not
loose z/x/y files. Route per layer: warp to EPSG:3857 at the exact resolution of
its max zoom -> GDAL MBTiles (base zoom) -> gdal Dataset.BuildOverviews (down to
z10) -> pmtiles.convert.mbtiles_to_pmtiles. MBTiles intermediates live in
web_build/ (gitignored, never under docs/).

Produces
--------
docs/tiles/scan.pmtiles                 full-res 1947 scan, georeferenced + warped
                                         to EPSG:3857, z10-16 (webp preferred,
                                         falls back to jpg -- see REPORT).
docs/tiles/zones1947.pmtiles            1947 zoning districts (A..J), RGBA, masked
                                         to the analysis extent, z10-15.
docs/tiles/lu2026.pmtiles               2026 land use (overlay_lib.LU codes), RGBA,
                                         masked to the analysis extent, z10-15.
docs/tiles/agree.pmtiles                per-cell agreement class (strict / cumulative
                                         / mismatch / neutral), RGBA, masked, z10-15.
docs/data/palette.json                  exact palette used by tiles + the page.
docs/data/meta.json                     bounds / tiles{file,format,minzoom,maxzoom} /
                                         timestamp.
docs/data/extent.geojson                analysis-extent outline, EPSG:4326, simplified.
docs/data/lookup.json                   30 m EPSG:3857 click-lookup grid.
docs/data/results.json                  headline numbers straight from the existing
                                         stats/accuracy outputs.
docs/data/legend_swatches/{A..J}.png    96x72 crops of the 1947 legend swatches.

Re-uses (does not re-derive): config.py, 05_georef_zones.py (GCP model),
overlay_lib.py (grids / parcel burn), stats.py (S/Cm/neutral matrices),
crosswalk.json, stats_summary.csv, stats_report.md, accuracy_v3_results.json,
accuracy_v3_report.md, crosstab_acres.csv, distance_decay.csv, extent_meta_v2.json.
"""
import base64
import csv
import importlib
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

from pmtiles.convert import mbtiles_to_pmtiles
from pmtiles.reader import MmapSource, Reader

import config
import overlay_lib as ol
import stats as st

georef = importlib.import_module("05_georef_zones")  # filename starts with a digit

DATA_DIR = config.DATA_DIR
DOCS_DIR = os.path.join(DATA_DIR, "docs")
TILES_DIR = os.path.join(DOCS_DIR, "tiles")
DATA_OUT = os.path.join(DOCS_DIR, "data")
LEGEND_OUT = os.path.join(DATA_OUT, "legend_swatches")
WEB_BUILD_DIR = os.path.join(DATA_DIR, "web_build")  # MBTiles intermediates -- gitignored, not under docs/

FULL_JPG = os.path.join(DATA_DIR, "11139001.jpg")

WEBMERC_C = 156543.03392804097  # Web Mercator meters/pixel at zoom 0, 256px tiles


def res_for_zoom(z, eps=1.0001):
    """Web Mercator meters/pixel for zoom z (256px tiles), nudged very slightly
    coarser (eps>1) than the exact nominal value. Empirically, warping to the
    exact nominal resolution put GDAL's ZOOM_LEVEL_STRATEGY=UPPER one zoom too
    FINE (floating-point landed just inside the next zoom's bucket); this epsilon
    reliably keeps gdal_translate's chosen base zoom equal to z."""
    return WEBMERC_C / (2 ** z) * eps

# Revised per orchestrator feedback: the original groups set failed the dataviz
# skill's CVD/lightness/normal-vision-floor validator (`validate_palette.js`):
# Commercial #D6453D vs Industrial #7B4FA0 -> deltaE 5.4 under deuteranopia
# (needed >=8, floor 6); Residential #E8B04A and the neutral gray #BDBDBD also sat
# outside the OKLCH lightness band. Re-picked to separate commercial (mid-light
# red) from industrial (dark purple) in LIGHTNESS as well as hue, and to deepen
# Residential into a proper yellow-orange -- see the run report for the full
# validator transcript (docs/data/palette_validation.txt).
PALETTE = {
    "lu2026": {
        "Single-Family Residential": "#F2D16B", "Multi-Family Residential": "#E89A3C",
        "Commercial": "#C1443B", "Office": "#E58FB0", "Industrial": "#60398D",
        "Public & Institutional": "#3E7CB1", "Transportation & Utility": "#A48BC0",
        "Park & Open Spaces": "#4E9A5B", "Undeveloped": "#C9C4B8",
        "Agriculture Production": "#B7C98A", "Unknown": "#DDDDDD",
    },
    "zones1947": {
        "A": "#F7E39A", "B": "#F2D16B", "C": "#EDB54F", "D": "#E89A3C", "E": "#E3786E",
        "F": "#C1443B", "G": "#9E2A2B", "H": "#B79FDE", "I": "#A47DD6", "J": "#60398D",
    },
    "agree": {"strict": "#1B9E8F", "cumulative": "#9ED8CE", "mismatch": "#D95F02", "neutral": "#BDBDBD"},
    "groups": {"Residential": "#C2860A", "Commercial": "#C1443B", "Industrial": "#603A99",
               "Public/Park": "#3E7CB1", "Neutral": "#B0B0B0"},
}

LOG_LINES = []


def log(msg):
    print(msg)
    LOG_LINES.append(msg)


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# GCP model (shared by all georeferenced layers)
# ---------------------------------------------------------------------------
def load_gcp_model():
    rows = georef.read_gcps(config.GCPS_FROZEN_CSV)
    xy_2278 = georef.lonlat_to_2278(rows)
    return rows, xy_2278


def build_mbtiles_pyramid(src_tif, out_mbtiles, minzoom, maxzoom, tile_format, overview_resample, quality=None):
    """src_tif must already be warped to EPSG:3857 at res_for_zoom(maxzoom). Writes
    the base (maxzoom) MBTiles level with gdal_translate, then fills zoom levels
    down to minzoom with Dataset.BuildOverviews (the gdaladdo equivalent via the
    GDAL python API). Verifies the resulting zoom range and raises if it doesn't
    match what was requested."""
    co = [f"TILE_FORMAT={tile_format}", "ZOOM_LEVEL_STRATEGY=UPPER"]
    if quality is not None:
        co.append(f"QUALITY={quality}")
    if os.path.exists(out_mbtiles):
        os.remove(out_mbtiles)
    t0 = time.time()
    gdal.Translate(out_mbtiles, src_tif, format="MBTILES", creationOptions=co)

    conn = sqlite3.connect(out_mbtiles)
    base_meta = dict(conn.execute("SELECT name,value FROM metadata").fetchall())
    conn.close()
    base_zoom = int(base_meta["maxzoom"])
    if base_zoom != maxzoom:
        raise RuntimeError(f"{out_mbtiles}: gdal_translate picked base zoom {base_zoom}, "
                            f"expected {maxzoom} (res_for_zoom epsilon may need adjusting)")

    factors = [2 ** k for k in range(1, maxzoom - minzoom + 1)]
    ds = gdal.OpenEx(out_mbtiles, gdal.OF_RASTER | gdal.OF_UPDATE)
    ds.BuildOverviews(overview_resample, factors)
    ds = None

    conn = sqlite3.connect(out_mbtiles)
    meta = dict(conn.execute("SELECT name,value FROM metadata").fetchall())
    zooms = sorted(r[0] for r in conn.execute("SELECT DISTINCT zoom_level FROM tiles"))
    conn.close()
    dt = time.time() - t0
    log(f"MBTiles {os.path.basename(out_mbtiles)} ({dt:.0f}s): minzoom={meta.get('minzoom')} "
        f"maxzoom={meta.get('maxzoom')} zoom_levels={zooms} format={meta.get('format')} "
        f"size={os.path.getsize(out_mbtiles)/1e6:.1f} MB")
    want_zooms = list(range(minzoom, maxzoom + 1))
    if int(meta.get("minzoom", -1)) != minzoom or int(meta.get("maxzoom", -1)) != maxzoom or zooms != want_zooms:
        raise RuntimeError(f"{out_mbtiles}: zoom range check FAILED -- got {zooms} "
                            f"(metadata minzoom={meta.get('minzoom')} maxzoom={meta.get('maxzoom')}), "
                            f"wanted {want_zooms}")
    return meta


def convert_to_pmtiles(mbtiles_path, pmtiles_path, maxzoom):
    mbtiles_to_pmtiles(mbtiles_path, pmtiles_path, maxzoom)
    with open(pmtiles_path, "r+b") as f:
        src = MmapSource(f)
        h = Reader(src).header()
    size_mb = os.path.getsize(pmtiles_path) / 1e6
    log(f"PMTiles {os.path.basename(pmtiles_path)}: min_zoom={h['min_zoom']} max_zoom={h['max_zoom']} "
        f"tile_type={h['tile_type'].name} bounds(lon/lat)=[{h['min_lon_e7']/1e7:.5f},{h['min_lat_e7']/1e7:.5f},"
        f"{h['max_lon_e7']/1e7:.5f},{h['max_lat_e7']/1e7:.5f}]  size={size_mb:.2f} MB")
    return h, size_mb


# ---------------------------------------------------------------------------
# Layer 1: scan -> scan.pmtiles
# ---------------------------------------------------------------------------
def build_scan_tiles(rows, xy_2278, tmp_dir, minzoom=10, maxzoom=16):
    log("\n=== Layer 1: scan.pmtiles ===")
    with open(config.EXTENT_META_V2 if hasattr(config, "EXTENT_META_V2") else
              os.path.join(DATA_DIR, "extent_meta_v2.json")) as f:
        meta = json.load(f)
    b = meta["neatline_detection"]["final_bounds_full_px"]
    left, top, right, bottom = b["left"], b["top"], b["right"], b["bottom"]
    w, h = right - left, bottom - top
    log(f"crop window (full-res px): left={left} top={top} right={right} bottom={bottom} ({w}x{h})")

    gcps_full = georef.build_gdal_gcps(rows, scale=1, xy_2278=xy_2278)
    full_vrt = georef.assign_gcps_vrt(FULL_JPG, gcps_full, tmp_dir, "scan_full", "EPSG:2278")

    crop_vrt = os.path.join(tmp_dir, "scan_crop.vrt")
    gdal.Translate(crop_vrt, full_vrt, srcWin=[left, top, w, h], format="VRT")

    scan_3857 = os.path.join(tmp_dir, "scan_3857.tif")
    r = res_for_zoom(maxzoom)
    warp_opts = gdal.WarpOptions(
        dstSRS="EPSG:3857", resampleAlg="bilinear", dstAlpha=True, multithread=True,
        warpMemoryLimit=512, xRes=r, yRes=r,
        transformerOptions=["SRC_METHOD=GCP_POLYNOMIAL", f"MAX_GCP_ORDER={config.GEOREF_ORDER}"],
        creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
    )
    t0 = time.time()
    gdal.Warp(scan_3857, crop_vrt, options=warp_opts)
    log(f"warped scan to EPSG:3857 @ z{maxzoom} res ({r:.3f} m/px) in {time.time()-t0:.0f}s -> {scan_3857}")

    # choose tile format: prefer WEBP (alpha-capable, quality ~80), fall back to JPEG
    fmt = None
    mbtiles_path = os.path.join(WEB_BUILD_DIR, "scan.mbtiles")
    for driver_name, ext, quality in (("WEBP", "webp", 80), ("JPEG", "jpg", 85)):
        try:
            build_mbtiles_pyramid(scan_3857, mbtiles_path, minzoom, maxzoom, driver_name,
                                   overview_resample="average", quality=quality)
            fmt = ext
            break
        except Exception as e:
            log(f"tiledriver {driver_name} failed ({e}), trying next option")
    if fmt is None:
        raise RuntimeError("no working tiledriver for scan tiles (tried WEBP/JPEG)")

    pmtiles_path = os.path.join(TILES_DIR, "scan.pmtiles")
    header, size_mb = convert_to_pmtiles(mbtiles_path, pmtiles_path, maxzoom)
    return dict(fmt=fmt, pmtiles_path=pmtiles_path, mbtiles_path=mbtiles_path,
                size=os.path.getsize(pmtiles_path), header=header, zmax=maxzoom, zmin=minzoom)


# ---------------------------------------------------------------------------
# RGBA class-raster helper (zones1947 / lu2026 / agree) -> <name>.pmtiles
# ---------------------------------------------------------------------------
def write_rgba_geotiff(path, codes, rgba_by_code, gt, proj):
    ny, nx = codes.shape
    maxcode = int(codes.max())
    lut = np.zeros((maxcode + 1, 4), dtype=np.uint8)
    for code, rgba in rgba_by_code.items():
        if code <= maxcode:
            lut[code] = rgba
    rgba_img = lut[codes]  # (ny, nx, 4)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, nx, ny, 4, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    for b in range(4):
        band = ds.GetRasterBand(b + 1)
        band.WriteArray(rgba_img[:, :, b])
    ds.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    ds.FlushCache()
    ds = None


def warp_class_raster_to_3857(src_tif, out_tif, maxzoom):
    r = res_for_zoom(maxzoom)
    opts = gdal.WarpOptions(
        dstSRS="EPSG:3857", resampleAlg="near", multithread=True, xRes=r, yRes=r,
        creationOptions=["TILED=YES", "COMPRESS=DEFLATE"],
    )
    gdal.Warp(out_tif, src_tif, options=opts)


def build_class_layer(name, codes, rgba_by_code, gt, proj, tmp_dir, minzoom=10, maxzoom=15):
    log(f"\n=== Layer: {name}.pmtiles ===")
    src = os.path.join(tmp_dir, f"{name}_2278.tif")
    write_rgba_geotiff(src, codes, rgba_by_code, gt, proj)
    dst = os.path.join(tmp_dir, f"{name}_3857.tif")
    warp_class_raster_to_3857(src, dst, maxzoom)

    mbtiles_path = os.path.join(WEB_BUILD_DIR, f"{name}.mbtiles")
    build_mbtiles_pyramid(dst, mbtiles_path, minzoom, maxzoom, "PNG8", overview_resample="nearest")

    pmtiles_path = os.path.join(TILES_DIR, f"{name}.pmtiles")
    header, size_mb = convert_to_pmtiles(mbtiles_path, pmtiles_path, maxzoom)
    return dict(fmt="png", pmtiles_path=pmtiles_path, mbtiles_path=mbtiles_path,
                size=os.path.getsize(pmtiles_path), header=header, zmax=maxzoom, zmin=minzoom)


# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------
DIST = ol.DIST
LU = ol.LU


def build_zones_rgba(grids):
    zones_masked = (grids.zones * grids.ext).astype(np.uint8)
    rgba_by_code = {0: (0, 0, 0, 0)}
    for i, letter in enumerate(DIST):
        rgba_by_code[i + 1] = hex_to_rgb(PALETTE["zones1947"][letter]) + (255,)
    return zones_masked, rgba_by_code


def build_lu_rgba(grids, lu):
    lu_masked = (lu * grids.ext).astype(np.uint8)
    rgba_by_code = {0: (0, 0, 0, 0)}
    for i, name in enumerate(LU):
        rgba_by_code[i + 1] = hex_to_rgb(PALETTE["lu2026"][name]) + (255,)
    return lu_masked, rgba_by_code


def build_agree_rgba(grids, lu, S, Cm, neutral):
    zones_masked = (grids.zones * grids.ext).astype(np.int64)
    lu_masked = (lu * grids.ext).astype(np.int64)
    n_lu = len(LU)
    lut = np.zeros(11 * (n_lu + 1), dtype=np.uint8)
    for z in range(11):
        for l in range(n_lu + 1):
            if z == 0 or l == 0:
                cat = 0
            elif neutral[l]:
                cat = 4
            elif S[z, l]:
                cat = 1
            elif Cm[z, l]:
                cat = 2
            else:
                cat = 3
            lut[z * (n_lu + 1) + l] = cat
    key = zones_masked * (n_lu + 1) + lu_masked
    agree_codes = lut[key].astype(np.uint8)
    rgba_by_code = {
        0: (0, 0, 0, 0),
        1: hex_to_rgb(PALETTE["agree"]["strict"]) + (255,),
        2: hex_to_rgb(PALETTE["agree"]["cumulative"]) + (255,),
        3: hex_to_rgb(PALETTE["agree"]["mismatch"]) + (255,),
        4: hex_to_rgb(PALETTE["agree"]["neutral"]) + (255,),
    }
    return agree_codes, rgba_by_code


def reproj_point(x, y, src_epsg, dst_epsg):
    src = osr.SpatialReference(); src.ImportFromEPSG(src_epsg); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(dst_epsg); dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    X, Y, _ = ct.TransformPoint(x, y)
    return X, Y


def write_extent_geojson(gpkg_path, out_path, simplify_m=20.0):
    ds = ogr.Open(gpkg_path)
    lyr = ds.GetLayerByName("extent_1947")
    feat = lyr.GetNextFeature()
    geom = feat.GetGeometryRef().Clone()
    # ft -> simplify in source units (EPSG:2278 is US survey feet)
    simplify_ft = simplify_m * 3.28084
    geom = geom.SimplifyPreserveTopology(simplify_ft)
    src = osr.SpatialReference(); src.ImportFromWkt(lyr.GetSpatialRef().ExportToWkt())
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(4326); dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    geom.Transform(ct)
    geojson = json.loads(geom.ExportToJson())
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geojson}]}
    with open(out_path, "w") as f:
        json.dump(fc, f)
    ds = None
    env = geom.GetEnvelope()  # minx,maxx,miny,maxy in lon/lat
    size_kb = os.path.getsize(out_path) / 1024
    log(f"extent.geojson: {size_kb:.1f} KB, bounds lon/lat = "
        f"[{env[0]:.5f},{env[2]:.5f},{env[1]:.5f},{env[3]:.5f}]")
    return env  # (minx, maxx, miny, maxy)


def mode_downsample(arr, block):
    """Majority (mode) value within non-overlapping block x block windows.
    Windows the array to bound memory; arr must already be small (this pipeline's
    zones grid is ~7300x5800 -> ~42M cells, well within budget)."""
    ny, nx = arr.shape
    ny2 = (ny // block) * block
    nx2 = (nx // block) * block
    arr = arr[:ny2, :nx2]
    nby, nbx = ny2 // block, nx2 // block
    out = np.zeros((nby, nbx), dtype=arr.dtype)
    # bincount per block via reshape + apply_along_axis-free loop over small block count
    reshaped = arr.reshape(nby, block, nbx, block).swapaxes(1, 2).reshape(nby, nbx, block * block)
    maxval = int(arr.max()) + 1
    for v in range(maxval):
        cnt = (reshaped == v).sum(axis=2)
        if v == 0:
            best = cnt.copy()
            out[:] = 0
        else:
            better = cnt > best
            out[better] = v
            best[better] = cnt[better]
    return out


def build_lookup_json(grids, lu, cw, S, Cm, neutral, extent_bbox_2278, cell=30.0):
    log("\n=== lookup.json ===")
    # target grid in EPSG:3857 covering the extent bbox (reprojected)
    minx2278, miny2278, maxx2278, maxy2278 = extent_bbox_2278
    corners = [(minx2278, miny2278), (minx2278, maxy2278), (maxx2278, miny2278), (maxx2278, maxy2278)]
    xs3857, ys3857 = [], []
    for x, y in corners:
        X, Y = reproj_point(x, y, 2278, 3857)
        xs3857.append(X); ys3857.append(Y)
    x0, x1 = min(xs3857), max(xs3857)
    y0, y1 = max(ys3857), min(ys3857)  # y0 = top(north), y1 = bottom(south) in 3857 metres
    nx = int(np.ceil((x1 - x0) / cell))
    ny = int(np.ceil((y0 - y1) / cell))
    log(f"lookup grid: nx={nx} ny={ny} cell={cell}m  x0={x0:.1f} y0={y0:.1f}")

    zones_masked = (grids.zones * grids.ext).astype(np.uint8)
    lu_masked = (lu * grids.ext).astype(np.uint8)

    def make_src(arr, path):
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte)
        ds.SetGeoTransform(grids.gt); ds.SetProjection(grids.proj)
        ds.GetRasterBand(1).WriteArray(arr)
        ds.FlushCache(); ds = None

    with tempfile.TemporaryDirectory() as tmp:
        z_src = os.path.join(tmp, "z.tif"); l_src = os.path.join(tmp, "l.tif")
        make_src(zones_masked, z_src); make_src(lu_masked, l_src)
        z_dst = os.path.join(tmp, "z3857.tif"); l_dst = os.path.join(tmp, "l3857.tif")
        opts = gdal.WarpOptions(dstSRS="EPSG:3857", resampleAlg="mode",
                                 outputBounds=(x0, y1, x1, y0), width=nx, height=ny)
        gdal.Warp(z_dst, z_src, options=opts)
        gdal.Warp(l_dst, l_src, options=opts)
        zgrid = gdal.Open(z_dst).ReadAsArray().astype(np.uint8)
        lgrid = gdal.Open(l_dst).ReadAsArray().astype(np.uint8)

    zone_b64 = base64.b64encode(zgrid.tobytes()).decode("ascii")
    lu_b64 = base64.b64encode(lgrid.tobytes()).decode("ascii")

    zone_names = {k: cw["districts"][k]["name"] for k in DIST}
    col = {name: i + 1 for i, name in enumerate(LU)}
    strict = {k: [col[n] for n in cw["districts"][k]["strict"]] for k in DIST}
    cumulative = {k: [col[n] for n in cw["districts"][k]["cumulative"]] for k in DIST}
    neutral_codes = [col[n] for n in cw["neutral"]]

    out = dict(x0=x0, y0=y0, cell=cell, nx=nx, ny=ny, zones=zone_b64, lu=lu_b64,
               zone_names=zone_names, lu_names=LU, strict=strict, cumulative=cumulative,
               neutral=neutral_codes,
               note="zone/lu are base64 row-major uint8 arrays sampled by majority (mode) "
                    "within each 30 m EPSG:3857 cell; zone codes 0=none,1..10=A..J; "
                    "lu codes 0=no parcel,1..11=lu_names[code-1].")
    path = os.path.join(DATA_OUT, "lookup.json")
    with open(path, "w") as f:
        json.dump(out, f)
    size_mb = os.path.getsize(path) / 1e6
    log(f"lookup.json: {size_mb:.2f} MB")
    return dict(x0=x0, y0=y0, x1=x1, y1=y1, size_mb=size_mb)


def build_legend_swatches(cw):
    log("\n=== legend swatches ===")
    ds = gdal.Open(FULL_JPG)
    sw = 96, 72
    # crosswalk.json's legend_swatch_px also carries a "_note" string entry alongside
    # the A..J (cx,cy) pixel pairs -- only emit swatches for the real district letters.
    swatches = {k: v for k, v in cw["legend_swatch_px"].items() if k in DIST}
    for letter, (cx, cy) in swatches.items():
        left = int(cx - sw[0] / 2)
        top = int(cy - sw[1] / 2)
        out = os.path.join(LEGEND_OUT, f"{letter}.png")
        gdal.Translate(out, ds, srcWin=[left, top, sw[0], sw[1]], format="PNG")
        # gdal leaves a .aux.xml sidecar for PNG; remove it (not needed by the page)
        aux = out + ".aux.xml"
        if os.path.exists(aux):
            os.remove(aux)
    ds = None
    log(f"wrote {len(swatches)} legend swatch PNGs to {LEGEND_OUT}")


def parse_markdown_table(md_text, header_contains):
    """Dict-per-row parse. NOTE: unsafe for tables with duplicate column names
    (e.g. two "95% CI" or "±" columns) since dict(zip(...)) silently keeps only
    the last one -- use extract_table_rows() (positional) for those instead."""
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and header_contains in line:
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows = []
            j = i + 2  # skip the |---|---| separator row
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                rows.append(dict(zip(header, cells)))
                j += 1
            return header, rows
    raise RuntimeError(f"table with header containing {header_contains!r} not found")


def extract_table_rows(md_text, header_contains):
    """Positional (not dict) parse: returns (header_cells, [row_cells, ...]). Safe
    for markdown tables with duplicate column headers."""
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and header_contains in line:
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                rows.append(cells)
                j += 1
            return header, rows
    raise RuntimeError(f"table with header containing {header_contains!r} not found")


def pct(s):
    return float(s.replace("%", "").strip())


def build_results_json(cw):
    log("\n=== results.json ===")
    with open(os.path.join(DATA_DIR, "stats_summary.csv")) as f:
        summary_rows = list(csv.DictReader(f))
    summary_by_key = {r["group"]: r for r in summary_rows}

    acc = json.load(open(os.path.join(DATA_DIR, "accuracy_v3_results.json")))
    acc_report = open(os.path.join(DATA_DIR, "accuracy_v3_report.md"), encoding="utf-8").read()

    # "true" (reference-corrected) strict/cumulative % + CI, parsed from the report's
    # match-rate table (more reliable than re-deriving CIs from SEs by hand). This
    # table has two "95% CI" columns (strict's and cumulative's) so it must be
    # parsed positionally, not as a header->cell dict.
    # columns: 1947 group | strict (true) | 95% CI | cumulative (true) | 95% CI | n points | strict (mapped labels...)
    _, match_rows_pos = extract_table_rows(acc_report, "strict (true)")
    match_by_group = {r[0]: dict(strict_true=r[1], strict_ci=r[2], cum_true=r[3], cum_ci=r[4]) for r in match_rows_pos}

    def parse_ci(cell):
        m = re.match(r"\[([\d.]+),\s*([\d.]+)\]", cell)
        return [round(float(m.group(1)), 1), round(float(m.group(2)), 1)]

    groups_out = []
    for g in ("Residential", "Commercial", "Industrial"):
        s = summary_by_key[g]
        mr = match_by_group[g]
        groups_out.append(dict(
            group=g,
            denom_acres=round(float(s["denom_parcel_acres_excl_neutral"]), 1),
            strict=round(float(s["observed_strict_pct"]), 1),
            strict_lo=round(float(s["bootstrap_strict_ci_lo"]), 1),
            strict_hi=round(float(s["bootstrap_strict_ci_hi"]), 1),
            cumulative=round(float(s["observed_cumulative_pct"]), 1),
            cum_lo=round(float(s["bootstrap_cumulative_ci_lo"]), 1),
            cum_hi=round(float(s["bootstrap_cumulative_ci_hi"]), 1),
            baseline=round(float(s["marginal_baseline_strict_pct"]), 1),
            baseline_cum=round(float(s["marginal_baseline_cumulative_pct"]), 1),
            index=round(float(s["chance_corrected_index_strict"]), 3),
            null_mean=round(float(s["null_mean_strict_pct"]), 1),
            null_p95=round(float(s["null_p95_strict_pct"]), 1),
            corrected=pct(mr["strict_true"]),
            corrected_lo=parse_ci(mr["strict_ci"])[0],
            corrected_hi=parse_ci(mr["strict_ci"])[1],
            corrected_cum=pct(mr["cum_true"]),
        ))

    districts_out = []
    for k in DIST:
        s = summary_by_key[f"district {k}"]
        districts_out.append(dict(
            code=k, name=cw["districts"][k]["name"], group=cw["districts"][k]["group"],
            strict=round(float(s["observed_strict_pct"]), 1),
            cumulative=round(float(s["observed_cumulative_pct"]), 1),
            acres=round(float(s["denom_parcel_acres_excl_neutral"]), 1),
        ))

    # flows: 1947 group -> 2026 group acres, from crosstab_acres.csv (parcel land only,
    # i.e. excluding the "No parcel (ROW/water)" column), grouped per stats.py's
    # off_diagonal_shares() land-use grouping (+ a Neutral catch-all).
    with open(os.path.join(DATA_DIR, "crosstab_acres.csv")) as f:
        ctrows = list(csv.DictReader(f))
    ct_by_name = {r["1947 zone"]: r for r in ctrows}
    lu2026_group = {
        "Residential": ["Single-Family Residential", "Multi-Family Residential"],
        "Commercial": ["Commercial", "Office"],
        "Industrial": ["Industrial", "Transportation & Utility"],
        "Public/Park": ["Public & Institutional", "Park & Open Spaces"],
        "Neutral": ["Undeveloped", "Agriculture Production", "Unknown"],
    }
    flows_out = []
    for g in ("Residential", "Commercial", "Industrial"):
        row = ct_by_name[f"{g} (total)"]
        for to_g, cols in lu2026_group.items():
            acres = sum(float(row[c]) for c in cols)
            flows_out.append(dict(**{"from": g, "to": to_g, "acres": round(acres, 1)}))

    with open(os.path.join(DATA_DIR, "distance_decay.csv")) as f:
        decay_rows = list(csv.DictReader(f))
    decay_out = []
    for r in decay_rows:
        decay_out.append(dict(
            shift_ft=round(float(r["shift_ft"]), 1),
            Residential=round(float(r["Residential_strict_pct_mean"]), 1),
            Commercial=round(float(r["Commercial_strict_pct_mean"]), 1),
            Industrial=round(float(r["Industrial_strict_pct_mean"]), 1),
        ))

    # accuracy: overall + per-group user's/producer's accuracy w/ CI, parsed from
    # accuracy_v3_report.md's error-matrix table.
    overall_ci = [round((acc["OA"] - 1.96 * acc["OA_se"]) * 100, 1),
                  round((acc["OA"] + 1.96 * acc["OA_se"]) * 100, 1)]

    # user's/producer's accuracy table has two "±" columns, so it must be parsed
    # positionally: | group | user's acc. | ± | producer's acc. | ± | mapped share | error-adjusted share | ± |
    code_to_group = {"R": "Residential", "C": "Commercial", "I": "Industrial"}
    _, ua_pa_rows_pos = extract_table_rows(acc_report, "user's acc.")
    accuracy_groups = []
    for cells in ua_pa_rows_pos:
        code = cells[0]
        if code not in code_to_group:
            continue
        users, users_pm = float(cells[1]), float(cells[2])
        producers, producers_pm = float(cells[3]), float(cells[4])
        accuracy_groups.append(dict(
            group=code_to_group[code],
            users=round(users * 100, 1),
            users_ci=[round((users - users_pm) * 100, 1), round((users + users_pm) * 100, 1)],
            producers=round(producers * 100, 1),
            producers_ci=[round((producers - producers_pm) * 100, 1), round((producers + producers_pm) * 100, 1)],
        ))

    accuracy_out = dict(overall=round(acc["OA"] * 100, 1), overall_ci=overall_ci, groups=accuracy_groups)

    # sensitivity: parsed straight from stats_report.md's sensitivity table
    stats_report_md = open(os.path.join(DATA_DIR, "stats_report.md"), encoding="utf-8").read()
    header, sens_rows = parse_markdown_table(stats_report_md, "Commercial_strict_pct")
    sensitivity_out = []
    for r in sens_rows:
        try:
            sensitivity_out.append(dict(
                variant=r["variant"],
                Residential=float(r["Residential_strict_pct"]) if r["Residential_strict_pct"] else None,
                Commercial=float(r["Commercial_strict_pct"]) if r["Commercial_strict_pct"] else None,
                Industrial=float(r["Industrial_strict_pct"]) if r["Industrial_strict_pct"] else None,
            ))
        except (KeyError, ValueError):
            sensitivity_out.append(dict(variant=r.get("variant", ""), note=r.get("note", "")))

    out = dict(groups=groups_out, districts=districts_out, flows=flows_out, decay=decay_out,
               accuracy=accuracy_out, sensitivity=sensitivity_out)
    path = os.path.join(DATA_OUT, "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    log(f"results.json written ({os.path.getsize(path)} bytes)")

    # cross-check a couple of headline numbers against stats_report.md's own table
    assert abs(groups_out[0]["strict"] - 57.8) < 0.05, "Residential strict %% mismatch vs stats_report.md"
    assert abs(groups_out[1]["strict"] - 37.7) < 0.05, "Commercial strict %% mismatch vs stats_report.md"
    assert abs(groups_out[2]["strict"] - 52.1) < 0.05, "Industrial strict %% mismatch vs stats_report.md"
    log("cross-check vs stats_report.md OK (Residential/Commercial/Industrial strict %% match)")
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default="scan,zones1947,lu2026,agree",
                     help="comma-separated subset of {scan,zones1947,lu2026,agree} tile layers to "
                          "(re)build. The scan warp+tiling is by far the slowest step and is "
                          "unaffected by the class-raster palette, so e.g. after a palette-only "
                          "change: --layers zones1947,lu2026")
    args = ap.parse_args()
    want = set(args.layers.split(","))

    os.makedirs(TILES_DIR, exist_ok=True)
    os.makedirs(DATA_OUT, exist_ok=True)
    os.makedirs(LEGEND_OUT, exist_ok=True)

    cw = ol.load_crosswalk()
    rows, xy_2278 = load_gcp_model()

    # --- palette.json (exact) ---
    with open(os.path.join(DATA_OUT, "palette.json"), "w") as f:
        json.dump(PALETTE, f, indent=1)
    log("wrote docs/data/palette.json")
    log("note: overlay_lib.burn_parcels() already redirects vacant parcels (and any "
        "null/Undeveloped/Unknown group, per config.VACANT_AS_NEUTRAL) into the "
        "'Undeveloped' LU slot (LU index 9) -- no separate vacant color is needed, "
        "it uses palette.lu2026.Undeveloped.")

    # --- grids / parcel burn (shared by zones1947, lu2026, agree, lookup.json) ---
    log("\nLoading grids + burning parcels...")
    grids = ol.load_grids()
    lu = ol.burn_parcels(grids)
    S, Cm, neutral = st.build_matrices(cw)

    def skipped_layer(name, default_fmt, default_zmin, default_zmax):
        """Reopen an already-built docs/tiles/<name>.pmtiles (not in --layers this
        run) and read its header back, instead of rebuilding it."""
        pmtiles_path = os.path.join(TILES_DIR, f"{name}.pmtiles")
        log(f"\n=== Layer: {name}.pmtiles (skipped -- not in --layers, reusing existing file) ===")
        if not os.path.exists(pmtiles_path):
            raise RuntimeError(f"--layers omitted '{name}' but {pmtiles_path} does not exist yet -- "
                                f"run with --layers including '{name}' at least once first")
        with open(pmtiles_path, "r+b") as f:
            h = Reader(MmapSource(f)).header()
        size = os.path.getsize(pmtiles_path)
        log(f"{name}.pmtiles (unchanged): min_zoom={h['min_zoom']} max_zoom={h['max_zoom']} "
            f"tile_type={h['tile_type'].name}  size={size/1e6:.1f} MB")
        fmt = {"WEBP": "webp", "JPEG": "jpg", "PNG": "png"}.get(h["tile_type"].name, default_fmt)
        return dict(fmt=fmt, pmtiles_path=pmtiles_path, size=size, header=h,
                    zmin=h.get("min_zoom", default_zmin), zmax=h.get("max_zoom", default_zmax))

    os.makedirs(WEB_BUILD_DIR, exist_ok=True)
    tile_info = {}
    with tempfile.TemporaryDirectory(prefix="h1947_web_") as tmp_dir:
        if "scan" in want:
            tile_info["scan"] = build_scan_tiles(rows, xy_2278, tmp_dir)
        else:
            tile_info["scan"] = skipped_layer("scan", "webp", 10, 16)

        if "zones1947" in want:
            zones_codes, zones_rgba = build_zones_rgba(grids)
            tile_info["zones1947"] = build_class_layer("zones1947", zones_codes, zones_rgba, grids.gt, grids.proj, tmp_dir)
        else:
            tile_info["zones1947"] = skipped_layer("zones1947", "png", 10, 15)

        if "lu2026" in want:
            lu_codes, lu_rgba = build_lu_rgba(grids, lu)
            tile_info["lu2026"] = build_class_layer("lu2026", lu_codes, lu_rgba, grids.gt, grids.proj, tmp_dir)
        else:
            tile_info["lu2026"] = skipped_layer("lu2026", "png", 10, 15)

        if "agree" in want:
            agree_codes, agree_rgba = build_agree_rgba(grids, lu, S, Cm, neutral)
            tile_info["agree"] = build_class_layer("agree", agree_codes, agree_rgba, grids.gt, grids.proj, tmp_dir)
        else:
            tile_info["agree"] = skipped_layer("agree", "png", 10, 15)

        # --- total size check / cap: rebuild scan at maxzoom=15 if over ~150 MB ---
        total_sz = sum(ti["size"] for ti in tile_info.values())
        log(f"\ndocs/tiles/*.pmtiles total: {total_sz/1e6:.1f} MB")
        if total_sz / 1e6 > 150 and tile_info["scan"]["zmax"] == 16 and "scan" in want:
            log("docs/tiles exceeded ~150 MB cap: rebuilding scan.pmtiles capped at z15")
            tile_info["scan"] = build_scan_tiles(rows, xy_2278, tmp_dir, minzoom=10, maxzoom=15)
            total_sz = sum(ti["size"] for ti in tile_info.values())
            log(f"docs/tiles/*.pmtiles total after cap: {total_sz/1e6:.1f} MB")

    # --- extent.geojson ---
    lon_lat_bounds = write_extent_geojson(config.GPKG, os.path.join(DATA_OUT, "extent.geojson"))

    # extent bbox in EPSG:2278 (for lookup.json's 3857 grid + meta.json extent_bounds)
    ds = ogr.Open(config.GPKG)
    lyr = ds.GetLayerByName("extent_1947")
    feat = lyr.GetNextFeature()
    minx, maxx, miny, maxy = feat.GetGeometryRef().GetEnvelope()
    extent_bbox_2278 = (minx, miny, maxx, maxy)
    ds = None

    # --- lookup.json ---
    lookup_info = build_lookup_json(grids, lu, cw, S, Cm, neutral, extent_bbox_2278)

    # --- legend swatches ---
    build_legend_swatches(cw)

    # --- results.json ---
    build_results_json(cw)

    # --- meta.json ---
    # Hosting moved to per-layer PMTiles served from Cloudflare R2 (not loose z/x/y
    # files under docs/tiles/), so tile_formats/zooms are replaced by one "tiles"
    # object per layer: {file, format, minzoom, maxzoom}. Bounds/zooms come straight
    # from each PMTiles file's own header (verified when it was written).
    scan = tile_info["scan"]
    scan_h = scan["header"]
    scan_bounds = [round(scan_h["min_lon_e7"] / 1e7, 6), round(scan_h["min_lat_e7"] / 1e7, 6),
                   round(scan_h["max_lon_e7"] / 1e7, 6), round(scan_h["max_lat_e7"] / 1e7, 6)]
    center_lon = (lon_lat_bounds[0] + lon_lat_bounds[1]) / 2
    center_lat = (lon_lat_bounds[2] + lon_lat_bounds[3]) / 2
    meta = dict(
        scan_bounds=scan_bounds,
        extent_bounds=[round(lon_lat_bounds[0], 6), round(lon_lat_bounds[2], 6),
                       round(lon_lat_bounds[1], 6), round(lon_lat_bounds[3], 6)],
        center=[round(center_lon, 6), round(center_lat, 6)],
        tiles={name: dict(file=f"{name}.pmtiles", format=tile_info[name]["fmt"],
                           minzoom=tile_info[name]["zmin"], maxzoom=tile_info[name]["zmax"])
               for name in ("scan", "zones1947", "lu2026", "agree")},
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with open(os.path.join(DATA_OUT, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    log(f"wrote docs/data/meta.json: {meta}")

    # --- final report ---
    total_sz = sum(ti["size"] for ti in tile_info.values())
    log("\n=== SUMMARY ===")
    for name in ("scan", "zones1947", "lu2026", "agree"):
        ti = tile_info[name]
        log(f"{name + '.pmtiles':20s}  z{ti['zmin']}-{ti['zmax']}  {ti['fmt']:5s}  {ti['size']/1e6:8.2f} MB")
    log(f"{'TOTAL':20s}  {'':17s}  {total_sz/1e6:8.2f} MB")
    log(f"lookup.json size: {lookup_info['size_mb']:.2f} MB")

    with open(os.path.join(DATA_DIR, "web_layers_report.txt"), "w") as f:
        f.write("\n".join(LOG_LINES) + "\n")
    log("\nwrote web_layers_report.txt")


if __name__ == "__main__":
    main()
