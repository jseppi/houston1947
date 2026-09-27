"""Importable overlay engine for the houston1947 pipeline (replaces the old
07b_overlay_raster.py exec()-of-a-text-slice pattern used by 08/09).

Public API:
    load_grids(zones_tif=None) -> Grids
    burn_parcels(grids, gpkg=None, vacant_as_neutral=None, burn_order=None) -> lu array
    crosstab(grids, lu) -> dict of numpy arrays / helper accessors
    summary_stats(grids, lu, cw=None) -> dict per group/district
    write_csvs(grids, lu, cw, out_prefix="") -> None (crosstab_acres.csv etc, task 1/6 compat)
"""
import json
import os
from dataclasses import dataclass, field

import numpy as np
from osgeo import gdal, ogr
from scipy import ndimage

gdal.UseExceptions()
ogr.UseExceptions()

import config

LU = ["Single-Family Residential", "Multi-Family Residential", "Commercial", "Office",
      "Industrial", "Public & Institutional", "Transportation & Utility", "Park & Open Spaces",
      "Undeveloped", "Agriculture Production", "Unknown"]
DIST = "ABCDEFGHIJ"


@dataclass
class Grids:
    zones: np.ndarray
    ext: np.ndarray          # 0/1 cleaned extent mask
    gt: tuple
    proj: str
    nx: int
    ny: int
    px_acres: float
    zones_tif: str


def _clean_extent(ext_raw, zones):
    """Extent cleanup.
    v2 extent (02b_extent_v2.py) already removes the sheet neatline and applies a
    250 ft opening in scan space, independent of the classifier, so here we only keep
    the largest connected component. The v1 extent needs the legacy 07b cleanup
    (250 ft opening + a density test on classified zones), which is classifier-dependent
    and kept only for the extent-version sensitivity run."""
    ext = ext_raw == 1
    if config.EXTENT_PX_VERSION == "v1":
        yy, xx = np.mgrid[-25:26, -25:26]
        ext = ndimage.binary_opening(ext, structure=(xx ** 2 + yy ** 2) <= 625)
        dens = ndimage.uniform_filter((zones > 0).astype(np.float32), size=201)
        ext &= dens > 0.3
    lab, n = ndimage.label(ext)
    if n == 0:
        raise RuntimeError("extent cleanup produced no components")
    ext = (lab == np.argmax(np.bincount(lab.ravel())[1:]) + 1).astype(np.uint8)
    return ext


def load_grids(zones_tif=None, gpkg=None):
    zones_tif = zones_tif or config.ZONES_2278
    gpkg = gpkg or config.GPKG
    zds = gdal.Open(zones_tif)
    zones = zds.ReadAsArray()
    gt, proj = zds.GetGeoTransform(), zds.GetProjection()
    ny, nx = zones.shape
    px_acres = abs(gt[1] * gt[5]) / 43560.0
    zds = None  # close the source file handle promptly -- don't hold zones_px_* open

    ds = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(gt); ds.SetProjection(proj)
    gdal.Rasterize(ds, gpkg, layers=["extent_1947"], burnValues=[1])
    ext_raw = ds.ReadAsArray()
    ext = _clean_extent(ext_raw, zones)

    return Grids(zones=zones, ext=ext, gt=gt, proj=proj, nx=nx, ny=ny,
                 px_acres=px_acres, zones_tif=zones_tif)


def ensure_parcel_fields(gpkg=None, force=False):
    import prep_parcel_fields
    prep_parcel_fields.main(force=force)


def burn_parcels(grids, gpkg=None, vacant_as_neutral=None, burn_order=None):
    """Burns parcels_2026 to the zones grid, returning an integer array `lu` where:
        0            = no parcel (ROW / water / outside coverage)
        1..len(LU)   = index into LU list (the "combined code": group index, with
                       vacant-as-neutral parcels redirected to the Undeveloped slot
                       when vacant_as_neutral is True)
    Duplicate-geometry clusters are resolved via the burn_group_fwd/burn_group_rev
    fields written by prep_parcel_fields.py (task 4); overlapping-but-distinct
    parcels are resolved by rasterization order, controlled by `burn_order`:
        "neutral_first" (default) -- neutral burnt first, non-neutral burnt on top
        "neutral_last"            -- reverse, for the sensitivity variant
    """
    gpkg = gpkg or config.GPKG
    vacant_as_neutral = config.VACANT_AS_NEUTRAL if vacant_as_neutral is None else vacant_as_neutral
    burn_order = burn_order or config.DEDUPE_BURN_ORDER
    ensure_parcel_fields(gpkg)

    burn_field = "burn_group_fwd" if burn_order == "neutral_first" else "burn_group_rev"
    order_dir = "ASC" if burn_order == "neutral_first" else "DESC"
    # neutral flag: NULL group, Undeveloped/Unknown, or (if enabled) vacant_neutral=1
    vacant_clause = "OR vacant_neutral = 1" if vacant_as_neutral else ""
    case = " ".join(f"WHEN '{d.replace(chr(39), chr(39) * 2)}' THEN {i + 1}" for i, d in enumerate(LU))
    sql = f"""
        SELECT geom,
               CASE
                 WHEN {burn_field} IS NULL THEN {LU.index('Unknown') + 1}
                 WHEN {burn_field} IN ('Undeveloped','Unknown') {vacant_clause} THEN {LU.index('Undeveloped') + 1}
                 ELSE (CASE {burn_field} {case} ELSE {len(LU)} END)
               END AS lu,
               CASE
                 WHEN {burn_field} IS NULL THEN 1
                 WHEN {burn_field} IN ('Undeveloped','Unknown') {vacant_clause} THEN 1
                 ELSE 0
               END AS is_neutral
        FROM parcels_2026
        ORDER BY is_neutral {order_dir}
    """
    ds = gdal.GetDriverByName("MEM").Create("", grids.nx, grids.ny, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(grids.gt); ds.SetProjection(grids.proj)
    gdal.Rasterize(ds, gpkg, SQLStatement=sql, attribute="lu")
    return ds.ReadAsArray()


def load_crosswalk(path=None):
    path = path or config.CROSSWALK_JSON
    return json.load(open(path))


def crosstab(grids, lu):
    """counts[d, l]: d = 0 (unclassified) .. 10 (J); l = 0 (no parcel) .. len(LU)."""
    counts = np.zeros((11, len(LU) + 1), dtype=np.int64)
    inside = grids.ext == 1
    np.add.at(counts, (grids.zones[inside].astype(np.int64), lu[inside].astype(np.int64)), 1)
    acres = counts * grids.px_acres
    return acres


def groups_from_crosswalk(cw):
    groups = {}
    for k in DIST:
        groups.setdefault(cw["districts"][k]["group"], []).append(k)
    return groups


def write_csvs(grids, lu, cw, acres=None, out_prefix=""):
    import csv
    acres = acres if acres is not None else crosstab(grids, lu)
    col = {d: i + 1 for i, d in enumerate(LU)}
    groups = groups_from_crosswalk(cw)

    rows = [(f"{k} {cw['districts'][k]['name']}", [DIST.index(k) + 1]) for k in DIST]
    rows += [(f"{g} (total)", [DIST.index(k) + 1 for k in ks]) for g, ks in groups.items()]
    rows += [("Unclassified in 1947 map (streets/bayous)", [0]), ("All 1947 extent", list(range(11)))]
    hdr = ["1947 zone"] + LU + ["No parcel (ROW/water)", "Total"]
    with open(f"{out_prefix}crosstab_acres.csv", "w", newline="") as fa, \
         open(f"{out_prefix}crosstab_pct.csv", "w", newline="") as fp:
        wa, wp = csv.writer(fa), csv.writer(fp)
        wa.writerow(hdr); wp.writerow(hdr)
        for name, idx in rows:
            r = acres[idx].sum(0)
            vals = list(r[1:]) + [r[0]]
            tot = r.sum()
            wa.writerow([name] + [f"{v:.1f}" for v in vals] + [f"{tot:.1f}"])
            wp.writerow([name] + [f"{100 * v / tot:.1f}" if tot else "" for v in vals] + ["100.0"])

    def stats(keys):
        zone = parcel = neutral = strict = cum = 0.0
        for k in keys:
            r = acres[DIST.index(k) + 1]
            spec = cw["districts"][k]
            zone += r.sum(); parcel += r[1:].sum()
            neutral += sum(r[col[n]] for n in cw["neutral"])
            strict += sum(r[col[n]] for n in spec["strict"])
            cum += sum(r[col[n]] for n in spec["cumulative"])
        denom = parcel - neutral
        return dict(zone_acres=zone, parcel_acres=parcel, row_share_pct=100 * (1 - parcel / zone) if zone else 0,
                    neutral_acres=neutral, neutral_pct=100 * neutral / parcel if parcel else 0, denom_acres=denom,
                    strict_pct=100 * strict / denom if denom else 0, cumulative_pct=100 * cum / denom if denom else 0)

    with open(f"{out_prefix}implementation_summary.csv", "w", newline="") as f:
        w = None
        for label, keys in [(f"{k} {cw['districts'][k]['name']}", [k]) for k in DIST] + \
                           [(f"{g} (total)", ks) for g, ks in groups.items()]:
            s = stats(keys)
            if w is None:
                w = csv.writer(f); w.writerow(["zone"] + list(s))
            w.writerow([label] + [f"{v:.1f}" for v in s.values()])

    with open(f"{out_prefix}landuse2026_by_1947group.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["land use 2026", "acres in 1947 extent"] + list(groups) + ["Unclassified"])
        for i, d in enumerate(LU):
            c = acres[:, i + 1]; tot = c.sum()
            parts = [sum(c[DIST.index(k) + 1] for k in ks) for ks in groups.values()] + [c[0]]
            w.writerow([d, f"{tot:.1f}"] + [f"{100 * p / tot:.1f}" if tot else "" for p in parts])


def landuse_detail_acres(grids, gpkg=None, out_csv="landuse_detail_acres.csv"):
    """Task 3: all distinct (group_dscr, landuse_dscr) pairs with acres inside the extent,
    burned directly from the raw fields (no crosswalk grouping) for auditing."""
    import csv
    gpkg = gpkg or config.GPKG
    ds = ogr.Open(gpkg)
    lyr = ds.GetLayerByName("parcels_2026")
    pairs = []
    seen = {}
    idx = 1
    for feat in lyr:
        g = feat.GetField("group_dscr") or "(null)"
        l = feat.GetField("landuse_dscr") or "(null)"
        key = (g, l)
        if key not in seen:
            seen[key] = idx
            pairs.append(key)
            idx += 1
    ds = None

    n = len(pairs) + 1
    code_ds = gdal.GetDriverByName("MEM").Create("", grids.nx, grids.ny, 1, gdal.GDT_UInt16)
    code_ds.SetGeoTransform(grids.gt); code_ds.SetProjection(grids.proj)
    case = " ".join(
        f"WHEN group_dscr {'IS NULL' if g == '(null)' else '= ' + repr(g).replace(chr(34), chr(39))} "
        f"AND landuse_dscr {'IS NULL' if l == '(null)' else '= ' + repr(l).replace(chr(34), chr(39))} THEN {c}"
        for (g, l), c in seen.items()
    )
    sql = f"SELECT geom, (CASE {case} ELSE 0 END) AS code FROM parcels_2026"
    gdal.Rasterize(code_ds, gpkg, SQLStatement=sql, attribute="code")
    codes = code_ds.ReadAsArray()

    inside = grids.ext == 1
    counts = np.bincount(codes[inside].ravel(), minlength=n)
    acres = counts * grids.px_acres

    rows = sorted(((g, l, acres[c]) for (g, l), c in seen.items()), key=lambda r: (-r[2]))
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group_dscr", "landuse_dscr", "acres_in_extent", "is_vacant_prefix"])
        for g, l, a in rows:
            w.writerow([g, l, f"{a:.2f}", int(bool(l) and l.strip().startswith("Vacant"))])
    return rows
