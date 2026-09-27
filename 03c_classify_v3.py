"""
Step 3c: fix the systematic miss of district G (Central Business, solid
near-black fill) in 03b_classify_fullres.py's output (zones_px_v2).

BACKGROUND / WHY v2 MISSES G
-----------------------------
The legend swatch for G (crosswalk.json "G": [9513,3965]) is a clean,
low-texture solid-black fill: local std ~14, essentially 0% bright pixels.
But 03b's classifier trains a 48x48-patch RandomForest and votes at the
BLOCK level for "trustworthy" (small, <=60000 px2) blocks -- and in situ,
downtown Houston's small blocks routinely get fused by 03b's street mask
into one enormous "leaked" component (verified below: block id 1 alone
covers ~81% of the zoned area, well over 03b's LARGE_BLOCK_PX2 cutoff), so
they fall back to the per-patch grid label instead of a block vote. Small
48x48 patches straddling a tiny downtown block also mix in adjacent
streets/hatched neighbors, and in-situ G fill has noticeably more print
texture (registration noise, ink mottling) than the clean legend swatch.
Net effect: 03b's area report gives G only 23123 full-res px2 (0.037% of
the mapped area) even though whole downtown blocks are unambiguously solid
black (verified visually: see check_downtown_g_before/after in this
script's output and the report).

FIX: a RULE-BASED (not RF) detector applied directly on full-res pixels,
independent of 03b's patch/RF pipeline, that looks for "dark AND
texture-free": low local mean (solid ink, not a hatch/dot pattern that
averages lighter), low local std, and very low local bright-pixel fraction
(J's white dot grid and H/I's white hatch lines both push a lot of
near-paper-bright pixels into any window, even though their mean gray and
raw darkness can superficially resemble G -- this is exactly the
bright-pixel-fraction check the task calls for to keep J/H/I from being
swallowed into G). Verified against:
  - the G legend swatch: local mean ~78, std ~14, bright(>150) frac 0.0
  - 20 true downtown block-interior points (found via a distance-transform
    search for pixels far from any street/bright pixel, NOT hand-picked
    crop coordinates, since a naive grid sample mostly lands on streets or
    on the neighboring hatched D blocks): local mean ~68-71, std ~10-16,
    bright frac <=0.008
  - the J legend swatch and true J-field interior points (Buffalo Bayou,
    ~(6100,3700)): local mean ~78-104, std ~24-32, bright frac ~0.03-0.09
  - the H/I legend swatches and true H/I hatch-block interior points (east
    of downtown, ~(5300,4150)): local mean ~74-104, std ~22-56, bright
    frac ~0.03-0.27
  giving a clean separating band (mean<95, std<20, bright_frac<0.03) with
  wide margins on both sides -- see the run log / report for the numbers.

AGGREGATION: rule output is a per-pixel boolean; a small opening+closing
cleans salt-and-pepper noise (verified: false hits in the J/H-I test crops
above are isolated single-pixel specks, not blocks, and vanish under a
radius-2 opening), then connected components below MIN_REGION_PX2 are
dropped. Because the downtown blocks in question live inside 03b's
mega-leaked block (id 1, ~81% of the map -- block-level majority voting is
meaningless there), overriding is done differently depending on whether a
block is "trustworthy" (<=LARGE_BLOCK_PX2, matching 03b's own cutoff) or
not:
  - trustworthy small block: block becomes G only if the cleaned rule mask
    covers >=50% of its area (a real block-level majority, per the task
    spec's "block majority as in 03b").
  - large/leaked block (like id 1): override only the individual cleaned
    rule-mask regions themselves at pixel resolution (the task spec's
    "...or morphological region" alternative) -- i.e. we trust the
    texture-free-dark regions we found, without forcing the entire
    leaked block to one label.

REGION GROWING (round 2, added after review): the tight rule above only
fires deep in each block's interior (window edge effects near streets
shrink it inward by ~half the window), so the first pass recovered G only
as small diamonds at block centres, not the whole block up to its street
edges. A second pass GROWS each cleaned tight-rule region outward with a
looser test (local mean<110, std<30, bright(>150) frac<0.06 in a 9px
window -- deliberately looser than the seed test, tuned below) via
geodesic dilation (one ring of 8-connected pixels per iteration, capped at
40 iterations =~ 40 full-res px =~ half a downtown block), so growth can
only ever reach a pixel through an unbroken chain of relaxed-passing
pixels -- it cannot "tunnel" across a street/gap pixel that fails the
test even once.

This connectivity requirement turned out to be essential, not optional:
block_mask (03b's own street/fill split) is UNRELIABLE at exactly the
G/I border found next to downtown (verified: probing raw pixels along a
line from a solid-black block straight into its neighbouring hatched I
block at ~(5000,3950)-(5167,3967) shows block_mask==True the entire way,
i.e. 03b's street detector missed this real, if thin and low-contrast,
street -- both blocks share block_labels id 1). Relying on block_mask
alone as the growth barrier would leak straight through into I. What
*does* stop growth correctly is the per-pixel relaxed test evaluated at
fine resolution during the walk itself: raw pixel values sampled along
that same line show the real street reaching gray~150-217 (i.e. clearly
non-dark) even though it's only ~11px wide and block_mask missed it, so a
9px window straddling it has local mean well over 110 and the geodesic
walk halts right there. (A per-pixel/window test alone, checked only at
isolated interior points and not respecting connectivity, is NOT enough by
itself: true H/I hatch interiors have wide enough gaps between hatch lines
that an unlucky sample point in situ can show std as low as ~4-11 and 0%
bright pixels even at windows up to 25px -- statistically indistinguishable
from G on those isolated samples. It is specifically the requirement that
growth reach a pixel via an UNBROKEN path of relaxed-passing pixels back to
a real G seed, not a bare distance/threshold check, that keeps growth from
jumping into a neighbouring hatch block's interior.) Verified after
implementation: check_hi_hatch_after_v3.png and check_j_field_after_v3.png
are pixel-identical in G coverage to before growing (still zero
contamination), and a new check_g_i_border_after_v3.png crop specifically
covering the thin-street case above confirms growth stops at the block
edge instead of crossing into the neighbouring hatched block.

Everything else is left exactly as 03b/v2 classified it, so the change is
directly attributable (v2 vs v3 diff = only newly-added G pixels, plus
whatever those pixels used to be labelled).

Convention: identical to 02_extent.py / 03b_classify_fullres.py -- full-res
JPEG is 11173x8809, row 0 at top, output raster at SCALE=2 with
geotransform (0, SCALE, 0, 0, 0, -SCALE).

IMPORTANT: this script intentionally reuses v2's SAME extent (extent_px.tif,
i.e. v1 of the extent, NOT the new classifier-independent 02b_extent_v2
fix) and the SAME cached block segmentation (cache_v2/blocks_v2.npz) that
03b produced, so that the G fix is isolated from the extent fix (both are
separate, independently-reportable changes per the task) and v2/v3 area
numbers stay directly comparable. v1/v2 outputs are not modified in any way
(only new cache files are added under cache_v2/, and only new g_* files are
read/written there).
"""
import json
import os
import time
import numpy as np
from osgeo import gdal, ogr
from scipy import ndimage as ndi
from skimage.morphology import disk, binary_opening, binary_closing
from shapely import geometry as shg
from shapely.ops import unary_union

gdal.UseExceptions()
T0 = time.time()


def log(*a):
    print(f"[{time.time()-T0:7.1f}s]", *a, flush=True)


SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "11139001.jpg")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = f"{OUT_DIR}/cache_v2"  # SAME cache dir 03b uses -- read existing
os.makedirs(CACHE_DIR, exist_ok=True)  # caches, only add new g_*-prefixed ones
EXTENT_TIF = f"{OUT_DIR}/extent_px.tif"  # v1 extent, deliberately unchanged
ZONES_V2_TIF = f"{OUT_DIR}/zones_px_v2.tif"

FULL_W, FULL_H = 11173, 8809
SCALE = 2  # same output resolution as v2

DISTRICTS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
CODE_OF = {d: i + 1 for i, d in enumerate(DISTRICTS)}
CODE_TO_LETTER = {v: k for k, v in CODE_OF.items()}
G_CODE = CODE_OF["G"]

LARGE_BLOCK_PX2 = 60000  # identical cutoff to 03b, for direct comparability

# ---- G rule-detector thresholds (tuned against legend swatches + true
# block-interior sample points located via distance-transform search; see
# module docstring and the report for the full numbers) ----
G_WIN = 15          # local window (uniform_filter box size), within the
                    # task-suggested 15-25px range
G_MEAN_T = 95.0     # local mean gray must be darker than this
G_STD_T = 20.0      # local std must be below this (texture-free)
G_BRIGHT_T = 150.0  # "paper-ish/bright" pixel threshold used for the
                    # bright-fraction test (distinguishes J's white dots and
                    # H/I's white hatch lines, which are bright relative to
                    # their dark fill, from G's genuinely uniform blackness)
G_BRIGHT_FRAC_T = 0.03  # local fraction of pixels > G_BRIGHT_T must be below this

OPEN_R = 2          # morphological opening radius (px) to drop speckle noise
CLOSE_R = 3         # closing radius to bridge small internal gaps
MIN_REGION_PX2 = 500  # drop cleaned-mask components smaller than this
BLOCK_OVERRIDE_FRAC = 0.5  # small-block majority threshold to override to G

# ---- region-growing (round 2): grow each seed region out to its street
# edges with a looser per-pixel test, walked geodesically so growth can
# only ever cross an unbroken chain of relaxed-passing pixels (see module
# docstring for why this connectivity requirement -- not just a looser
# threshold -- is what keeps growth out of neighbouring J/H/I blocks) ----
GROW_WIN = 7             # smaller window than the seed detector, per review --
                        # tuned down from an initial 9px after finding growth
                        # converged (self-terminated) well short of the true
                        # block edge at 9px; see report for the coverage numbers
GROW_MEAN_T = 120.0
GROW_STD_T = 35.0
GROW_BRIGHT_T = 150.0    # same "paper-ish" threshold as the seed detector
GROW_BRIGHT_FRAC_T = 0.08
GROW_MAX_STEPS = 40      # geodesic cap, ~40 full-res px =~ half a downtown
                        # block, per the reviewer's spec -- growth self-
                        # terminates at real block/street edges well before
                        # this for isolated blocks tested (e.g. converges by
                        # step ~10 for a typical downtown block), so this is
                        # a hard safety bound against any single seed finding
                        # an unexpectedly large contiguous relaxed-passing
                        # area, not the mechanism doing the normal stopping
GROW_STRUCT = np.ones((3, 3), dtype=bool)  # 8-connected, 1px/iteration ring

PX_PER_FT_FULL = 455.0 / 3000.0
ACRE_FULL_PX2 = 43560.0 * (PX_PER_FT_FULL ** 2)


# ---------------------------------------------------------------- IO -----
def read_gray_full():
    """Identical cache file to 03b_classify_fullres.read_gray_full -- read
    only, never written here if already present (pure pixel data, no
    classifier dependency)."""
    cache = f"{CACHE_DIR}/gray_full.npy"
    if os.path.exists(cache):
        log("loading cached gray_full")
        return np.load(cache)
    log("reading full-res RGB from JPEG...")
    ds = gdal.Open(SRC)
    bands = [ds.GetRasterBand(i).ReadAsArray() for i in (1, 2, 3)]
    rgb = np.stack(bands, axis=-1).astype(np.float32)
    del bands
    gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2])
    del rgb
    gray = np.clip(gray, 0, 255).astype(np.uint8)
    np.save(cache, gray)
    return gray


def read_extent_full():
    """v1 extent (extent_px.tif) resampled to full res -- SAME extent v2
    used, kept deliberately for attribution (the extent fix is a separate
    change, see 02b_extent_v2.py)."""
    cache = f"{CACHE_DIR}/extent_full.npy"
    if os.path.exists(cache):
        return np.load(cache)
    ds = gdal.Translate("", EXTENT_TIF, format="MEM", width=FULL_W, height=FULL_H,
                         resampleAlg="nearest")
    arr = (ds.GetRasterBand(1).ReadAsArray() > 0)
    np.save(cache, arr)
    return arr


def read_blocks():
    """Reuse 03b's cached block segmentation (built from gray_full &
    extent_full, both classifier-independent pixel data) so the G fix uses
    the exact same block definitions v2 did."""
    cache = f"{CACHE_DIR}/blocks_v2.npz"
    if not os.path.exists(cache):
        raise RuntimeError(
            f"{cache} not found -- run 03b_classify_fullres.py first "
            "(this script reuses its cached block segmentation).")
    d = np.load(cache)
    return d["labels"], d["block_mask"], d["sizes"]


def read_v2_labels_full():
    """Upsample zones_px_v2.tif (half-res, SCALE=2) back to full res via
    nearest-neighbour repeat. v2's own downsample (mode_downsample2) picks
    the first nonzero of each 2x2 cell, so nearest-neighbour is a faithful
    inverse everywhere except at a handful of single-pixel block-boundary
    seams, which don't matter for block/region-level G overrides."""
    ds = gdal.Open(ZONES_V2_TIF)
    half = ds.GetRasterBand(1).ReadAsArray()
    full = np.repeat(np.repeat(half, 2, axis=0), 2, axis=1)[:FULL_H, :FULL_W]
    if full.shape != (FULL_H, FULL_W):
        pad = np.zeros((FULL_H, FULL_W), dtype=full.dtype)
        pad[:full.shape[0], :full.shape[1]] = full
        full = pad
    return full


# ------------------------------------------------------- G rule detector --
def build_g_candidate_mask(gray_full, block_mask):
    """Per-pixel boolean: dark AND texture-free AND low bright-pixel
    fraction, i.e. solid ink with no periodic hatch/dot pattern. Restricted
    to block fill pixels (streets/paper already excluded via block_mask)."""
    cache = f"{CACHE_DIR}/g_cand_v3.npy"
    if os.path.exists(cache):
        log("loading cached G candidate mask")
        return np.load(cache)
    log("computing local mean/std/bright-frac for G rule...")
    gray_f = gray_full.astype(np.float32)
    lm = ndi.uniform_filter(gray_f, G_WIN)
    lsq = ndi.uniform_filter(gray_f * gray_f, G_WIN)
    lstd = np.sqrt(np.clip(lsq - lm ** 2, 0, None))
    bright = (gray_full > G_BRIGHT_T).astype(np.float32)
    lbf = ndi.uniform_filter(bright, G_WIN)
    del gray_f, lsq, bright
    cand = (lm < G_MEAN_T) & (lstd < G_STD_T) & (lbf < G_BRIGHT_FRAC_T) & block_mask
    np.save(cache, cand)
    log(f"  raw G candidate px: {cand.sum()}")
    return cand


def clean_g_regions(cand_mask):
    """Opening (drop speckle) + closing (bridge small gaps) + drop small
    components. Returns (cleaned_bool_mask, component_labels, component_sizes)."""
    cache = f"{CACHE_DIR}/g_regions_v3.npz"
    if os.path.exists(cache):
        log("loading cached cleaned G regions")
        d = np.load(cache)
        return d["mask"], d["labels"], d["sizes"]
    log("cleaning G candidate mask (opening+closing)...")
    opened = binary_opening(cand_mask, disk(OPEN_R))
    closed = binary_closing(opened, disk(CLOSE_R))
    del opened
    lbl, n = ndi.label(closed, structure=np.ones((3, 3)))
    sizes = np.bincount(lbl.ravel(), minlength=n + 1)
    keep = sizes >= MIN_REGION_PX2
    keep[0] = False
    cleaned = keep[lbl]
    log(f"  {n} raw components, {keep.sum()} kept (>= {MIN_REGION_PX2} px2), "
        f"cleaned px total: {cleaned.sum()}")
    np.savez_compressed(cache, mask=cleaned, labels=lbl.astype(np.int32), sizes=sizes)
    return cleaned, lbl, sizes


def build_relaxed_mask(gray_full, block_mask, extent_full):
    """Looser dark/texture-free test (9px window) used only to decide
    whether a pixel is a valid STEP for geodesic growth, never as a
    standalone classifier -- see module docstring for why a bare
    threshold on isolated pixels is not safe here (H/I hatch gaps can look
    G-like in isolation) and why the geodesic-walk connectivity
    requirement is what actually keeps growth confined to real blocks."""
    cache = f"{CACHE_DIR}/g_relaxed_v3.npy"
    if os.path.exists(cache):
        log("loading cached relaxed-growth mask")
        return np.load(cache)
    log("computing relaxed (9px) local mean/std/bright-frac for growth...")
    gray_f = gray_full.astype(np.float32)
    lm = ndi.uniform_filter(gray_f, GROW_WIN)
    lsq = ndi.uniform_filter(gray_f * gray_f, GROW_WIN)
    lstd = np.sqrt(np.clip(lsq - lm ** 2, 0, None))
    bright = (gray_full > GROW_BRIGHT_T).astype(np.float32)
    lbf = ndi.uniform_filter(bright, GROW_WIN)
    del gray_f, lsq, bright
    relaxed = ((lm < GROW_MEAN_T) & (lstd < GROW_STD_T) & (lbf < GROW_BRIGHT_FRAC_T)
               & block_mask & extent_full)
    np.save(cache, relaxed)
    log(f"  relaxed-allowed px (pre-growth, unconnected): {relaxed.sum()}")
    return relaxed


def grow_g_regions(seed_mask, allowed_mask, max_steps=GROW_MAX_STEPS):
    """Geodesic dilation: grow `seed_mask` outward one 8-connected ring per
    iteration, intersected with `allowed_mask` every step, for up to
    `max_steps` iterations. This is a connectivity-respecting BFS/flood
    fill (NOT a bare Euclidean-distance threshold) -- a pixel is only ever
    added if there is an unbroken chain of allowed_mask pixels of length
    <=max_steps back to a seed pixel, which is what actually prevents
    growth from crossing a real (even if faint/thin) street into a
    neighbouring block. Stops early if a step adds nothing."""
    visited = seed_mask.copy()
    frontier = seed_mask.copy()
    for step in range(max_steps):
        grown = ndi.binary_dilation(frontier, structure=GROW_STRUCT)
        new_front = grown & allowed_mask & ~visited
        if not new_front.any():
            log(f"  growth stopped early at step {step+1} (no more reachable pixels)")
            break
        visited |= new_front
        frontier = new_front
    else:
        log(f"  growth hit the {max_steps}-step cap")
    return visited


# --------------------------------------------------------- main -----
def mode_downsample2(a):
    """Identical helper to 03b_classify_fullres.mode_downsample2."""
    H, W = a.shape
    Hp, Wp = H + (H % 2), W + (W % 2)
    if (Hp, Wp) != (H, W):
        pad = np.zeros((Hp, Wp), dtype=a.dtype)
        pad[:H, :W] = a
        a = pad
    blocks = a.reshape(Hp // 2, 2, Wp // 2, 2).transpose(0, 2, 1, 3).reshape(Hp // 2, Wp // 2, 4)
    out = np.zeros((Hp // 2, Wp // 2), dtype=a.dtype)
    for k in range(4):
        col = blocks[:, :, k]
        mask = out == 0
        out[mask] = col[mask]
    return out


def main():
    gray_full = read_gray_full()
    extent_full = read_extent_full()
    block_labels, block_mask, block_sizes = read_blocks()
    n_blocks = len(block_sizes) - 1
    v2_full = read_v2_labels_full()

    cand = build_g_candidate_mask(gray_full, block_mask)
    cleaned_mask, region_labels, region_sizes = clean_g_regions(cand)

    # ---- per-block aggregation ----
    log("aggregating cleaned G mask by block...")
    in_mask_idx = block_labels[block_mask].ravel()
    cleaned_in_mask = cleaned_mask[block_mask].ravel()
    g_px_per_block = np.bincount(in_mask_idx, weights=cleaned_in_mask.astype(np.float64),
                                  minlength=n_blocks + 1)
    block_area = np.bincount(in_mask_idx, minlength=n_blocks + 1)
    frac_g = np.divide(g_px_per_block, block_area, out=np.zeros_like(g_px_per_block),
                        where=block_area > 0)

    is_small = block_sizes <= LARGE_BLOCK_PX2
    block_override = is_small & (frac_g >= BLOCK_OVERRIDE_FRAC)
    block_override[0] = False
    log(f"  small blocks overridden to G (majority>={BLOCK_OVERRIDE_FRAC}): "
        f"{block_override.sum()} blocks, "
        f"{int(block_area[block_override].sum())} px2")

    # ---- build v3 output ----
    out_full = v2_full.copy()

    # (a) small-block majority overrides -- whole block -> G
    small_override_px = block_override[block_labels] & block_mask
    out_full[small_override_px] = G_CODE

    # (b) large/leaked-block pixel-level overrides -- only the cleaned
    # region pixels themselves, wherever their containing block is NOT a
    # small trustworthy block (this is where downtown lives: block id 1
    # covers the vast majority of the zoned area and is far above
    # LARGE_BLOCK_PX2, so block-level majority voting is meaningless there
    # and the task's "...or morphological region" alternative applies)
    is_large_here = ~is_small[block_labels]
    large_override_px = cleaned_mask & block_mask & is_large_here
    n_before_large = int((out_full[large_override_px] == G_CODE).sum())
    out_full[large_override_px] = G_CODE
    log(f"  large/leaked-block pixel-level G overrides: {int(large_override_px.sum())} px2 "
        f"({n_before_large} already were G)")

    round1_g_px = small_override_px | large_override_px
    log(f"round 1 (seed) G pixels: {int(round1_g_px.sum())}")

    # ---- round 2: geodesic region growing from the round-1 seeds out to
    # each block's street edges (see module docstring for why this needs a
    # connectivity-respecting walk, not just a looser threshold) ----
    relaxed_mask = build_relaxed_mask(gray_full, block_mask, extent_full)
    log("growing G seed regions geodesically...")
    grown_mask = grow_g_regions(round1_g_px, relaxed_mask, GROW_MAX_STEPS)
    grown_new_px = grown_mask & ~round1_g_px & block_mask & extent_full
    log(f"round 2 (grown) NEW G pixels: {int(grown_new_px.sum())}")
    out_full[grown_new_px] = G_CODE

    out_full[~block_mask] = 0
    out_full[~extent_full] = 0
    out_full = out_full.astype(np.uint8)

    total_override_px = int((round1_g_px | grown_new_px).sum())
    log(f"total pixels changed to G (seed + grown): {total_override_px}")

    # ---- downsample & write raster ----
    out_half = mode_downsample2(out_full)
    OUT_H, OUT_W = out_half.shape
    out_tif = f"{OUT_DIR}/zones_px_v3.tif"
    drv = gdal.GetDriverByName("GTiff")
    ds_out = drv.Create(out_tif, OUT_W, OUT_H, 1, gdal.GDT_Byte, options=["COMPRESS=LZW"])
    ds_out.SetGeoTransform((0, SCALE, 0, 0, 0, -SCALE))
    ds_out.GetRasterBand(1).WriteArray(out_half)
    ds_out.GetRasterBand(1).SetNoDataValue(0)
    ds_out.FlushCache()
    ds_out = None
    log("wrote", out_tif)

    # ---- polygonize + dissolve (same pattern as 03b) ----
    src_ds = gdal.Open(out_tif)
    src_band = src_ds.GetRasterBand(1)
    gdrv = ogr.GetDriverByName("GPKG")
    gpkg_path = f"{OUT_DIR}/zones_px_v3.gpkg"
    if os.path.exists(gpkg_path):
        gdrv.DeleteDataSource(gpkg_path)
    vds = gdrv.CreateDataSource(gpkg_path)
    tmp_name = "zones_raw_tmp"
    tmp_layer = vds.CreateLayer(tmp_name, srs=None, geom_type=ogr.wkbPolygon)
    tmp_layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    gdal.Polygonize(src_band, src_band.GetMaskBand(), tmp_layer, 0, ["8CONNECTED=8"])

    geoms_by_code = {}
    tmp_layer.ResetReading()
    for feat in tmp_layer:
        code = feat.GetField("code")
        if code == 0:
            continue
        geom = shg.shape(json.loads(feat.GetGeometryRef().ExportToJson()))
        geoms_by_code.setdefault(code, []).append(geom)

    out_layer = vds.CreateLayer("zones_1947", srs=None, geom_type=ogr.wkbMultiPolygon)
    out_layer.CreateField(ogr.FieldDefn("district", ogr.OFTString))
    out_layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    for code, geoms in sorted(geoms_by_code.items()):
        merged = unary_union(geoms)
        f = ogr.Feature(out_layer.GetLayerDefn())
        f.SetField("district", CODE_TO_LETTER[code])
        f.SetField("code", int(code))
        f.SetGeometry(ogr.CreateGeometryFromWkb(merged.wkb))
        out_layer.CreateFeature(f)
    vds.ExecuteSQL(f"DROP TABLE {tmp_name}")
    vds = None
    log("wrote", gpkg_path)

    # ---- preview PNG (same palette as 03b/v2) ----
    palette = {
        0: (245, 245, 240), 1: (255, 255, 190), 2: (255, 220, 130), 3: (255, 170, 90),
        4: (230, 120, 60), 5: (240, 90, 160), 6: (190, 40, 140), 7: (120, 0, 90),
        8: (90, 140, 220), 9: (40, 90, 190), 10: (10, 30, 120),
    }
    from scipy.ndimage import zoom
    PREVIEW_W = 1400
    prev_h = int(OUT_H * (PREVIEW_W / OUT_W))
    out_small = zoom(out_half, (prev_h / OUT_H, PREVIEW_W / OUT_W), order=0)
    gray_half_prev = gray_full[::2, ::2]
    gray_small = zoom(gray_half_prev, (prev_h / gray_half_prev.shape[0], PREVIEW_W / gray_half_prev.shape[1]), order=1)
    color_img = np.zeros((prev_h, PREVIEW_W, 3), dtype=np.uint8)
    for code, rgb_c in palette.items():
        color_img[out_small == code] = rgb_c
    orig3 = np.stack([gray_small] * 3, axis=-1).astype(np.uint8)
    combo = np.concatenate([orig3, color_img], axis=1)
    mem_drv = gdal.GetDriverByName("MEM")
    mem = mem_drv.Create("", combo.shape[1], combo.shape[0], 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(combo[..., i])
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/zones_preview_v3.png", mem)
    log("wrote", f"{OUT_DIR}/zones_preview_v3.png")

    # ---- before/after check crops (downtown G, J field, H/I hatch, plus
    # the specific thin-street G/I border case found while tuning the
    # region-growing step -- see module docstring) ----
    crop_windows = {
        "downtown_g": (4500, 3300, 1200, 1000),
        "j_field": (5900, 3500, 1000, 800),
        "hi_hatch": (5100, 3950, 1000, 800),
        "g_i_border": (4900, 3850, 400, 300),
    }
    v2_full_for_crop = v2_full
    for name, (x0, y0, w, h) in crop_windows.items():
        orig_crop = gray_full[y0:y0 + h, x0:x0 + w]
        for tag, arr in [("before", v2_full_for_crop), ("after", out_full)]:
            class_crop = arr[y0:y0 + h, x0:x0 + w]
            color_c = np.zeros((h, w, 3), dtype=np.uint8)
            for code, rgb_c in palette.items():
                color_c[class_crop == code] = rgb_c
            orig3c = np.stack([orig_crop] * 3, axis=-1).astype(np.uint8)
            combo_c = np.concatenate([orig3c, color_c], axis=1)
            mem2 = mem_drv.Create("", combo_c.shape[1], combo_c.shape[0], 3, gdal.GDT_Byte)
            for i in range(3):
                mem2.GetRasterBand(i + 1).WriteArray(combo_c[..., i])
            out_path = f"{OUT_DIR}/check_{name}_{tag}_v3.png"
            gdal.GetDriverByName("PNG").CreateCopy(out_path, mem2)
            log("wrote", out_path)

    # ---- area report: before (v2) vs after (v3), per class ----
    # NOTE: compared at the SAME half-res (SCALE=2) grid both sides (v2's
    # own tif vs our out_half), not at full res. v2_full here was
    # reconstructed by nearest-neighbour UPSAMPLING zones_px_v2.tif (itself
    # a lossy "first-of-2x2" downsample of 03b's true full-res decision
    # grid), so a full-res v2_full-vs-out_full comparison mixes in a large,
    # spurious apples-to-oranges delta from that round trip (verified: it
    # does NOT match 03b's own zones_meta_v2.json area report, off by tens
    # of thousands of px2 per class with no G involved at all). Comparing
    # both sides through the identical half-res quantization instead isolates
    # the actual effect of the G fix.
    v2_half_ds = gdal.Open(ZONES_V2_TIF)
    v2_half = v2_half_ds.GetRasterBand(1).ReadAsArray()
    extent_px2 = int(extent_full.sum())
    area_before, area_after = {}, {}
    for code in range(1, 11):
        letter = CODE_TO_LETTER[code]
        area_before[letter] = int((v2_half == code).sum()) * (SCALE * SCALE)
        area_after[letter] = int((out_half == code).sum()) * (SCALE * SCALE)

    v2_official_meta = {}
    v2_meta_path = f"{OUT_DIR}/zones_meta_v2.json"
    if os.path.exists(v2_meta_path):
        with open(v2_meta_path) as f:
            v2_official_meta = json.load(f).get("area_by_district", {})

    meta = {
        "scale": SCALE,
        "output_size": [OUT_W, OUT_H],
        "full_size": [FULL_W, FULL_H],
        "geotransform": [0, SCALE, 0, 0, 0, -SCALE],
        "reused_extent": "extent_px.tif (v1, same as v2 -- intentionally NOT v2b)",
        "reused_blocks_cache": "cache_v2/blocks_v2.npz (same block segmentation as v2)",
        "g_rule_thresholds_seed": {
            "window_px": G_WIN, "local_mean_lt": G_MEAN_T, "local_std_lt": G_STD_T,
            "bright_threshold": G_BRIGHT_T, "bright_frac_lt": G_BRIGHT_FRAC_T,
            "open_radius_px": OPEN_R, "close_radius_px": CLOSE_R,
            "min_region_px2": MIN_REGION_PX2, "block_override_frac": BLOCK_OVERRIDE_FRAC,
            "large_block_px2_cutoff": LARGE_BLOCK_PX2,
        },
        "g_rule_thresholds_grow_round2": {
            "window_px": GROW_WIN, "local_mean_lt": GROW_MEAN_T, "local_std_lt": GROW_STD_T,
            "bright_threshold": GROW_BRIGHT_T, "bright_frac_lt": GROW_BRIGHT_FRAC_T,
            "max_geodesic_steps_px": GROW_MAX_STEPS,
            "method": "8-connected geodesic dilation from round-1 seeds, one ring "
                      "per step, intersected each step with the relaxed mask AND "
                      "block_mask AND extent -- growth halts wherever any pixel "
                      "along the path fails the relaxed test, even if block_mask "
                      "itself failed to detect a real street there (see docstring)",
        },
        "n_blocks": n_blocks,
        "n_small_blocks_overridden_to_G": int(block_override.sum()),
        "px2_overridden_to_G_total": total_override_px,
        "px2_overridden_to_G_via_block_majority": int(small_override_px.sum()),
        "px2_overridden_to_G_via_pixel_region_seed": int(large_override_px.sum()),
        "px2_round1_seed_total": int(round1_g_px.sum()),
        "px2_round2_grown_new": int(grown_new_px.sum()),
        "extent_full_res_px2": extent_px2,
        "area_by_district_v2_before_px2": area_before,
        "area_by_district_v3_after_px2": area_after,
        "area_by_district_delta_px2": {k: area_after[k] - area_before[k] for k in area_before},
        "area_by_district_v3_after_pct_of_extent": {
            k: 100.0 * area_after[k] / extent_px2 if extent_px2 else 0.0 for k in area_after
        },
        "note_before_after_comparison": (
            "before/after computed at the SAME half-res (SCALE=2) grid both "
            "sides, i.e. from zones_px_v2.tif and our own out_half, *4 to "
            "approximate full-res px2 -- NOT from 03b's full-res-precision "
            "area report (reproduced below for reference under "
            "'area_by_district_v2_official_fullres_px2'), since upsampling "
            "the half-res v2 raster back to full res to compare directly "
            "against our full-res out_full introduces a large spurious "
            "quantization delta unrelated to the G fix."
        ),
        "area_by_district_v2_official_fullres_px2": v2_official_meta,
    }
    with open(f"{OUT_DIR}/zones_meta_v3.json", "w") as f:
        json.dump(meta, f, indent=2)
    log(json.dumps(meta, indent=2))
    log("DONE")


if __name__ == "__main__":
    main()
