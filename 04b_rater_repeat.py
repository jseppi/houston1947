"""Intra-rater reliability: re-render a shuffled 30-point subset of accuracy_blind.csv with new ids
(rater_repeat.png) for a second blind judgement; key saved to rater_repeat_key.csv."""
import csv, numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw
gdal.UseExceptions()
rng = np.random.default_rng(2026)
rows = list(csv.DictReader(open("accuracy_blind.csv")))
pick = rng.choice(len(rows), 30, replace=False)
src = gdal.Open("11139001.jpg"); T = 180
im = Image.new("RGB", (6 * (T + 6), 5 * (T + 20)), "white"); d = ImageDraw.Draw(im)
with open("rater_repeat_key.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["new_id", "orig_id"])
    for k, i in enumerate(pick):
        x, y = int(rows[i]["x"]), int(rows[i]["y"])
        x0, y0 = max(0, x - T // 2), max(0, y - T // 2)
        t = Image.fromarray(np.dstack([src.GetRasterBand(b).ReadAsArray(x0, y0, T, T) for b in (1, 2, 3)]))
        ImageDraw.Draw(t).rectangle([x - x0 - 12, y - y0 - 12, x - x0 + 12, y - y0 + 12], outline=(255, 0, 0), width=2)
        r, c = divmod(k, 6); im.paste(t, (c * (T + 6), r * (T + 20) + 20)); d.text((c * (T + 6) + 2, r * (T + 20) + 4), f"r{k}", fill=(0, 0, 0))
        w.writerow([k, rows[i]["id"]])
im.save("rater_repeat.png")
