"""
Step 7: overlay the 1947 zoning districts (zones_1947) with current (2026)
land-use parcels (parcels_2026), clipped to the mapped 1947 extent
(extent_1947), and produce the district/group crosstabs + implementation
summary described in crosswalk.json.

Inputs (houston1947.gpkg, EPSG:2278, US ft):
    zones_1947    (code int, district str)         -- from 05_georef_zones.py
    parcels_2026  (objectid, group_cd, group_dscr,
                   landuse_cd, landuse_dscr)         -- from 06_fetch_parcels.py
    extent_1947   (single polygon)                   -- from 05_georef_zones.py
    crosswalk.json

Outputs (in OUT_DIR):
    layer `overlay` in houston1947.gpkg (district, group, group_dscr, acres) + geometry
    crosstab_acres.csv        rows: A..J + Residential/Commercial/Industrial subtotal + Total
                               cols: each GROUP_DSCR + Total   -- acres
    crosstab_pct.csv           same shape, row-normalized to % of that row's total
    implementation_summary.csv per district AND per group:
        zone_acres, parcel_acres, row_share (=1-parcel_acres/zone_acres),
        neutral_acres, denom (=parcel_acres-neutral_acres),
        strict_pct, cumulative_pct (% of denom), neutral_pct (% of parcel_acres)
    landuse2026_by_1947group.csv   2026-perspective: for each GROUP_DSCR today,
                               share of its (extent-clipped) acres in each 1947 group
                               (Residential/Commercial/Industrial/Unclassified)

Usage:
    python 07_overlay.py                 # real run against houston1947.gpkg
    python 07_overlay.py --test           # synthetic self-test (hand-checkable numbers)
"""
import argparse
import json
import os

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

import shapely
from shapely import wkb as swkb
from shapely.strtree import STRtree
from shapely.prepared import prep as shapely_prep

try:
    from shapely.validation import make_valid as _make_valid
except Exception:
    _make_valid = None

DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GPKG = os.path.join(DEFAULT_DIR, "houston1947.gpkg")
DEFAULT_CROSSWALK = os.path.join(DEFAULT_DIR, "crosswalk.json")
FT2_PER_ACRE = 43560.0
GROUPS = ["Residential", "Commercial", "Industrial"]


def fix_geom(geom):
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        if _make_valid is not None:
            geom = _make_valid(geom)
        else:
            geom = geom.buffer(0)
    return geom


def read_layer(gpkg_path, layer_name, fields):
    ds = ogr.Open(gpkg_path)
    if ds is None:
        raise RuntimeError(f"cannot open {gpkg_path}")
    lyr = ds.GetLayerByName(layer_name)
    if lyr is None:
        raise RuntimeError(f"no layer {layer_name} in {gpkg_path}")
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        geom = swkb.loads(bytes(g.ExportToWkb()))
        attrs = {fld: f.GetField(fld) for fld in fields}
        out.append((geom, attrs))
    ds = None
    return out


def load_crosswalk(path):
    with open(path) as f:
        return json.load(f)


def clip_to_extent(parcels, extent_geom):
    extent_geom = fix_geom(extent_geom)
    prep_extent = shapely_prep(extent_geom)
    out = []
    for geom, attrs in parcels:
        geom = fix_geom(geom)
        if geom is None or geom.is_empty:
            continue
        if not prep_extent.intersects(geom):
            continue
        if prep_extent.contains(geom):
            clipped = geom
        else:
            clipped = geom.intersection(extent_geom)
            if clipped.is_empty:
                continue
        out.append((clipped, attrs))
    return out


def overlay_zones_parcels(zones, parcels):
    """Return list of (district, code, group, group_dscr, acres, geom)."""
    zone_geoms = [fix_geom(g) for g, _ in zones]
    zone_attrs = [a for _, a in zones]
    tree = STRtree(zone_geoms)

    rows = []
    for pgeom, pattrs in parcels:
        idxs = tree.query(pgeom, predicate="intersects")
        for idx in idxs:
            zgeom = zone_geoms[idx]
            inter = zgeom.intersection(pgeom)
            if inter.is_empty:
                continue
            area_ft2 = inter.area
            if area_ft2 <= 0:
                continue
            acres = area_ft2 / FT2_PER_ACRE
            za = zone_attrs[idx]
            rows.append({
                "district": za["district"],
                "code": za["code"],
                "group_dscr": pattrs.get("group_dscr"),
                "acres": acres,
                "geom": inter,
            })
    return rows


def district_group(crosswalk, district):
    return crosswalk["districts"][district]["group"]


def write_overlay_layer(gpkg_path, rows, crosswalk):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2278)
    ds = ogr.Open(gpkg_path, update=1) if os.path.exists(gpkg_path) else \
        ogr.GetDriverByName("GPKG").CreateDataSource(gpkg_path)
    if ds.GetLayerByName("overlay") is not None:
        ds.DeleteLayer("overlay")
    lyr = ds.CreateLayer("overlay", srs, ogr.wkbMultiPolygon)
    lyr.CreateField(ogr.FieldDefn("district", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("group", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("group_dscr", ogr.OFTString))
    fdefn = ogr.FieldDefn("acres", ogr.OFTReal)
    lyr.CreateField(fdefn)
    defn = lyr.GetLayerDefn()
    for r in rows:
        feat = ogr.Feature(defn)
        geom = r["geom"]
        try:
            multi = shapely.geometry.MultiPolygon([geom]) if geom.geom_type == "Polygon" else geom
        except Exception:
            multi = geom
        ogr_geom = ogr.CreateGeometryFromWkb(shapely.to_wkb(multi))
        ogr_geom = ogr.ForceToMultiPolygon(ogr_geom)
        feat.SetGeometry(ogr_geom)
        feat.SetField("district", r["district"])
        feat.SetField("group", district_group(crosswalk, r["district"]))
        feat.SetField("group_dscr", r["group_dscr"] or "")
        feat.SetField("acres", float(r["acres"]))
        lyr.CreateFeature(feat)
    ds = None


def build_crosstab(rows, crosswalk, districts_order):
    group_dscrs = sorted(set((r["group_dscr"] or "") for r in rows))
    acres_by_district = {d: {g: 0.0 for g in group_dscrs} for d in districts_order}
    for r in rows:
        acres_by_district[r["district"]][r["group_dscr"] or ""] += r["acres"]

    group_of = {d: district_group(crosswalk, d) for d in districts_order}

    def row_total(d):
        return sum(acres_by_district[d].values())

    lines_acres = []
    lines_pct = []
    header = ["row"] + group_dscrs + ["Total"]
    lines_acres.append(header)
    lines_pct.append(header)

    group_subtotals = {g: {gd: 0.0 for gd in group_dscrs} for g in GROUPS}
    for d in districts_order:
        vals = [acres_by_district[d][gd] for gd in group_dscrs]
        tot = sum(vals)
        lines_acres.append([d] + [f"{v:.3f}" for v in vals] + [f"{tot:.3f}"])
        if tot > 0:
            lines_pct.append([d] + [f"{100*v/tot:.3f}" for v in vals] + ["100.000"])
        else:
            lines_pct.append([d] + ["0.000"] * len(vals) + ["0.000"])
        g = group_of[d]
        for gd in group_dscrs:
            group_subtotals[g][gd] += acres_by_district[d][gd]

    for g in GROUPS:
        vals = [group_subtotals[g][gd] for gd in group_dscrs]
        tot = sum(vals)
        lines_acres.append([g] + [f"{v:.3f}" for v in vals] + [f"{tot:.3f}"])
        if tot > 0:
            lines_pct.append([g] + [f"{100*v/tot:.3f}" for v in vals] + ["100.000"])
        else:
            lines_pct.append([g] + ["0.000"] * len(vals) + ["0.000"])

    grand = [sum(group_subtotals[g][gd] for g in GROUPS) for gd in group_dscrs]
    grand_tot = sum(grand)
    lines_acres.append(["Total"] + [f"{v:.3f}" for v in grand] + [f"{grand_tot:.3f}"])
    if grand_tot > 0:
        lines_pct.append(["Total"] + [f"{100*v/grand_tot:.3f}" for v in grand] + ["100.000"])
    else:
        lines_pct.append(["Total"] + ["0.000"] * len(grand) + ["0.000"])

    return lines_acres, lines_pct


def write_csv(path, lines):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for line in lines:
            w.writerow(line)


def build_implementation_summary(rows, zones, crosswalk, districts_order, neutral):
    zone_acres_by_district = {}
    for geom, attrs in zones:
        d = attrs["district"]
        zone_acres_by_district[d] = zone_acres_by_district.get(d, 0.0) + fix_geom(geom).area / FT2_PER_ACRE

    acres_by_district_gd = {d: {} for d in districts_order}
    for r in rows:
        gd = r["group_dscr"] or ""
        acres_by_district_gd[r["district"]][gd] = acres_by_district_gd[r["district"]].get(gd, 0.0) + r["acres"]

    def sums_for(district_list, key):
        """key: 'strict' or 'cumulative' -- sum acres over each district's own set."""
        total = 0.0
        for d in district_list:
            wanted = set(crosswalk["districts"][d][key])
            for gd, acres in acres_by_district_gd.get(d, {}).items():
                if gd in wanted:
                    total += acres
        return total

    def neutral_sum(district_list):
        total = 0.0
        for d in district_list:
            for gd, acres in acres_by_district_gd.get(d, {}).items():
                if gd in neutral:
                    total += acres
        return total

    out_rows = []

    def make_row(name, district_list):
        zone_acres = sum(zone_acres_by_district.get(d, 0.0) for d in district_list)
        parcel_acres = sum(sum(acres_by_district_gd.get(d, {}).values()) for d in district_list)
        row_share = (1 - parcel_acres / zone_acres) if zone_acres > 0 else None
        neutral_acres = neutral_sum(district_list)
        denom = parcel_acres - neutral_acres
        strict_acres = sums_for(district_list, "strict")
        cumulative_acres = sums_for(district_list, "cumulative")
        strict_pct = 100 * strict_acres / denom if denom > 0 else None
        cumulative_pct = 100 * cumulative_acres / denom if denom > 0 else None
        neutral_pct = 100 * neutral_acres / parcel_acres if parcel_acres > 0 else None
        out_rows.append({
            "row": name,
            "zone_acres": round(zone_acres, 3),
            "parcel_acres": round(parcel_acres, 3),
            "row_share": round(row_share, 4) if row_share is not None else "",
            "neutral_acres": round(neutral_acres, 3),
            "denom": round(denom, 3),
            "strict_pct": round(strict_pct, 3) if strict_pct is not None else "",
            "cumulative_pct": round(cumulative_pct, 3) if cumulative_pct is not None else "",
            "neutral_pct": round(neutral_pct, 3) if neutral_pct is not None else "",
        })

    for d in districts_order:
        make_row(d, [d])

    group_members = {g: [d for d in districts_order if district_group(crosswalk, d) == g] for g in GROUPS}
    for g in GROUPS:
        make_row(g, group_members[g])

    return out_rows


def build_2026_perspective(rows, parcels_clipped, crosswalk, districts_order):
    group_of = {d: district_group(crosswalk, d) for d in districts_order}
    # total clipped acres per group_dscr (independent of zone overlap)
    total_by_gd = {}
    for geom, attrs in parcels_clipped:
        gd = attrs.get("group_dscr") or ""
        total_by_gd[gd] = total_by_gd.get(gd, 0.0) + fix_geom(geom).area / FT2_PER_ACRE

    by_gd_group = {gd: {g: 0.0 for g in GROUPS} for gd in total_by_gd}
    for r in rows:
        gd = r["group_dscr"] or ""
        g = group_of[r["district"]]
        by_gd_group.setdefault(gd, {gg: 0.0 for gg in GROUPS})
        by_gd_group[gd][g] += r["acres"]

    out_rows = []
    for gd in sorted(total_by_gd):
        total = total_by_gd[gd]
        covered = sum(by_gd_group.get(gd, {}).values())
        unclassified = max(total - covered, 0.0)
        row = {"group_dscr": gd, "total_acres": round(total, 3)}
        for g in GROUPS:
            a = by_gd_group.get(gd, {}).get(g, 0.0)
            row[f"{g}_acres"] = round(a, 3)
            row[f"{g}_pct"] = round(100 * a / total, 3) if total > 0 else ""
        row["Unclassified_acres"] = round(unclassified, 3)
        row["Unclassified_pct"] = round(100 * unclassified / total, 3) if total > 0 else ""
        out_rows.append(row)
    return out_rows


def write_dict_csv(path, rows, fieldnames):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run(gpkg_path, crosswalk_path, out_dir):
    crosswalk = load_crosswalk(crosswalk_path)
    districts_order = list(crosswalk["districts"].keys())
    neutral = set(crosswalk["neutral"])

    zones = read_layer(gpkg_path, "zones_1947", ["code", "district"])
    parcels = read_layer(gpkg_path, "parcels_2026",
                          ["objectid", "group_cd", "group_dscr", "landuse_cd", "landuse_dscr"])
    extent = read_layer(gpkg_path, "extent_1947", [])
    if not extent:
        raise RuntimeError("extent_1947 layer empty")
    extent_geom = extent[0][0]

    print(f"zones features: {len(zones)}  parcels features: {len(parcels)}")
    parcels_clipped = clip_to_extent(parcels, extent_geom)
    print(f"parcels after clip to extent: {len(parcels_clipped)}")

    rows = overlay_zones_parcels(zones, parcels_clipped)
    print(f"overlay intersections: {len(rows)}")

    write_overlay_layer(gpkg_path, rows, crosswalk)
    print(f"wrote overlay layer to {gpkg_path}")

    lines_acres, lines_pct = build_crosstab(rows, crosswalk, districts_order)
    write_csv(os.path.join(out_dir, "crosstab_acres.csv"), lines_acres)
    write_csv(os.path.join(out_dir, "crosstab_pct.csv"), lines_pct)
    print("wrote crosstab_acres.csv / crosstab_pct.csv")

    impl_rows = build_implementation_summary(rows, zones, crosswalk, districts_order, neutral)
    write_dict_csv(os.path.join(out_dir, "implementation_summary.csv"), impl_rows,
                   ["row", "zone_acres", "parcel_acres", "row_share", "neutral_acres",
                    "denom", "strict_pct", "cumulative_pct", "neutral_pct"])
    print("wrote implementation_summary.csv")

    persp_rows = build_2026_perspective(rows, parcels_clipped, crosswalk, districts_order)
    fieldnames = ["group_dscr", "total_acres"] + \
        [f"{g}_{suf}" for g in GROUPS for suf in ("acres", "pct")] + \
        ["Unclassified_acres", "Unclassified_pct"]
    write_dict_csv(os.path.join(out_dir, "landuse2026_by_1947group.csv"), persp_rows, fieldnames)
    print("wrote landuse2026_by_1947group.csv")


def make_synthetic(tmp_dir):
    """Two zone squares (A, E) and parcels overlapping them, with known areas so
    the resulting acreages / percentages can be hand-checked."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2278)
    gpkg = os.path.join(tmp_dir, "synthetic.gpkg")
    if os.path.exists(gpkg):
        os.remove(gpkg)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)

    def box(x0, y0, x1, y1):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        ring.AddPoint(x0, y0); ring.AddPoint(x1, y0); ring.AddPoint(x1, y1); ring.AddPoint(x0, y1); ring.AddPoint(x0, y0)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        return poly

    # zones: A = 0..200 x 0..100 (20000 ft2), E = 200..400 x 0..100 (20000 ft2)
    zl = ds.CreateLayer("zones_1947", srs, ogr.wkbMultiPolygon)
    zl.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    zl.CreateField(ogr.FieldDefn("district", ogr.OFTString))
    for code, district, box_coords in [(1, "A", (0, 0, 200, 100)), (5, "E", (200, 0, 400, 100))]:
        f = ogr.Feature(zl.GetLayerDefn())
        f.SetGeometry(ogr.ForceToMultiPolygon(box(*box_coords)))
        f.SetField("code", code)
        f.SetField("district", district)
        zl.CreateFeature(f)

    # extent = the union bbox, slightly larger: 0..400 x 0..100
    el = ds.CreateLayer("extent_1947", srs, ogr.wkbPolygon)
    ef = ogr.Feature(el.GetLayerDefn())
    ef.SetGeometry(box(0, 0, 400, 100))
    el.CreateFeature(ef)

    # parcels: p1 fully inside A, single-family, 100x100=10000ft2 (half of A)
    #          p2 straddles A/E boundary, commercial, 100x100 centered on x=150..250 -> 5000 in A, 5000 in E
    #          p3 fully inside E, "Undeveloped", 100x100
    #          p4 extends past extent (x 380..480), commercial -- only x 380..400 (20x100=2000ft2) should count after clip
    pl = ds.CreateLayer("parcels_2026", srs, ogr.wkbMultiPolygon)
    for n, t in [("objectid", ogr.OFTInteger), ("group_cd", ogr.OFTInteger), ("group_dscr", ogr.OFTString),
                 ("landuse_cd", ogr.OFTInteger), ("landuse_dscr", ogr.OFTString)]:
        pl.CreateField(ogr.FieldDefn(n, t))
    parcels_def = [
        (1, "Single-Family Residential", (0, 0, 100, 100)),
        (2, "Commercial", (150, 0, 250, 100)),
        (3, "Undeveloped", (300, 0, 400, 100)),
        (4, "Commercial", (380, 0, 480, 100)),
    ]
    for oid, gd, coords in parcels_def:
        f = ogr.Feature(pl.GetLayerDefn())
        f.SetGeometry(ogr.ForceToMultiPolygon(box(*coords)))
        f.SetField("objectid", oid)
        f.SetField("group_cd", 1)
        f.SetField("group_dscr", gd)
        f.SetField("landuse_cd", 1)
        f.SetField("landuse_dscr", gd)
        pl.CreateFeature(f)
    ds = None
    return gpkg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpkg", default=DEFAULT_GPKG)
    ap.add_argument("--crosswalk", default=DEFAULT_CROSSWALK)
    ap.add_argument("--out-dir", default=DEFAULT_DIR)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="houston1947_overlay_test_")
        gpkg = make_synthetic(tmp)
        crosswalk_path = os.path.join(tmp, "crosswalk.json")
        cw = {
            "neutral": ["Undeveloped", "Unknown"],
            "districts": {
                "A": {"name": "First Dwelling", "group": "Residential",
                      "strict": ["Single-Family Residential"],
                      "cumulative": ["Single-Family Residential", "Commercial"]},
                "E": {"name": "Local Business", "group": "Commercial",
                      "strict": ["Commercial"],
                      "cumulative": ["Commercial", "Single-Family Residential"]},
            },
        }
        with open(crosswalk_path, "w") as f:
            json.dump(cw, f)
        print(f"[TEST MODE] synthetic gpkg -> {gpkg}")
        print("[TEST MODE] expected (hand-checked):")
        print("  A: zone_acres=20000/43560=0.4591; parcel overlap = p1(10000 in A) + p2 half in A(5000) = 15000 ft2 = 0.3444 ac")
        print("     A strict (Single-Family) = 10000 ft2 (p1); A cumulative (SF+Commercial) = 15000 ft2 (p1+half p2)")
        print("     neutral in A = 0")
        print("  E: parcel overlap = p2 half in E (5000, Commercial) + p3 in E (10000, Undeveloped) "
              "+ p4 clipped-to-extent-then-intersected-with-E (20x100=2000 ft2, Commercial) = 17000 ft2 = 0.3903 ac")
        print("     E neutral (Undeveloped) = 10000 ft2 = 0.2296 ac; denom = 17000-10000=7000 ft2=0.1607 ac")
        print("     E strict(Commercial)=5000+2000=7000 ft2 -> strict_pct = 7000/7000*100 = 100%")
        run(gpkg, crosswalk_path, tmp)
        print(f"[TEST MODE] outputs written under {tmp} -- inspect implementation_summary.csv / crosstab_acres.csv")
    else:
        run(args.gpkg, args.crosswalk, args.out_dir)


if __name__ == "__main__":
    main()
