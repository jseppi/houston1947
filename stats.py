"""Task 5: replacement for 08_sensitivity.py. Statistics for the 1947-plan-vs-2026
overlay: observed strict/cumulative %, marginal (independence) baseline and
chance-corrected index, a toroidal-shift null, a distance-decay curve, a spatial
block bootstrap, a sensitivity table across input versions/toggles, and the
"where the plan missed" off-diagonal shares.

Outputs: stats_summary.csv, stats_report.md, distance_decay.csv, distance_decay.png
"""
import csv
import json
import os

import numpy as np

import config
import overlay_lib as ol

DIST = ol.DIST
LU = ol.LU
N_LU = len(LU)


def build_matrices(cw):
    """S/Cm[zone_code(1..10), lu_code(0..N_LU)] boolean membership; neutral flag per lu_code."""
    S = np.zeros((11, N_LU + 1), dtype=bool)
    Cm = np.zeros((11, N_LU + 1), dtype=bool)
    neutral = np.zeros(N_LU + 1, dtype=bool)
    for n in cw["neutral"]:
        neutral[LU.index(n) + 1] = True
    for i, k in enumerate(DIST):
        spec = cw["districts"][k]
        for n in spec["strict"]:
            S[i + 1, LU.index(n) + 1] = True
        for n in spec["cumulative"]:
            Cm[i + 1, LU.index(n) + 1] = True
    return S, Cm, neutral


GROUP_DISTRICTS = {"Residential": "ABCD", "Commercial": "EFG", "Industrial": "HIJ"}


def group_pcts_from_zonelu(zones, lu, ext, S, Cm, neutral, groups=GROUP_DISTRICTS):
    """Strict/cumulative % per group, and per-district, given zone/lu arrays (already
    masked to ext elsewhere or masked here)."""
    m = (ext == 1) & (lu > 0) & (zones > 0)
    key = (zones[m].astype(np.int64) * (N_LU + 1) + lu[m].astype(np.int64))
    ct = np.bincount(key, minlength=11 * (N_LU + 1)).reshape(11, N_LU + 1)

    out_group = {}
    for g, ks in groups.items():
        ix = [DIST.index(k) + 1 for k in ks]
        c = ct[ix]
        nonneutral = c * (~neutral)
        denom = nonneutral.sum()
        strict = (c * S[ix]).sum()
        cum = (c * Cm[ix]).sum()
        out_group[g] = dict(denom=int(denom), strict_pct=100 * strict / denom if denom else float("nan"),
                             cumulative_pct=100 * cum / denom if denom else float("nan"))

    out_district = {}
    for k in DIST:
        i = DIST.index(k) + 1
        c = ct[i]
        nonneutral = c * (~neutral)
        denom = nonneutral.sum()
        strict = (c * S[i]).sum()
        cum = (c * Cm[i]).sum()
        out_district[k] = dict(denom=int(denom), strict_pct=100 * strict / denom if denom else float("nan"),
                                cumulative_pct=100 * cum / denom if denom else float("nan"))
    return out_group, out_district, ct


def marginal_baseline(zones, lu, ext, S, Cm, neutral, groups=GROUP_DISTRICTS):
    """Independence baseline: hold the group's own district-area mix fixed, but use
    the whole-extent land-use MIX (marginal distribution) instead of the actual
    co-located land use -- i.e. what strict/cumulative % would be if 1947 zone and
    2026 land use were statistically independent."""
    m = (ext == 1) & (lu > 0) & (zones > 0)
    lu_all = lu[m]
    nonneutral_all = ~neutral[lu_all]
    lu_marginal_counts = np.bincount(lu_all[nonneutral_all], minlength=N_LU + 1).astype(np.float64)
    total_nonneutral = lu_marginal_counts.sum()
    lu_marginal_frac = lu_marginal_counts / total_nonneutral if total_nonneutral else lu_marginal_counts

    out = {}
    for g, ks in groups.items():
        area_by_district = {}
        for k in ks:
            sel = m & (zones == DIST.index(k) + 1)
            area_by_district[k] = sel.sum()
        tot_area = sum(area_by_district.values())
        strict = cum = 0.0
        for k in ks:
            spec_S = S[DIST.index(k) + 1]
            spec_Cm = Cm[DIST.index(k) + 1]
            w = area_by_district[k]
            strict += w * (lu_marginal_frac * spec_S).sum()
            cum += w * (lu_marginal_frac * spec_Cm).sum()
        out[g] = dict(strict_pct=100 * strict / tot_area if tot_area else float("nan"),
                      cumulative_pct=100 * cum / tot_area if tot_area else float("nan"))
    return out


def chance_corrected(obs_pct, exp_pct):
    """(obs-exp)/(1-exp) on the 0..1 scale, reported as a number (kappa-like index)."""
    o, e = obs_pct / 100.0, exp_pct / 100.0
    if e >= 1.0:
        return float("nan")
    return (o - e) / (1 - e)


def toroidal_null(zones, lu, ext, S, Cm, neutral, n_shifts, rng):
    """>=200 random toroidal shifts of the ZONE raster only (parcels/lu and the
    extent mask held fixed). Wrapping (np.roll) means pixels shifted off one edge
    reappear on the opposite edge -- this can introduce edge artefacts where the
    wrapped-in content differs systematically from what left (e.g. water/ROW at
    one edge vs. dense parcels at the other), but keeps every shift's mask the
    same size as `ext`, unlike a border-mask approach which would shrink the
    usable area per shift. We accept this well-documented approximation, consistent
    with the referee's null2.py reference."""
    ny, nx = zones.shape
    groups = GROUP_DISTRICTS
    results = {g: [] for g in groups}
    results_cum = {g: [] for g in groups}
    for i in range(n_shifts):
        r = rng.uniform(config.NULL_SHIFT_MIN_FT, config.NULL_SHIFT_MAX_FT)
        th = rng.uniform(0, 2 * np.pi)
        dx = int(round(r * np.cos(th) / config.ZONES_RES_FT))
        dy = int(round(r * np.sin(th) / config.ZONES_RES_FT))
        z = np.roll(np.roll(zones, dy, axis=0), dx, axis=1)
        out_group, _, _ = group_pcts_from_zonelu(z, lu, ext, S, Cm, neutral, groups)
        for g in groups:
            results[g].append(out_group[g]["strict_pct"])
            results_cum[g].append(out_group[g]["cumulative_pct"])
    return ({g: np.array(v) for g, v in results.items()},
            {g: np.array(v) for g, v in results_cum.items()})


def distance_decay(zones, lu, ext, S, Cm, neutral, max_ft, step_ft, n_dirs):
    groups = GROUP_DISTRICTS
    mags = np.arange(0, max_ft + 1e-6, step_ft)
    rows = []
    for mag in mags:
        vals = {g: [] for g in groups}
        for j in range(n_dirs):
            th = 2 * np.pi * j / n_dirs
            dx = int(round(mag * np.cos(th) / config.ZONES_RES_FT))
            dy = int(round(mag * np.sin(th) / config.ZONES_RES_FT))
            z = np.roll(np.roll(zones, dy, axis=0), dx, axis=1) if mag > 0 else zones
            out_group, _, _ = group_pcts_from_zonelu(z, lu, ext, S, Cm, neutral, groups)
            for g in groups:
                vals[g].append(out_group[g]["strict_pct"])
        row = {"shift_ft": mag}
        for g in groups:
            row[f"{g}_strict_pct_mean"] = float(np.mean(vals[g]))
        rows.append(row)
    return rows


def precompute_blocks(zones, lu, ext, block_px):
    """Per-block (zone,lu) count table for the spatial block bootstrap. Returns
    block_table[block_id, zone_code(0..10), lu_code(0..N_LU)] and n_blocks."""
    ny, nx = zones.shape
    by = (np.arange(ny) // block_px)
    bx = (np.arange(nx) // block_px)
    nby, nbx = by[-1] + 1, bx[-1] + 1
    block_id = (by[:, None] * nbx + bx[None, :]).astype(np.int64)
    m = ext == 1
    n_blocks = nby * nbx
    key = (block_id[m] * 11 * (N_LU + 1) + zones[m].astype(np.int64) * (N_LU + 1) + lu[m].astype(np.int64))
    table = np.bincount(key, minlength=n_blocks * 11 * (N_LU + 1)).reshape(n_blocks, 11, N_LU + 1)
    return table, n_blocks


def block_bootstrap(table, n_blocks, S, Cm, neutral, reps, rng, groups=GROUP_DISTRICTS):
    out = {g: {"strict": [], "cumulative": []} for g in groups}
    for r in range(reps):
        idx = rng.integers(0, n_blocks, size=n_blocks)
        counts = np.bincount(idx, minlength=n_blocks)
        ct = np.tensordot(counts, table, axes=(0, 0))  # (11, N_LU+1)
        for g, ks in groups.items():
            ix = [DIST.index(k) + 1 for k in ks]
            c = ct[ix]
            # denominator = parcel land excluding neutral; column 0 ("no parcel" = ROW/water)
            # must be excluded too, as in the headline estimate
            keep = ~neutral
            keep[0] = False
            denom = (c * keep).sum()
            strict = (c * S[ix]).sum()
            cum = (c * Cm[ix]).sum()
            out[g]["strict"].append(100 * strict / denom if denom else np.nan)
            out[g]["cumulative"].append(100 * cum / denom if denom else np.nan)
    return {g: {k: np.array(v) for k, v in d.items()} for g, d in out.items()}


def off_diagonal_shares(ct, cw, groups=GROUP_DISTRICTS):
    """'Where the plan missed': for each 1947 group, the share of its (parcel land
    excl. neutral) denominator that fell into each OTHER 2026 group -- same
    denominator as the headline strict %."""
    col = {d: i + 1 for i, d in enumerate(LU)}
    lu2026_group = {
        "Residential": ["Single-Family Residential", "Multi-Family Residential"],
        "Commercial": ["Commercial", "Office"],
        "Industrial": ["Industrial", "Transportation & Utility"],
        "Public/Park": ["Public & Institutional", "Park & Open Spaces"],
    }
    out = {}
    for g, ks in groups.items():
        ix = [DIST.index(k) + 1 for k in ks]
        c = ct[ix].sum(0)
        neutral_mask = np.zeros(N_LU + 1, dtype=bool)
        for n in cw["neutral"]:
            neutral_mask[col[n]] = True
        denom = c[~neutral_mask].sum() - c[0]  # exclude no-parcel(0) and neutral
        # (c[0] is "no parcel" which isn't in neutral list but also not part of
        # the parcel-land denominator; c already excludes it since col starts at 1,
        # but ~neutral_mask[0] is True by default -- guard explicitly)
        denom = c[1:][~neutral_mask[1:]].sum()
        shares = {}
        for lg, lus in lu2026_group.items():
            shares[lg] = 100 * sum(c[col[n]] for n in lus) / denom if denom else float("nan")
        out[g] = shares
    return out


_ALT_WARP_CACHE = {}


def warp_alt_zones(px_tif, tag):
    """Warp an alternate zones_px_*.tif (v2 / v3 / v3g / ...) to EPSG:2278 with the
    SAME frozen GCPs + order-2 fit used for the headline warp, so it can be dropped
    into run_variant() for a same-registration zones-version sensitivity comparison.
    Cached per-process; the source raster is opened, read, and closed immediately
    (not held open) to avoid file locks while a concurrent process may still be
    writing sibling files."""
    if tag in _ALT_WARP_CACHE:
        return _ALT_WARP_CACHE[tag]
    if not os.path.exists(px_tif):
        return None
    import importlib
    import tempfile
    georef = importlib.import_module("05_georef_zones")  # filename starts with a digit, hence importlib
    out_tif = os.path.join(config.DATA_DIR, f"zones_2278_{tag}.tif")
    rows = georef.read_gcps(config.GCPS_FROZEN_CSV)
    xy_2278 = georef.lonlat_to_2278(rows) if config.FIT_IN_TARGET_CRS else None
    out_srs = f"EPSG:{config.DST_EPSG}" if config.FIT_IN_TARGET_CRS else "EPSG:4326"
    gcps = georef.build_gdal_gcps(rows, config.ZONES_SCALE, xy_2278=xy_2278)
    with tempfile.TemporaryDirectory() as tmp:
        vrt = georef.assign_gcps_vrt(px_tif, gcps, tmp, tag, out_srs)
        georef.warp_to_2278(vrt, out_tif, config.ZONES_RES_FT, resample="near", nodata=0,
                            order=config.GEOREF_ORDER, method="poly")
    _ALT_WARP_CACHE[tag] = out_tif
    return out_tif


def run_variant(zones_tif, vacant_as_neutral, burn_order, cw, gpkg=None):
    grids = ol.load_grids(zones_tif=zones_tif, gpkg=gpkg)
    lu = ol.burn_parcels(grids, gpkg=gpkg, vacant_as_neutral=vacant_as_neutral, burn_order=burn_order)
    S, Cm, neutral = build_matrices(cw)
    out_group, out_district, ct = group_pcts_from_zonelu(grids.zones, lu, grids.ext, S, Cm, neutral)
    return grids, lu, S, Cm, neutral, out_group, out_district, ct


def main():
    cw = ol.load_crosswalk()
    rng = np.random.default_rng(config.RNG_SEED)

    print("Loading grids + burning parcels (headline configuration)...")
    grids, lu, S, Cm, neutral, out_group, out_district, ct = run_variant(
        config.ZONES_2278, config.VACANT_AS_NEUTRAL, config.DEDUPE_BURN_ORDER, cw)

    print("Marginal baseline...")
    baseline = marginal_baseline(grids.zones, lu, grids.ext, S, Cm, neutral)

    print(f"Toroidal-shift null ({config.N_NULL_SHIFTS} shifts)...")
    null, null_cum = toroidal_null(grids.zones, lu, grids.ext, S, Cm, neutral, config.N_NULL_SHIFTS, rng)

    print("Distance-decay curve...")
    decay_rows = distance_decay(grids.zones, lu, grids.ext, S, Cm, neutral,
                                 config.DECAY_MAX_FT, config.DECAY_STEP_FT, config.DECAY_N_DIRECTIONS)
    with open("distance_decay.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(decay_rows[0].keys()))
        w.writeheader()
        w.writerows(decay_rows)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 4))
        xs = [r["shift_ft"] for r in decay_rows]
        for g in GROUP_DISTRICTS:
            ax.plot(xs, [r[f"{g}_strict_pct_mean"] for r in decay_rows], marker="o", label=g)
        ax.set_xlabel("rigid shift magnitude (ft, averaged over 8 directions)")
        ax.set_ylabel("strict match %")
        ax.set_title("Distance-decay: strict match vs. misregistration")
        ax.legend()
        fig.tight_layout()
        fig.savefig("distance_decay.png", dpi=140)
        plt.close(fig)
    except Exception as e:
        print("WARN: could not render distance_decay.png:", e)

    print(f"Spatial block bootstrap ({config.BOOTSTRAP_REPS} reps, {config.BOOTSTRAP_BLOCK_FT} ft blocks)...")
    block_px = int(round(config.BOOTSTRAP_BLOCK_FT / config.ZONES_RES_FT))
    table, n_blocks = precompute_blocks(grids.zones, lu, grids.ext, block_px)
    boot = block_bootstrap(table, n_blocks, S, Cm, neutral, config.BOOTSTRAP_REPS, rng)

    print("Off-diagonal shares...")
    offdiag = off_diagonal_shares(ct, cw)

    print("Sensitivity table across input versions/toggles...")
    sensitivity_rows = []

    def add_variant(label, **kw):
        try:
            g2, lu2, S2, Cm2, neu2, og2, od2, ct2 = run_variant(cw=cw, **kw)
            row = {"variant": label}
            for g in GROUP_DISTRICTS:
                row[f"{g}_strict_pct"] = round(og2[g]["strict_pct"], 1)
            sensitivity_rows.append(row)
        except Exception as e:
            sensitivity_rows.append({"variant": label, "error": str(e)})

    add_variant(f"headline (zones {config.ZONES_PX_VERSION}, extent {config.EXTENT_PX_VERSION}, "
                f"vacant=neutral, dedupe neutral_first)",
                zones_tif=config.ZONES_2278, vacant_as_neutral=True, burn_order="neutral_first")
    add_variant("vacant NOT treated as neutral",
                zones_tif=config.ZONES_2278, vacant_as_neutral=False, burn_order="neutral_first")
    add_variant("dedupe burn order reversed (neutral_last)",
                zones_tif=config.ZONES_2278, vacant_as_neutral=True, burn_order="neutral_last")
    if os.path.exists(config.ZONES_2278_TPS):
        add_variant("TPS warp instead of order-2 polynomial",
                    zones_tif=config.ZONES_2278_TPS, vacant_as_neutral=True, burn_order="neutral_first")

    # zones-classification-version sensitivity: v2 vs v3 (prefilter) vs v3g (final,
    # G-district post-filtered), all re-warped with the SAME frozen GCPs/order-2 fit
    # so only the classifier input differs, not the registration.
    zone_versions = [("v2", os.path.join(config.DATA_DIR, "zones_px_v2.tif")),
                      ("v3", os.path.join(config.DATA_DIR, "zones_px_v3.tif")),
                      ("v3g", os.path.join(config.DATA_DIR, "zones_px_v3g.tif"))]
    for tag, src in zone_versions:
        if not os.path.exists(src):
            sensitivity_rows.append({"variant": f"{tag} zones classification",
                                      "note": f"not yet available ({os.path.basename(src)} missing)"})
            continue
        warped = warp_alt_zones(src, tag)
        if warped is None:
            sensitivity_rows.append({"variant": f"{tag} zones classification", "note": "warp failed"})
            continue
        add_variant(f"zones {tag} ({os.path.basename(src)}, same GCPs/order-2 registration)",
                    zones_tif=warped, vacant_as_neutral=True, burn_order="neutral_first")

    if os.path.exists(os.path.join(config.DATA_DIR, "extent_px_v2.tif")) and \
       os.path.exists(os.path.join(config.DATA_DIR, "extent_px.tif")):
        sensitivity_rows.append({"variant": "v1 vs v2 extent extraction",
                                  "note": "both extent_px.tif (v1) and extent_px_v2.tif (v2) exist; the "
                                          "headline run already uses v2 per config.py's auto-detection "
                                          "(EXTENT_PX_VERSION=v2). A held-fixed v1-vs-v2 comparison would "
                                          "require re-polygonizing extent_1947 from each and is not run "
                                          "here to keep this pass's runtime bounded -- see extent_meta_v2.json "
                                          "for the v1-vs-v2 area delta (-9,245,760 full-res px2, i.e. v2 is "
                                          "smaller / tighter than v1)."})

    # --- write stats_summary.csv --------------------------------------------------
    with open("stats_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "denom_parcel_acres_excl_neutral", "observed_strict_pct", "observed_cumulative_pct",
                    "bootstrap_strict_ci_lo", "bootstrap_strict_ci_hi",
                    "bootstrap_cumulative_ci_lo", "bootstrap_cumulative_ci_hi",
                    "marginal_baseline_strict_pct", "marginal_baseline_cumulative_pct",
                    "chance_corrected_index_strict", "chance_corrected_index_cumulative",
                    "null_mean_strict_pct", "null_p95_strict_pct", "empirical_p_value_strict",
                    "null_mean_cumulative_pct", "null_p95_cumulative_pct", "empirical_p_value_cumulative"])
        for g in GROUP_DISTRICTS:
            o = out_group[g]
            b = boot[g]
            s_lo, s_hi = np.nanpercentile(b["strict"], [2.5, 97.5])
            c_lo, c_hi = np.nanpercentile(b["cumulative"], [2.5, 97.5])
            bl = baseline[g]
            kappa_s = chance_corrected(o["strict_pct"], bl["strict_pct"])
            kappa_c = chance_corrected(o["cumulative_pct"], bl["cumulative_pct"])
            nv = null[g]
            pval = float((nv >= o["strict_pct"]).mean())
            nc = null_cum[g]
            pval_c = float((nc >= o["cumulative_pct"]).mean())
            w.writerow([g, round(o["denom"] * grids.px_acres, 1), round(o["strict_pct"], 1), round(o["cumulative_pct"], 1),
                        round(s_lo, 1), round(s_hi, 1), round(c_lo, 1), round(c_hi, 1),
                        round(bl["strict_pct"], 1), round(bl["cumulative_pct"], 1),
                        round(kappa_s, 3), round(kappa_c, 3),
                        round(nv.mean(), 1), round(np.percentile(nv, 95), 1), round(pval, 4),
                        round(nc.mean(), 1), round(np.percentile(nc, 95), 1), round(pval_c, 4)])
        # district rows
        for k in DIST:
            o = out_district[k]
            w.writerow([f"district {k}", round(o["denom"] * grids.px_acres, 1), round(o["strict_pct"], 1), round(o["cumulative_pct"], 1),
                        "", "", "", "", "", "", "", "", "", "", "", "", "", ""])

    # --- write stats_report.md ----------------------------------------------------
    lines = ["# Statistics report (task 5)", ""]
    lines.append("## Observed strict/cumulative %, chance-corrected index, null, bootstrap CI\n")
    lines.append("| Group | Denom (ac, excl. neutral) | Strict % | Strict 95% CI | Cumulative % | "
                  "Cumulative 95% CI | Marginal baseline (strict) | Chance-corrected index (strict) | "
                  "Null mean / p95 (strict) | Empirical p-value |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for g in GROUP_DISTRICTS:
        o = out_group[g]; b = boot[g]; bl = baseline[g]; nv = null[g]
        s_lo, s_hi = np.nanpercentile(b["strict"], [2.5, 97.5])
        c_lo, c_hi = np.nanpercentile(b["cumulative"], [2.5, 97.5])
        pval = float((nv >= o["strict_pct"]).mean())
        kappa_s = chance_corrected(o["strict_pct"], bl["strict_pct"])
        lines.append(f"| {g} | {o['denom']*grids.px_acres:,.0f} | {o['strict_pct']:.1f}% | "
                      f"[{s_lo:.1f}, {s_hi:.1f}] | {o['cumulative_pct']:.1f}% | [{c_lo:.1f}, {c_hi:.1f}] | "
                      f"{bl['strict_pct']:.1f}% | {kappa_s:.2f} | {nv.mean():.1f}% / {np.percentile(nv,95):.1f}% | "
                      f"{pval:.4f} |")
    lines.append("")
    lines.append("## Cumulative (permitted-use) match: baseline, chance-corrected index, null\n")
    lines.append("| Group | Cumulative % | 95% CI | Marginal baseline | Chance-corrected index | "
                  "Null mean / p95 | Empirical p-value |")
    lines.append("|---|---|---|---|---|---|---|")
    for g in GROUP_DISTRICTS:
        o = out_group[g]; b = boot[g]; bl = baseline[g]; nc = null_cum[g]
        c_lo, c_hi = np.nanpercentile(b["cumulative"], [2.5, 97.5])
        lines.append(f"| {g} | {o['cumulative_pct']:.1f}% | [{c_lo:.1f}, {c_hi:.1f}] | {bl['cumulative_pct']:.1f}% | "
                      f"{chance_corrected(o['cumulative_pct'], bl['cumulative_pct']):.2f} | "
                      f"{nc.mean():.1f}% / {np.percentile(nc, 95):.1f}% | "
                      f"{float((nc >= o['cumulative_pct']).mean()):.4f} |")
    lines.append("")
    lines.append("## By district (strict / cumulative %, corrected denominator)\n")
    lines.append("| District | Strict % | Cumulative % |")
    lines.append("|---|---|---|")
    for k in DIST:
        o = out_district[k]
        lines.append(f"| {k} | {o['strict_pct']:.1f}% | {o['cumulative_pct']:.1f}% |")
    lines.append("")
    lines.append("## Where the plan missed (share of each group's denominator landing in another 2026 group)\n")
    lines.append("| 1947 group | Residential | Commercial | Industrial | Public/Park |")
    lines.append("|---|---|---|---|---|")
    for g, shares in offdiag.items():
        lines.append(f"| {g} | {shares['Residential']:.1f}% | {shares['Commercial']:.1f}% | "
                      f"{shares['Industrial']:.1f}% | {shares['Public/Park']:.1f}% |")
    lines.append("")
    lines.append("## Sensitivity: headline strict % per group across input/toggle variants\n")
    keys = sorted({k for r in sensitivity_rows for k in r})
    lines.append("| " + " | ".join(keys) + " |")
    lines.append("|" + "---|" * len(keys))
    for r in sensitivity_rows:
        lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    lines.append("")
    lines.append(f"## Methodology notes\n")
    lines.append(f"- Toroidal-shift null: {config.N_NULL_SHIFTS} shifts, magnitude "
                  f"{config.NULL_SHIFT_MIN_FT:.0f}-{config.NULL_SHIFT_MAX_FT:.0f} ft, random direction, "
                  f"np.roll wrap (toroidal); the extent mask and 2026 parcels are held fixed, only the "
                  f"1947 zone raster is shifted. Wrapping can pull in content from the opposite edge of "
                  f"the raster (an approximation noted in toroidal_null()'s docstring).")
    lines.append(f"- Distance-decay: strict % averaged over {config.DECAY_N_DIRECTIONS} directions at each "
                  f"magnitude from 0 to {config.DECAY_MAX_FT:.0f} ft in steps of {config.DECAY_STEP_FT:.0f} ft. "
                  f"See distance_decay.csv / distance_decay.png.")
    lines.append(f"- Spatial block bootstrap: {config.BOOTSTRAP_REPS} reps, resampling "
                  f"{config.BOOTSTRAP_BLOCK_FT:.0f} ft square blocks (~{block_px}x{block_px} px) with "
                  f"replacement across the {n_blocks} blocks covering the extent's bounding grid.")
    with open("stats_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("wrote stats_summary.csv, stats_report.md, distance_decay.csv, distance_decay.png")
    print(open("stats_summary.csv").read())


if __name__ == "__main__":
    main()
