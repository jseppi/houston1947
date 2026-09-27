"""Raster overlay of 1947 zones vs 2026 parcel land use (fast replacement for 07_overlay.py).

Burns parcels_2026 (by GROUP_DSCR -- GROUP_CD is ambiguous: code 2 is used for both
Commercial and Multi-Family) onto the 10 ft zones_2278.tif grid, then cross-tabulates
pixels inside the 1947 extent. Writes crosstab_acres.csv, crosstab_pct.csv,
implementation_summary.csv and landuse2026_by_1947group.csv.
"""
import csv, json
import numpy as np
from osgeo import gdal, ogr

gdal.UseExceptions(); ogr.UseExceptions()
GPKG = "houston1947.gpkg"
LU = ["Single-Family Residential", "Multi-Family Residential", "Commercial", "Office",
      "Industrial", "Public & Institutional", "Transportation & Utility", "Park & Open Spaces",
      "Undeveloped", "Agriculture Production", "Unknown"]
DIST = "ABCDEFGHIJ"

cw = json.load(open("crosswalk.json"))
zds = gdal.Open("zones_2278.tif")
zones = zds.ReadAsArray()
gt, proj = zds.GetGeoTransform(), zds.GetProjection()
ny, nx = zones.shape
px_acres = abs(gt[1] * gt[5]) / 43560.0


def burn(layer=None, sql=None, attr=None, value=None):
    ds = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(gt); ds.SetProjection(proj)
    opts = dict(SQLStatement=sql, layers=None if sql else [layer])
    if attr:
        opts["attribute"] = attr
    else:
        opts["burnValues"] = [value]
    gdal.Rasterize(ds, GPKG, **opts)
    return ds.ReadAsArray()


case = " ".join(f"WHEN '{d.replace(chr(39), chr(39) * 2)}' THEN {i + 1}" for i, d in enumerate(LU))
lu = burn(sql=f"SELECT geom, CASE group_dscr {case} ELSE {len(LU)} END AS lu FROM parcels_2026", attr="lu")
ext = burn(layer="extent_1947", value=1)
# The extent polygon also picked up the sheet's neatline frame and stray street lines in the
# blank margins; a 250 ft opening removes thin strips, then keep the largest component.
from scipy import ndimage
yy, xx = np.mgrid[-25:26, -25:26]
ext = ndimage.binary_opening(ext == 1, structure=(xx ** 2 + yy ** 2) <= 625)
# Frame strips touching the city survive the opening; drop areas where <30% of a 2000 ft
# window is classified zone (real city blocks are ~70% classified, the frame is a thin line).
dens = ndimage.uniform_filter((zones > 0).astype(np.float32), size=201)
ext &= dens > 0.3
lab, n = ndimage.label(ext)
ext = (lab == np.argmax(np.bincount(lab.ravel())[1:]) + 1).astype(np.uint8)

inside = ext == 1
# counts[d, l]: d = 0 (unclassified) .. 10 (J); l = 0 (no parcel: ROW/water) .. 11
counts = np.zeros((11, len(LU) + 1), dtype=np.int64)
np.add.at(counts, (zones[inside].astype(np.int64), lu[inside].astype(np.int64)), 1)
acres = counts * px_acres
col = {d: i + 1 for i, d in enumerate(LU)}

groups = {}
for k in DIST:
    groups.setdefault(cw["districts"][k]["group"], []).append(k)

# --- crosstabs ---------------------------------------------------------------
rows = [(f"{k} {cw['districts'][k]['name']}", [DIST.index(k) + 1]) for k in DIST]
rows += [(f"{g} (total)", [DIST.index(k) + 1 for k in ks]) for g, ks in groups.items()]
rows += [("Unclassified in 1947 map (streets/bayous)", [0]), ("All 1947 extent", list(range(11)))]
hdr = ["1947 zone"] + LU + ["No parcel (ROW/water)", "Total"]
with open("crosstab_acres.csv", "w", newline="") as fa, open("crosstab_pct.csv", "w", newline="") as fp:
    wa, wp = csv.writer(fa), csv.writer(fp)
    wa.writerow(hdr); wp.writerow(hdr)
    for name, idx in rows:
        r = acres[idx].sum(0)
        vals = list(r[1:]) + [r[0]]
        tot = r.sum()
        wa.writerow([name] + [f"{v:.1f}" for v in vals] + [f"{tot:.1f}"])
        wp.writerow([name] + [f"{100 * v / tot:.1f}" if tot else "" for v in vals] + ["100.0"])


# --- implementation summary ----------------------------------------------------
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


with open("implementation_summary.csv", "w", newline="") as f:
    w = None
    for label, keys in [(f"{k} {cw['districts'][k]['name']}", [k]) for k in DIST] + \
                       [(f"{g} (total)", ks) for g, ks in groups.items()]:
        s = stats(keys)
        if w is None:
            w = csv.writer(f); w.writerow(["zone"] + list(s))
        w.writerow([label] + [f"{v:.1f}" for v in s.values()])

# --- 2026 perspective: where does each current land use sit in the 1947 plan? --
with open("landuse2026_by_1947group.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["land use 2026", "acres in 1947 extent"] + list(groups) + ["Unclassified"])
    for i, d in enumerate(LU):
        c = acres[:, i + 1]; tot = c.sum()
        parts = [sum(c[DIST.index(k) + 1] for k in ks) for ks in groups.values()] + [c[0]]
        w.writerow([d, f"{tot:.1f}"] + [f"{100 * p / tot:.1f}" if tot else "" for p in parts])

print(f"extent acres {inside.sum() * px_acres:,.0f}; classified-zone acres {acres[1:].sum():,.0f}")
print(open("implementation_summary.csv").read())
