"""Accuracy assessment and misclassification-corrected match estimates (Olofsson et al. 2014).

Inputs: accuracy_v3_sample.csv / accuracy_v3_design.json (04c_accuracy_v3.py) and blind reference
labels accuracy_v3_judged.txt (judging_protocol.md).

Part 1 - map accuracy at the group level. Strata are the predicted groups R/C/I, weighted by
mapped area (W_h). Reference classes are R/C/I/N, where N = "no zone" (a street, bayou or paper
labelled as a zone = commission error). Outputs: an area-weighted error matrix, overall, user's
and producer's accuracy, and error-adjusted areas with 95% CIs.

Part 2 - misclassification-corrected match rates. For each true 1947 group j, estimate
  R_j = P(true group j, parcel non-neutral, 2026 use in the strict/cumulative set of the TRUE district)
        / P(true group j, parcel non-neutral)
with the stratified combined ratio estimator and a linearised SE. The 2026 land use at each point
comes from the parcel raster (overlay_lib.burn_parcels) after mapping the point's scan pixel to
EPSG:2278 with the same order-2 GCP polynomial used for the warp.
"""
import csv, json
import numpy as np
from osgeo import osr
import config, overlay_lib as ol

Z = 1.96
rows = list(csv.DictReader(open("accuracy_v3_sample.csv")))
ref_d = open("accuracy_v3_judged.txt").read().split()
assert len(ref_d) == len(rows)
design = json.load(open("accuracy_v3_design.json"))
G = {**{k: "R" for k in "ABCD"}, **{k: "C" for k in "EFG"}, **{k: "I" for k in "HIJ"}, "0": "N"}
S = ["R", "C", "I"]; REF = ["R", "C", "I", "N"]
Npix = design["strata_pixels_halfres"]; tot = sum(Npix.values())
W = {h: Npix[h] / tot for h in S}
n = {h: sum(r["stratum"] == h for r in rows) for h in S}

# ---------------- Part 1: group-level map accuracy -------------------------------
nij = {h: {j: 0 for j in REF} for h in S}
for r, t in zip(rows, ref_d):
    nij[r["stratum"]][G[t]] += 1
p = {h: {j: W[h] * nij[h][j] / n[h] for j in REF} for h in S}
pj = {j: sum(p[h][j] for h in S) for j in REF}
OA = sum(p[h][h] for h in S)
OA_se = np.sqrt(sum(W[h] ** 2 * (nij[h][h] / n[h]) * (1 - nij[h][h] / n[h]) / (n[h] - 1) for h in S))
out = ["# Accuracy assessment (group level, area-weighted; Olofsson et al. 2014)\n",
       f"Sample: {len(rows)} points, {n} per predicted stratum; weights W_h = {{{', '.join(f'{h}: {W[h]:.3f}' for h in S)}}}\n",
       "## Error matrix (estimated area proportions; rows = map, cols = reference)\n",
       "| map \\ ref | " + " | ".join(REF) + " | W_h | n_h |", "|" + "---|" * (len(REF) + 3)]
for h in S:
    out.append(f"| {h} | " + " | ".join(f"{p[h][j]:.4f}" for j in REF) + f" | {W[h]:.3f} | {n[h]} |")
out.append("| total | " + " | ".join(f"{pj[j]:.4f}" for j in REF) + " | 1 | |\n")
out.append(f"**Overall accuracy**: {OA:.3f} ± {Z * OA_se:.3f} (95% CI)\n")
out.append("| group | user's acc. | ± | producer's acc. | ± | mapped share | error-adjusted share | ± |")
out.append("|---|---|---|---|---|---|---|---|")
for j in S:
    U = nij[j][j] / n[j]; U_se = np.sqrt(U * (1 - U) / (n[j] - 1))
    P = p[j][j] / pj[j]
    # producer's accuracy variance: Olofsson et al. (2014) eq. 7
    Nj_hat = sum(Npix[h] / n[h] * nij[h][j] for h in S)
    a = Npix[j] ** 2 * (1 - P) ** 2 * U * (1 - U) / (n[j] - 1)
    b = P ** 2 * sum(Npix[h] ** 2 * (nij[h][j] / n[h]) * (1 - nij[h][j] / n[h]) / (n[h] - 1) for h in S if h != j)
    P_se = np.sqrt((a + b) / Nj_hat ** 2)
    A_se = np.sqrt(sum(W[h] ** 2 * (nij[h][j] / n[h]) * (1 - nij[h][j] / n[h]) / (n[h] - 1) for h in S))
    out.append(f"| {j} | {U:.3f} | {Z * U_se:.3f} | {P:.3f} | {Z * P_se:.3f} | {W[j]:.3f} | {pj[j]:.3f} | {Z * A_se:.3f} |")
out.append(f"\nReference 'N' (no zone) share of mapped zone area: {pj['N']:.3f}\n")

# district-level (descriptive, unweighted counts)
L = "ABCDEFGHIJ0"
cm = {a: {b: 0 for b in L} for a in L[:-1]}
for r, t in zip(rows, ref_d):
    cm[r["pred"]][t] += 1
out.append("## District-level confusion (sample counts; rows = predicted, cols = reference)\n")
out.append("| pred \\ ref | " + " | ".join(L) + " | n | UA |"); out.append("|" + "---|" * (len(L) + 3))
for a in L[:-1]:
    na = sum(cm[a].values())
    out.append(f"| {a} | " + " | ".join(str(cm[a][b]) for b in L) + f" | {na} | {cm[a][a] / na:.2f} |" if na else f"| {a} | " + " | ".join("0" for _ in L) + " | 0 | – |")

# ---------------- Part 2: corrected match rates -----------------------------------
g = [r for r in csv.DictReader(open(config.GCPS_FROZEN_CSV))]
src = osr.SpatialReference(); src.ImportFromEPSG(4326); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
dst = osr.SpatialReference(); dst.ImportFromEPSG(config.DST_EPSG)
tr = osr.CoordinateTransformation(src, dst)
XY = np.array([tr.TransformPoint(float(r["lon"]), float(r["lat"]))[:2] for r in g])
px = np.array([[float(r["pixel_x"]), float(r["pixel_y"])] for r in g])
def design_m(u, v): return np.column_stack([np.ones_like(u), u, v, u * u, u * v, v * v])
sc = 1e-4
cx, *_ = np.linalg.lstsq(design_m(px[:, 0] * sc, px[:, 1] * sc), XY[:, 0], rcond=None)
cy, *_ = np.linalg.lstsq(design_m(px[:, 0] * sc, px[:, 1] * sc), XY[:, 1], rcond=None)

grids = ol.load_grids()
lu = ol.burn_parcels(grids)
cw = ol.load_crosswalk()
LU = ol.LU
gt = grids.gt
pts_px = np.array([[float(r["x"]), float(r["y"])] for r in rows])
D = design_m(pts_px[:, 0] * sc, pts_px[:, 1] * sc)
X, Y = D @ cx, D @ cy
ci = ((X - gt[0]) / gt[1]).astype(int); ri = ((Y - gt[3]) / gt[5]).astype(int)
inb = (ci >= 0) & (ci < grids.nx) & (ri >= 0) & (ri < grids.ny)
lu_at = np.where(inb, lu[np.clip(ri, 0, grids.ny - 1), np.clip(ci, 0, grids.nx - 1)], 0)
neutral = set(cw["neutral"])
with open("accuracy_v3_points.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["id", "x", "y", "stratum", "pred", "ref", "x2278", "y2278", "lu2026"])
    for i, r in enumerate(rows):
        w.writerow([r["id"], r["x"], r["y"], r["stratum"], r["pred"], ref_d[i], f"{X[i]:.1f}", f"{Y[i]:.1f}",
                    LU[lu_at[i] - 1] if lu_at[i] > 0 else "NO_PARCEL"])


def ratio(j, kind, basis):
    """Stratified combined ratio estimate for true (basis='ref') or mapped (basis='pred') group j."""
    ys, xs = {h: [] for h in S}, {h: [] for h in S}
    for i, r in enumerate(rows):
        d = ref_d[i] if basis == "ref" else r["pred"]
        use = LU[lu_at[i] - 1] if lu_at[i] > 0 else None
        x = int(d != "0" and G[d] == j and use is not None and use not in neutral)
        y = int(x and use in cw["districts"][d][kind])
        ys[r["stratum"]].append(y); xs[r["stratum"]].append(x)
    Yh = sum(W[h] * np.mean(ys[h]) for h in S); Xh = sum(W[h] * np.mean(xs[h]) for h in S)
    R = Yh / Xh
    var = sum(W[h] ** 2 * np.var(np.array(ys[h]) - R * np.array(xs[h]), ddof=1) / n[h] for h in S) / Xh ** 2
    return R, np.sqrt(var), sum(sum(xs[h]) for h in S)


out.append("\n## Match rates corrected for misclassification (stratified ratio estimator)\n")
out.append("Match = 2026 parcel use in the strict/cumulative set of the point's **reference** (true) 1947 district; "
           "denominator = points on non-neutral parcel land in that true group. The same estimator on **mapped** "
           "labels is shown for comparison with the wall-to-wall overlay.\n")
out.append("| 1947 group | strict (true) | 95% CI | cumulative (true) | 95% CI | n points | strict (mapped labels, same sample) |")
out.append("|---|---|---|---|---|---|---|")
res = {}
for j, name in zip(S, ["Residential", "Commercial", "Industrial"]):
    Rs, ses, nx_ = ratio(j, "strict", "ref"); Rc, sec, _ = ratio(j, "cumulative", "ref"); Rm, _, _ = ratio(j, "strict", "pred")
    res[name] = dict(strict=Rs, strict_se=ses, cumulative=Rc, cumulative_se=sec, n=nx_, strict_mapped=Rm)
    out.append(f"| {name} | {100 * Rs:.1f}% | [{100 * (Rs - Z * ses):.1f}, {100 * (Rs + Z * ses):.1f}] | {100 * Rc:.1f}% | "
               f"[{100 * (Rc - Z * sec):.1f}, {100 * (Rc + Z * sec):.1f}] | {nx_} | {100 * Rm:.1f}% |")
open("accuracy_v3_report.md", "w", encoding="utf-8").write("\n".join(out) + "\n")
json.dump(dict(OA=OA, OA_se=OA_se, corrected=res), open("accuracy_v3_results.json", "w"), indent=1)
print("\n".join(out))
