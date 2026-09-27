"""DEPRECATED (task 5): superseded by stats.py, which reuses overlay_lib.py instead of
exec()-ing a text slice of 07b_overlay_raster.py, adds a proper marginal-independence
baseline + chance-corrected index, a toroidal-shift null (>=200 shifts), a
distance-decay curve, a spatial block bootstrap, and a cross-variant sensitivity
table. Kept only for reference; run_all.py calls stats.py, not this file.

Baseline + misregistration sensitivity for the raster overlay.
Baseline: strict/cumulative % expected if 1947 zones were unrelated to today's land use
(each group's sets applied to the land-use mix of the whole extent).
Sensitivity: recompute group strict % with the zone raster shifted by +/- SHIFT_FT in x and y."""
import json, sys, numpy as np
from osgeo import gdal
gdal.UseExceptions()
exec(open("07b_overlay_raster.py").read().split("# --- crosstabs")[0])  # reuse zones, lu, ext, cw, LU, DIST, groups
def group_pcts(z):
    m = (ext == 1) & (lu > 0)
    out = {}
    for g, ks in groups.items():
        s = c = d = 0
        for k in ks:
            sel = m & (z == DIST.index(k) + 1)
            l = lu[sel]; spec = cw["districts"][k]
            neutral = np.isin(l, [LU.index(n) + 1 for n in cw["neutral"]])
            d += (~neutral).sum()
            s += np.isin(l, [LU.index(n) + 1 for n in spec["strict"]]).sum()
            c += np.isin(l, [LU.index(n) + 1 for n in spec["cumulative"]]).sum()
        out[g] = (100 * s / d, 100 * c / d)
    return out
# baseline: every parcel pixel in extent treated as if in zone k, averaged by the group's own district mix
m = (ext == 1) & (lu > 0) & (zones > 0)
base = {}
for g, ks in groups.items():
    s = c = d = 0
    for k in ks:
        n_k = (m & (zones == DIST.index(k) + 1)).sum()
        l = lu[m]; spec = cw["districts"][k]
        nn = ~np.isin(l, [LU.index(n) + 1 for n in cw["neutral"]])
        w = n_k / nn.sum()
        d += w * nn.sum(); s += w * np.isin(l, [LU.index(n) + 1 for n in spec["strict"]]).sum()
        c += w * np.isin(l, [LU.index(n) + 1 for n in spec["cumulative"]]).sum()
    base[g] = (100 * s / d, 100 * c / d)
print("baseline (no relationship):", {g: f"strict {a:.1f} / cum {b:.1f}" for g, (a, b) in base.items()})
print("actual:", {g: f"strict {a:.1f} / cum {b:.1f}" for g, (a, b) in group_pcts(zones).items()})
shift = int(float(sys.argv[1]) / gt[1]) if len(sys.argv) > 1 else 50
for dx, dy in [(shift, 0), (-shift, 0), (0, shift), (0, -shift)]:
    z = np.roll(np.roll(zones, dy, 0), dx, 1)
    print(f"shift ({dx*gt[1]:+.0f},{dy*gt[1]:+.0f}) ft:", {g: f"{a:.1f}/{b:.1f}" for g, (a, b) in group_pcts(z).items()})
