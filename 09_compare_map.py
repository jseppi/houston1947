"""Side-by-side map: 1947 zoning group vs 2026 land-use group, clipped to the 1947 extent.
Uses overlay_lib so it stays consistent with stats.py / run_all.py (task 6)."""
import numpy as np
from PIL import Image, ImageDraw

import overlay_lib as ol

cw = ol.load_crosswalk()
grids = ol.load_grids()
lu = ol.burn_parcels(grids)
zones, ext = grids.zones, grids.ext

R, C, I, P, N, X = (246, 214, 90), (220, 60, 60), (140, 60, 200), (70, 150, 220), (190, 190, 190), (255, 255, 255)
g47 = {1: R, 2: R, 3: R, 4: R, 5: C, 6: C, 7: C, 8: I, 9: I, 10: I}
g26 = {1: R, 2: R, 3: C, 4: C, 5: I, 7: I, 6: P, 8: P, 9: N, 10: N, 11: N}


def paint(a, lut):
    img = np.full(a.shape + (3,), 255, np.uint8)
    for k, v in lut.items():
        img[a == k] = v
    img[ext != 1] = (245, 245, 245)
    return Image.fromarray(img).resize((a.shape[1] // 4, a.shape[0] // 4), Image.NEAREST)


a, b = paint(zones, g47), paint(lu, g26)
W, H = a.size
out = Image.new("RGB", (2 * W + 20, H + 70), "white")
out.paste(a, (0, 40))
out.paste(b, (W + 20, 40))
d = ImageDraw.Draw(out)
d.text((10, 10), "1947 zoning plan (auto-classified)", fill=(0, 0, 0))
d.text((W + 30, 10), "2026 land use (parcels)", fill=(0, 0, 0))
for i, (lab, c) in enumerate([("Residential", R), ("Commercial/Office", C), ("Industrial/Transp.", I),
                              ("Public/Park", P), ("Undeveloped/other", N), ("Street/ROW/unclassified", X)]):
    d.rectangle([10 + i * 190, H + 48, 26 + i * 190, H + 62], fill=c, outline=(0, 0, 0))
    d.text((32 + i * 190, H + 50), lab, fill=(0, 0, 0))
out.save("compare_1947_2026.png")
print(out.size)
