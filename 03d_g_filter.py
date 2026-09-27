"""Post-filter for the v3 Central Business (G) relabelling (03c_classify_v3.py).

Region growing in 03c is confined within single street blocks, so a genuine G region is at most one
downtown block (~300 px^2 on the half-res grid). Two large false-G regions arose inside faint-dot J
fields along Buffalo Bayou. Rule: a G connected component reverts to its v2 label if
  (a) its area > 600 half-res px^2 (2x the largest downtown block), or
  (b) it contains > 20 bright-dot blobs per 1000 full-res px (pixel > 15 px local median + 25),
      i.e. the white-dot screen of J (or stipple of A) rather than solid black.
Input zones_px_v3.tif -> output zones_px_v3g.tif (same grid)."""
import json, numpy as np
from osgeo import gdal
from scipy import ndimage
gdal.UseExceptions()
MAX_AREA, MAX_DOTS = 600, 20.0
src = gdal.Open("11139001.jpg")
pre = gdal.Open("zones_px_v3.tif"); v3 = pre.ReadAsArray()
v2 = gdal.Open("zones_px_v2.tif").ReadAsArray()
lab, n = ndimage.label(v3 == 7)
out = v3.copy(); log = []
for i, o in enumerate(ndimage.find_objects(lab)):
    m = lab[o] == i + 1; area = int(m.sum())
    y0, x0 = o[0].start * 2, o[1].start * 2
    mm = np.kron(m, np.ones((2, 2), bool))
    g = src.GetRasterBand(1).ReadAsArray(x0, y0, mm.shape[1], mm.shape[0]).astype(np.float32)
    mm = mm[:g.shape[0], :g.shape[1]]
    dots = ndimage.label((g - ndimage.median_filter(g, size=15) > 25) & mm)[1] / (mm.sum() / 1000)
    reject = area > MAX_AREA or dots > MAX_DOTS
    if reject:
        sel = lab[o] == i + 1
        out[o][sel] = v2[o][sel]
    log.append(dict(x=x0 + mm.shape[1] // 2, y=y0 + mm.shape[0] // 2, area=area, dots=round(float(dots), 2), rejected=bool(reject)))
dst = gdal.GetDriverByName("GTiff").CreateCopy("zones_px_v3g.tif", pre, options=["COMPRESS=DEFLATE"])
dst.GetRasterBand(1).WriteArray(out); dst = None
kept = [r for r in log if not r["rejected"]]
summary = dict(components=len(log), kept=len(kept), rejected=len(log) - len(kept),
               g_area_before=int((v3 == 7).sum()), g_area_after=int((out == 7).sum()),
               rule=dict(max_area_halfres_px=MAX_AREA, max_dots_per_1000px=MAX_DOTS), components_log=log)
json.dump(summary, open("g_filter_log.json", "w"), indent=1)
print({k: v for k, v in summary.items() if k != "components_log"})
print("kept component centres x range:", min(r["x"] for r in kept), max(r["x"] for r in kept), "y range:", min(r["y"] for r in kept), max(r["y"] for r in kept))
