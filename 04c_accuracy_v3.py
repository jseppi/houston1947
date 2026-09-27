"""Accuracy-assessment sample for the final 1947 zone map (zones_px_v3g.tif), following
Olofsson et al. (2014): stratified random sampling by PREDICTED group (Residential A-D,
Commercial E-G, Industrial H-J), N_PER points per stratum, restricted to the final extent
(extent_px_v2.tif). Tiles are rendered blind (id only, random order, fixed seed) with the legend
swatches for reference; judge with judging_protocol.md and record labels in accuracy_v3_judged.txt
(one token per id, A-J or 0). Stratum pixel counts are saved for the area-weighted estimators."""
import csv, json, numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw
gdal.UseExceptions()
N_PER, T, SEED = 90, 180, 31415
rng = np.random.default_rng(SEED)
z = gdal.Open("zones_px_v3g.tif").ReadAsArray()                      # scale 2
e = gdal.Open("extent_px_v2.tif").ReadAsArray()                      # scale 8
ext = np.kron(e > 0, np.ones((4, 4), bool))[:z.shape[0], :z.shape[1]]
if ext.shape != z.shape:
    ext = np.pad(ext, ((0, z.shape[0] - ext.shape[0]), (0, z.shape[1] - ext.shape[1])))
GROUP = {"R": (1, 2, 3, 4), "C": (5, 6, 7), "I": (8, 9, 10)}
L = " ABCDEFGHIJ"
pts, strata = [], {}
for g, codes in GROUP.items():
    rr, cc = np.nonzero(np.isin(z, codes) & ext)
    strata[g] = int(len(rr))
    for i in rng.choice(len(rr), N_PER, replace=False):
        pts.append(dict(x=int(cc[i] * 2 + 1), y=int(rr[i] * 2 + 1), stratum=g, pred=L[z[rr[i], cc[i]]]))
pts = [pts[i] for i in rng.permutation(len(pts))]
json.dump(dict(strata_pixels_halfres=strata, n_per_stratum=N_PER, seed=SEED, zones="zones_px_v3g.tif",
               extent="extent_px_v2.tif"), open("accuracy_v3_design.json", "w"), indent=1)
with open("accuracy_v3_sample.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["id", "x", "y", "stratum", "pred"]); w.writeheader()
    for i, p in enumerate(pts): w.writerow(dict(id=i, **p))
src = gdal.Open("11139001.jpg")
sw = dict(zip("ABCDEFGHIJ", [3555, 3624, 3692, 3760, 3829, 3897, 3965, 4034, 4103, 4171]))
PER = 40
for s in range(0, len(pts), PER):
    im = Image.new("RGB", (8 * (T + 6), 5 * (T + 20) + 130), "white"); d = ImageDraw.Draw(im)
    for k, p in enumerate(pts[s:s + PER]):
        x0, y0 = max(0, p["x"] - T // 2), max(0, p["y"] - T // 2)
        t = Image.fromarray(np.dstack([src.GetRasterBand(b).ReadAsArray(x0, y0, T, T) for b in (1, 2, 3)]))
        ImageDraw.Draw(t).rectangle([p["x"] - x0 - 12, p["y"] - y0 - 12, p["x"] - x0 + 12, p["y"] - y0 + 12], outline=(255, 0, 0), width=2)
        r, c = divmod(k, 8); im.paste(t, (c * (T + 6), r * (T + 20) + 20)); d.text((c * (T + 6) + 2, r * (T + 20) + 4), f"#{s + k}", fill=(0, 0, 0))
    oy = 5 * (T + 20) + 10
    for j, (l, y) in enumerate(sw.items()):
        t = Image.fromarray(np.dstack([src.GetRasterBand(b).ReadAsArray(9483, y - 22, 60, 44) for b in (1, 2, 3)])).resize((120, 88))
        im.paste(t, (j * 140, oy + 20)); d.text((j * 140 + 55, oy + 4), l, fill=(0, 0, 0))
    im.save(f"accuracy_v3_sheet_{s // PER}.png")
print(strata, len(pts))
