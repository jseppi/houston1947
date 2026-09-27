"""Compare warp models on the frozen GCP sets: order-1/2 polynomials and thin-plate spline (TPS,
with optional smoothing), fitted pixel -> EPSG:2278. Criteria: leave-one-out RMSE on the GCPs and
RMSE on the 8 independent check points (checkpoints.csv). Writes gcp_model_compare.csv."""
import csv, numpy as np
from osgeo import osr
from scipy.interpolate import RBFInterpolator
src = osr.SpatialReference(); src.ImportFromEPSG(4326); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
dst = osr.SpatialReference(); dst.ImportFromEPSG(2278); tr = osr.CoordinateTransformation(src, dst)
def load(p):
    r = list(csv.DictReader(open(p)))
    return (np.array([[float(a["pixel_x"]), float(a["pixel_y"])] for a in r]),
            np.array([tr.TransformPoint(float(a["lon"]), float(a["lat"]))[:2] for a in r]))
def poly(order):
    def D(p):
        u, v = p[:, 0] / 1e4, p[:, 1] / 1e4
        cols = [np.ones_like(u), u, v] + ([u * u, u * v, v * v] if order == 2 else [])
        return np.column_stack(cols)
    def fit(P, X):
        c, *_ = np.linalg.lstsq(D(P), X, rcond=None); return lambda Q: D(Q) @ c
    return fit
def tps(smooth):
    return lambda P, X: RBFInterpolator(P / 1e4, X, kernel="thin_plate_spline", smoothing=smooth, degree=1)
def tps_pred(f): return lambda Q: f(Q / 1e4)
cp_P, cp_X = load("checkpoints.csv")
rows = []
for gname in ["gcps_frozen.csv", "gcps_frozen_v2.csv"]:
    P, X = load(gname)
    models = {"poly1": poly(1), "poly2": poly(2)}
    for s in [0.0, 1e-4, 1e-3, 1e-2]:
        models[f"tps_s{s:g}"] = tps(s)
    for m, fitter in models.items():
        wrap = (lambda f: tps_pred(f)) if m.startswith("tps") else (lambda f: f)
        loo = []
        for i in range(len(P)):
            k = np.arange(len(P)) != i
            f = wrap(fitter(P[k], X[k])); loo.append(np.hypot(*(f(P[i:i + 1])[0] - X[i])))
        f = wrap(fitter(P, X)); cp = np.hypot(*(f(cp_P) - cp_X).T)
        rows.append(dict(gcps=gname, n=len(P), model=m, loo_rmse=np.sqrt(np.mean(np.square(loo))), loo_max=max(loo),
                         cp_rmse=np.sqrt(np.mean(cp ** 2)), cp_max=cp.max()))
with open("gcp_model_compare.csv", "w", newline="") as fo:
    w = csv.DictWriter(fo, fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
for r in rows: print(f"{r['gcps']:<20}{r['n']:>3} {r['model']:<12} LOO {r['loo_rmse']:7.1f} (max {r['loo_max']:6.1f})  check {r['cp_rmse']:6.1f} (max {r['cp_max']:6.1f})")
