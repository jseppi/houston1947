"""
Step 3b: classify zoning districts (A..J) from texture/tone, at FULL resolution.

v1 (03_classify.py) worked at 1/4-scale overview -> destroyed hatch pitch/J-dot
detail, mislabelled ~69% of the city "A", confused J (dark+white dots) with
G (solid near-black). This version stays at full resolution (11173x8809) for
ALL steps, including block/street segmentation (an earlier internal attempt at
half-res street segmentation, and even full-res segmentation using plain
brightness, catastrophically under-segmented: cream A/B residential fill is
itself mostly bright, indistinguishable from streets by brightness alone, and
any single-pixel break anywhere in the street network -- very common, e.g.
under street-name text printed on top of the street -- merges the two sides
into one connected blob; a naive brightness+opening approach ends up fusing
~80% of the mapped area into one "block"). Two things fix this:
  (a) streets are identified by BRIGHT *and* locally SMOOTH (low local std --
      real fill patterns have texture/hatch even where they're bright, like
      02_extent.py's paper-vs-zoned test, just at street-width scale), and
      a small morphological CLOSING bridges the text-interruption gaps before
      an OPENING removes small isolated bright dot/hatch noise.
  (b) even so, some components still leak into large multi-block blobs. We
      don't force a single label onto those: components above a size
      threshold are treated as untrustworthy for block-level majority voting,
      and get the smoothed per-patch (grid) label directly instead. Only
      "small enough to plausibly be one real block" components get a single
      area-weighted-majority label.

Convention: identical to 02_extent.py / 03_classify.py -- full-res JPEG is
11173x8809, row 0 at top. Output raster is written at SCALE=2 (half-res grid,
allowed by the task spec to keep file size down) using geotransform
(0, SCALE, 0, 0, 0, -SCALE):
    map_x =  col * SCALE
    map_y = -row * SCALE
All classification decisions are made at full resolution; only the final
raster is downsampled (mode of each 2x2 full-res cell).

Pipeline:
  1. Read full-res RGB -> grayscale (uint8). Read extent mask (from
     extent_px.tif, scale 8) resampled to full res.
  2. Full-res street mask: bright & locally-smooth (local std over a 9px
     window) -> binary closing (bridge text gaps) -> binary opening (remove
     small dot/hatch noise). Blocks = connected components of
     extent & ~street (eroded 1px then grown back via nearest-label fill).
  3. Precompute ONCE over the full-res array: an "isolated small bright dot"
     mask (bright pixels a small opening removes -- J's white-dot signature)
     and integral images (summed-area tables) of gray, gray^2, dark mask,
     dot mask and street mask, so most per-patch statistics are O(1) lookups
     instead of a python loop over ~10^5 patches.
  4. Training samples: many overlapping 48x48 patches from each legend swatch
     interior (crosswalk.json legend_swatch_px), stride 3px, plus
     brightness/contrast jitter.
  5. Grid patch features (stride 16, 48x48 window, skip patches with >15%
     street-mask pixels): mean, std, frac dark, frac isolated-bright-dot (all
     via integral images), batched 2D FFT magnitude (patches pulled via a
     zero-copy sliding_window_view + fancy indexing) binned into 8 angle x 4
     radial-frequency bins, + dominant peak angle/freq.
  6. RandomForest on standardised features, trained on legend patches only,
     then self-trained: classify all grid patches, take top-800 most
     confident (>=0.75 proba) per class, retrain on legend+those, reclassify.
     NOTE: the code below uses n_rounds=1 (a single self-training refit), not
     the 2 rounds an earlier revision of this docstring described -- see the
     n_rounds comment in main() for why (an n_rounds=2 sweep caused runaway
     "G" self-training drift and was reverted).
  7. Block aggregation: "trustworthy" (small, <= LARGE_BLOCK_PX2) blocks get
     the area-weighted majority vote of their patch predictions; blocks with
     no valid patches get their nearest classified block's label (centroid
     nearest-neighbour). Large/leaked blocks fall back to the per-patch grid
     label (nearest-filled + mode-smoothed), i.e. they keep patch-resolution
     detail rather than being forced to one class.
  8. Write zones_px_v2.tif/.gpkg, preview PNGs, accuracy sample contact sheet.
"""
import csv
import json
import os
import time
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from osgeo import gdal, ogr
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from skimage.morphology import disk, binary_opening, binary_closing, binary_erosion
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from shapely import geometry as shg
from shapely.ops import unary_union

gdal.UseExceptions()
T0 = time.time()


def log(*a):
    print(f"[{time.time()-T0:7.1f}s]", *a, flush=True)


SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "11139001.jpg")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = f"{OUT_DIR}/cache_v2"
os.makedirs(CACHE_DIR, exist_ok=True)
CROSSWALK = f"{OUT_DIR}/crosswalk.json"
EXTENT_TIF = f"{OUT_DIR}/extent_px.tif"

FULL_W, FULL_H = 11173, 8809
SCALE = 2  # output raster resolution (mode-downsampled from full-res decisions)

DISTRICTS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
CODE_OF = {d: i + 1 for i, d in enumerate(DISTRICTS)}
CODE_TO_LETTER = {v: k for k, v in CODE_OF.items()}

PATCH = 48
HALF = PATCH // 2
STRIDE = 16
BRIGHT_T = 195.0        # cream paper / street threshold on raw gray
DARK_T = 90.0           # "dark fill" threshold
LOCAL_STD_WIN = 9       # window for local-std "smoothness" test (street vs textured fill)
LOCAL_STD_T = 14.0
STREET_CLOSE_R = 4      # bridges text-on-street gaps
STREET_OPEN_R = 3        # removes small dot/hatch noise from the street mask
STREET_FRAC_T = 0.15    # skip grid patches with more street-mask px than this
LARGE_BLOCK_PX2 = 60000  # ~60 acres; components bigger than this are treated as
                          # leaked/merged, not a real single block -> use patch fallback

PX_PER_FT_FULL = 455.0 / 3000.0
ACRE_FULL_PX2 = 43560.0 * (PX_PER_FT_FULL ** 2)


# ---------------------------------------------------------------- IO -----
def read_gray_full():
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
    log("gray_full shape", gray.shape)
    return gray


def read_extent_full():
    cache = f"{CACHE_DIR}/extent_full.npy"
    if os.path.exists(cache):
        return np.load(cache)
    ds = gdal.Translate("", EXTENT_TIF, format="MEM", width=FULL_W, height=FULL_H,
                         resampleAlg="nearest")
    arr = (ds.GetRasterBand(1).ReadAsArray() > 0)
    np.save(cache, arr)
    return arr


# ------------------------------------------------- block segmentation -----
def build_blocks(gray_full, extent_full):
    cache = f"{CACHE_DIR}/blocks_v2.npz"
    if os.path.exists(cache):
        log("loading cached block labels")
        d = np.load(cache)
        return d["labels"], d["block_mask"], d["sizes"]

    log("full-res street mask (bright & locally-smooth)...")
    gray_f = gray_full.astype(np.float32)
    lm = ndi.uniform_filter(gray_f, LOCAL_STD_WIN)
    lsq = ndi.uniform_filter(gray_f * gray_f, LOCAL_STD_WIN)
    lstd = np.sqrt(np.clip(lsq - lm ** 2, 0, None))
    street_raw = (gray_full > BRIGHT_T) & (lstd < LOCAL_STD_T)
    del lm, lsq, lstd, gray_f

    log("  closing (bridge text-on-street gaps) + opening (remove noise)...")
    street_closed = binary_closing(street_raw, disk(STREET_CLOSE_R))
    street = binary_opening(street_closed, disk(STREET_OPEN_R))
    del street_raw, street_closed

    block_mask = extent_full & ~street
    del street

    # NOTE: an earlier version eroded block_mask by 1px before labelling (to
    # separate touching blocks) then "regrew" labels back out via nearest-fill.
    # That was destructive: many real blocks here are already thin/small, and
    # a 1px erosion made a lot of them vanish entirely -- their footprint then
    # got nearest-filled from whatever label was closest, which was usually
    # the one dominant leaked/merged component, inflating it from ~81% of the
    # block area to ~99%. Labelling block_mask directly (no erosion) matches
    # what manual parameter-sweep testing showed to work; touching-block
    # separation is secondary to not accidentally erasing whole blocks.
    log("connected components...")
    structure = np.ones((3, 3), dtype=bool)
    grown, n = ndi.label(block_mask, structure=structure)
    log(f"  {n} block components")

    sizes = np.bincount(grown.ravel(), minlength=n + 1)
    log(f"  n_blocks={n}, largest={sizes[1:].max()} px2 "
        f"({100*sizes[1:].max()/block_mask.sum():.1f}% of block area), "
        f"n_large(>{LARGE_BLOCK_PX2})={(sizes[1:] > LARGE_BLOCK_PX2).sum()}")

    np.savez_compressed(cache, labels=grown.astype(np.int32), block_mask=block_mask, sizes=sizes)
    return grown.astype(np.int32), block_mask, sizes


# ------------------------------------------------------- global masks -----
def build_dot_mask(gray_full):
    """One global pass: isolated small bright dots (J's white-dot signature)."""
    cache = f"{CACHE_DIR}/dotmask.npy"
    if os.path.exists(cache):
        return np.load(cache)
    log("global isolated-bright-dot mask...")
    bright = gray_full > BRIGHT_T
    opened = binary_opening(bright, disk(2))
    dots = bright & ~opened
    np.save(cache, dots)
    return dots


def integral_image(mask):
    """Summed-area table with a leading zero row/col: sat[y,x] = sum of
    mask[:y, :x]. Rectangle sum for [y0:y1, x0:x1) is
    sat[y1,x1]-sat[y0,x1]-sat[y1,x0]+sat[y0,x0]. int64 accumulation (a plain
    float32 cumsum over ~10^8 boolean elements loses precision past ~1.6e7)."""
    m = mask.astype(np.int64)
    sat = np.zeros((m.shape[0] + 1, m.shape[1] + 1), dtype=np.int64)
    sat[1:, 1:] = np.cumsum(np.cumsum(m, axis=0), axis=1)
    return sat


def rect_frac(sat, cy, cx, half=HALF):
    y0, y1, x0, x1 = cy - half, cy + half, cx - half, cx + half
    s = sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0]
    return s / float((2 * half) ** 2)


# ------------------------------------------------------- FFT features -----
N_ANGLE = 8
N_RAD = 4


def _fft_bin_matrix(patch=PATCH):
    freqs_y = np.fft.fftfreq(patch)
    freqs_x = np.fft.rfftfreq(patch)
    fy, fx = np.meshgrid(freqs_y, freqs_x, indexing="ij")
    radius = np.sqrt(fy ** 2 + fx ** 2)
    angle = np.mod(np.arctan2(fy, fx), np.pi)  # undirected orientation

    max_r = radius.max()
    rbin = np.clip((radius / max_r * N_RAD).astype(int), 0, N_RAD - 1)
    abin = np.clip((angle / np.pi * N_ANGLE).astype(int), 0, N_ANGLE - 1)
    bin_idx = (abin * N_RAD + rbin).ravel()
    n_freq = radius.size
    M = np.zeros((n_freq, N_ANGLE * N_RAD), dtype=np.float32)
    M[np.arange(n_freq), bin_idx] = 1.0
    return M


_BIN_MATRIX = _fft_bin_matrix(PATCH)
_HANN2D = np.outer(np.hanning(PATCH), np.hanning(PATCH)).astype(np.float32)
N_FFT_FEAT = N_ANGLE * N_RAD
N_FEAT = 4 + N_FFT_FEAT + 2
FEATURE_NAMES = (["mean", "std", "frac_dark", "frac_bright_dot"] +
                  [f"fft_a{a}_r{r}" for a in range(N_ANGLE) for r in range(N_RAD)] +
                  ["peak_angle", "peak_rad"])


def batch_fft_features(patches_f32):
    win = patches_f32 * _HANN2D[None, :, :]
    F = np.fft.rfft2(win, axes=(-2, -1))
    mag = np.abs(F).astype(np.float32)
    mag[:, 0, 0] = 0.0
    flat = mag.reshape(mag.shape[0], -1)
    binned = flat @ _BIN_MATRIX
    total = binned.sum(axis=1, keepdims=True)
    binned_norm = binned / np.clip(total, 1e-6, None)
    peak_idx = np.argmax(binned, axis=1)
    peak_abin = (peak_idx // N_RAD).astype(np.float32) / N_ANGLE
    peak_rbin = (peak_idx % N_RAD).astype(np.float32) / N_RAD
    return binned_norm, peak_abin, peak_rbin


class FeatureExtractor:
    """Only ONE full-image integral image is kept in memory (street mask, used
    to prefilter ~380k grid candidates down to the ones worth extracting).
    Everything else (mean/std/dark-frac/dot-frac/FFT) is computed per-batch
    directly from actual patch pixels pulled via zero-copy
    sliding_window_view + fancy indexing -- avoids holding several ~800MB
    float64 whole-image integral images at once (14GB RAM box)."""

    def __init__(self, gray_full, dots_full, street_mask):
        self.gray_full = gray_full
        self.dots_full = dots_full
        self.sat_street = integral_image(street_mask)
        self.sliding_gray = sliding_window_view(gray_full, (PATCH, PATCH))  # view, no copy
        self.sliding_dots = sliding_window_view(dots_full, (PATCH, PATCH))  # view, no copy

    def street_frac(self, cy, cx):
        return rect_frac(self.sat_street, cy, cx)

    def drop_street_sat(self):
        self.sat_street = None

    def features(self, cy, cx, batch_size=4096):
        """cy, cx: 1D int arrays of patch centers (full-res coords)."""
        n = len(cy)
        feats = np.empty((n, N_FEAT), dtype=np.float32)
        top_y = cy - HALF
        top_x = cx - HALF
        for b0 in range(0, n, batch_size):
            b1 = min(n, b0 + batch_size)
            patches = self.sliding_gray[top_y[b0:b1], top_x[b0:b1]].astype(np.float32)
            dot_patches = self.sliding_dots[top_y[b0:b1], top_x[b0:b1]]
            mean_full = patches.mean(axis=(1, 2))
            std_full = patches.std(axis=(1, 2))
            dark_frac = (patches < DARK_T).mean(axis=(1, 2))
            dot_frac = dot_patches.mean(axis=(1, 2), dtype=np.float32)
            centered = patches - mean_full[:, None, None]
            binned_norm, peak_a, peak_r = batch_fft_features(centered)
            feats[b0:b1, 0] = mean_full
            feats[b0:b1, 1] = std_full
            feats[b0:b1, 2] = dark_frac
            feats[b0:b1, 3] = dot_frac
            feats[b0:b1, 4:4 + N_FFT_FEAT] = binned_norm
            feats[b0:b1, 4 + N_FFT_FEAT] = peak_a
            feats[b0:b1, 5 + N_FFT_FEAT] = peak_r
        return feats


# --------------------------------------------------------- training -----
def legend_training_samples(fx, crosswalk):
    swatch = crosswalk["legend_swatch_px"]
    centers = []
    labels = []
    for d in DISTRICTS:
        cx, cy = swatch[d]
        max_dx, max_dy = 12, 8
        stride = 3
        for yy in range(cy - max_dy, cy + max_dy + 1, stride):
            for xx in range(cx - max_dx, cx + max_dx + 1, stride):
                centers.append((yy, xx))
                labels.append(CODE_OF[d])
    centers = np.array(centers, dtype=np.int64)
    labels = np.array(labels, dtype=np.int32)
    log(f"legend base patches: {len(labels)} ({len(labels)//len(DISTRICTS)} per class)")

    cy0, cx0 = centers[:, 0], centers[:, 1]
    feats_list = [fx.features(cy0, cx0)]
    labels_list = [labels]

    # brightness/contrast jitter -- re-render jittered pixels for a *copy* of
    # the legend region and recompute features from scratch (small region, ok
    # to do without the integral-image shortcuts)
    for gain, bias in [(1.08, 0), (0.92, 0), (1.0, 10), (1.0, -10)]:
        n = len(labels)
        feat_rows = np.empty((n, N_FEAT), dtype=np.float32)
        for i in range(n):
            cy, cx = cy0[i], cx0[i]
            p = fx.gray_full[cy - HALF:cy + HALF, cx - HALF:cx + HALF].astype(np.float32)
            pj = np.clip(p * gain + bias, 0, 255)
            mean = pj.mean()
            std = pj.std()
            dark = (pj < DARK_T).mean()
            dot = fx.dots_full[cy - HALF:cy + HALF, cx - HALF:cx + HALF].mean()
            centered = (pj - mean)[None, :, :]
            binned_norm, peak_a, peak_r = batch_fft_features(centered)
            feat_rows[i, 0] = mean
            feat_rows[i, 1] = std
            feat_rows[i, 2] = dark
            feat_rows[i, 3] = dot
            feat_rows[i, 4:4 + N_FFT_FEAT] = binned_norm[0]
            feat_rows[i, 4 + N_FFT_FEAT] = peak_a[0]
            feat_rows[i, 5 + N_FFT_FEAT] = peak_r[0]
        feats_list.append(feat_rows)
        labels_list.append(labels.copy())

    X = np.concatenate(feats_list, axis=0)
    y = np.concatenate(labels_list, axis=0)
    log(f"legend training samples with jitter: {X.shape}")
    return X, y


def make_rf():
    return RandomForestClassifier(n_estimators=400, max_depth=20, min_samples_leaf=2,
                                   class_weight="balanced_subsample", n_jobs=-1,
                                   random_state=0)


# --------------------------------------------------------- main -----
def main():
    with open(CROSSWALK) as f:
        crosswalk = json.load(f)

    gray_full = read_gray_full()
    extent_full = read_extent_full()
    dots_full = build_dot_mask(gray_full)
    block_labels, block_mask, block_sizes = build_blocks(gray_full, extent_full)
    n_blocks = len(block_sizes) - 1

    fx = FeatureExtractor(gray_full, dots_full, ~block_mask & extent_full)

    log("building legend training set...")
    X_train, y_train = legend_training_samples(fx, crosswalk)

    scaler0 = StandardScaler()
    Xs0 = scaler0.fit_transform(X_train)
    clf0 = make_rf()
    clf0.fit(Xs0, y_train)
    log("legend-only resubstitution accuracy:", clf0.score(Xs0, y_train))

    # ---- candidate grid over the extent, indexed by (row_idx, col_idx) so we
    # can later scatter predictions back into a dense low-res grid ----
    log("building candidate grid...")
    ys = np.arange(HALF, FULL_H - HALF, STRIDE)
    xs = np.arange(HALF, FULL_W - HALF, STRIDE)
    n_rows, n_cols = len(ys), len(xs)
    ii, jj = np.meshgrid(np.arange(n_rows), np.arange(n_cols), indexing="ij")
    grid_y_all = ys[ii].ravel()
    grid_x_all = xs[jj].ravel()
    ii = ii.ravel()
    jj = jj.ravel()

    in_block = block_labels[grid_y_all, grid_x_all] > 0
    log(f"  grid candidates: {len(grid_y_all)}, in-block: {in_block.sum()}")
    grid_y_all, grid_x_all, ii, jj = grid_y_all[in_block], grid_x_all[in_block], ii[in_block], jj[in_block]
    block_id = block_labels[grid_y_all, grid_x_all]

    log("computing street fraction per candidate (integral image, vectorized)...")
    sfrac = fx.street_frac(grid_y_all, grid_x_all)
    keep = sfrac < STREET_FRAC_T
    log(f"  keeping {keep.sum()} / {len(keep)} patches after street-fraction filter")
    grid_y, grid_x, ii, jj, block_id = (grid_y_all[keep], grid_x_all[keep], ii[keep], jj[keep],
                                         block_id[keep])

    log("extracting grid patch features...")
    t1 = time.time()
    X_grid = fx.features(grid_y, grid_x)
    log(f"  done in {time.time()-t1:.1f}s, shape {X_grid.shape}")

    # ---- self-training rounds ----
    # NOTE: an earlier attempt with n_rounds=2, top-2000/round, no confidence
    # floor caused runaway "G" (solid black) drift -- G predictions went
    # 257 -> 11453 -> 46783 across rounds, and the accuracy contact sheet
    # showed most of those "G" tiles were actually dense hatch/dot patterns,
    # not solid black. Self-training was reinforcing its own mistakes. Now:
    # fewer rounds, a confidence floor, and a smaller per-class cap so a class
    # can't snowball just because the classifier is (wrongly) confident.
    n_rounds = 1
    SELF_TRAIN_TOP_K = 800
    SELF_TRAIN_MIN_CONF = 0.75
    cur_X, cur_y = X_train.copy(), y_train.copy()
    pred = None
    scaler, clf = scaler0, clf0
    for rnd in range(n_rounds + 1):
        scaler = StandardScaler()
        Xs = scaler.fit_transform(cur_X)
        clf = make_rf()
        clf.fit(Xs, cur_y)
        Xg_s = scaler.transform(X_grid)
        proba = clf.predict_proba(Xg_s)
        classes = clf.classes_
        pred_idx = np.argmax(proba, axis=1)
        pred = classes[pred_idx]
        conf = proba[np.arange(len(pred_idx)), pred_idx]
        log(f"round {rnd}: predicted class counts",
            {CODE_TO_LETTER[int(c)]: int((pred == c).sum()) for c in np.unique(pred)})
        if rnd == n_rounds:
            break
        add_X, add_y = [], []
        for c in classes:
            idx = np.where((pred == c) & (conf >= SELF_TRAIN_MIN_CONF))[0]
            if len(idx) == 0:
                continue
            top = idx[np.argsort(-conf[idx])[:SELF_TRAIN_TOP_K]]
            add_X.append(X_grid[top])
            add_y.append(np.full(len(top), c, dtype=np.int32))
        cur_X = np.concatenate([X_train] + add_X, axis=0)
        cur_y = np.concatenate([y_train] + add_y, axis=0)
        log(f"  self-train round {rnd}: retrain set size {cur_X.shape[0]}")

    # ---- dense per-patch grid label (nearest-fill + mode smooth) for the
    # large/leaked-block fallback ----
    log("building dense per-patch label grid (fallback for large blocks)...")
    grid_label = np.zeros((n_rows, n_cols), dtype=np.uint8)
    grid_label[ii, jj] = pred.astype(np.uint8)
    # nearest-fill zero (excluded/street) cells
    _, (niy, nix) = ndi.distance_transform_edt(grid_label == 0, return_indices=True)
    grid_filled = grid_label[niy, nix]
    # mode-smooth with a small window
    from skimage.filters.rank import majority
    from skimage.morphology import square
    grid_smoothed = majority(grid_filled, footprint=square(3))

    row_idx = np.clip((np.arange(FULL_H) - HALF) // STRIDE, 0, n_rows - 1)
    col_idx = np.clip((np.arange(FULL_W) - HALF) // STRIDE, 0, n_cols - 1)
    dense_label_full = grid_smoothed[row_idx[:, None], col_idx[None, :]]

    # ---- block aggregation: area-weighted majority vote for trustworthy
    # (small) blocks only ----
    log("aggregating patch predictions to blocks...")
    order = np.argsort(block_id, kind="stable")
    b_sorted = block_id[order]
    p_sorted = pred[order]
    uniq_blocks, starts = np.unique(b_sorted, return_index=True)
    starts = list(starts) + [len(b_sorted)]

    block_class = np.zeros(n_blocks + 1, dtype=np.uint8)
    block_has_patches = np.zeros(n_blocks + 1, dtype=bool)
    for k in range(len(uniq_blocks)):
        bid = uniq_blocks[k]
        if bid == 0:
            continue
        seg = p_sorted[starts[k]:starts[k + 1]]
        vals, counts = np.unique(seg, return_counts=True)
        block_class[bid] = vals[np.argmax(counts)]
        block_has_patches[bid] = True

    is_small = block_sizes <= LARGE_BLOCK_PX2
    trustworthy = is_small & block_has_patches
    log(f"blocks total={n_blocks}, classified_with_patches={block_has_patches.sum()}, "
        f"small_and_trustworthy={trustworthy.sum()}, "
        f"large(fallback_to_patch_grid)={(~is_small).sum()}")

    # small blocks with NO patches -> nearest classified small-block centroid
    centroids = ndi.center_of_mass(np.ones_like(block_labels), block_labels,
                                    index=np.arange(1, n_blocks + 1))
    centroids = np.array(centroids)
    existing = block_sizes[1:n_blocks + 1] > 0
    small_missing = np.where(is_small[1:n_blocks + 1] & ~block_has_patches[1:n_blocks + 1] &
                              existing)[0] + 1
    small_have = np.where(trustworthy[1:n_blocks + 1])[0] + 1
    if len(small_have) and len(small_missing):
        tree = cKDTree(centroids[small_have - 1])
        _, nn = tree.query(centroids[small_missing - 1])
        block_class[small_missing] = block_class[small_have[nn]]
        trustworthy[small_missing] = True
        log(f"  filled {len(small_missing)} small patch-less blocks via nearest-neighbour")

    # ---- combine: trustworthy small blocks -> block_class; everything else
    # (large/leaked blocks, and any residual) -> dense per-patch grid ----
    log("combining block-majority + dense patch-grid fallback...")
    out_full = np.where(trustworthy[block_labels], block_class[block_labels], dense_label_full)
    out_full[~block_mask] = 0
    out_full = out_full.astype(np.uint8)

    # ---- downsample to output SCALE via mode of each 2x2 cell ----
    def mode_downsample2(a):
        H, W = a.shape
        Hp, Wp = H + (H % 2), W + (W % 2)
        if (Hp, Wp) != (H, W):
            pad = np.zeros((Hp, Wp), dtype=a.dtype)
            pad[:H, :W] = a
            a = pad
        blocks = a.reshape(Hp // 2, 2, Wp // 2, 2).transpose(0, 2, 1, 3).reshape(Hp // 2, Wp // 2, 4)
        # mode of 4 values, treating 0 as lowest priority (prefer classified)
        out = np.zeros((Hp // 2, Wp // 2), dtype=a.dtype)
        for k in range(4):
            col = blocks[:, :, k]
            mask = out == 0
            out[mask] = col[mask]
        # simple heuristic above just fills with first nonzero; good enough since
        # 2x2 cells are almost always homogeneous except at block boundaries
        return out

    out_half = mode_downsample2(out_full)
    OUT_H, OUT_W = out_half.shape
    extent_half_dummy = mode_downsample2(extent_full.astype(np.uint8)).astype(bool)

    out_tif = f"{OUT_DIR}/zones_px_v2.tif"
    drv = gdal.GetDriverByName("GTiff")
    ds_out = drv.Create(out_tif, OUT_W, OUT_H, 1, gdal.GDT_Byte, options=["COMPRESS=LZW"])
    ds_out.SetGeoTransform((0, SCALE, 0, 0, 0, -SCALE))
    ds_out.GetRasterBand(1).WriteArray(out_half)
    ds_out.GetRasterBand(1).SetNoDataValue(0)
    ds_out.FlushCache()
    ds_out = None
    log("wrote", out_tif)

    # ---- polygonize + dissolve ----
    src_ds = gdal.Open(out_tif)
    src_band = src_ds.GetRasterBand(1)
    gdrv = ogr.GetDriverByName("GPKG")
    gpkg_path = f"{OUT_DIR}/zones_px_v2.gpkg"
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

    # ---- preview PNG ----
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
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/zones_preview_v2.png", mem)
    log("wrote", f"{OUT_DIR}/zones_preview_v2.png")

    # ---- full-res check crops ----
    crop_windows = {
        "crop1_stude_park": (3600, 2400, 1000, 750),
        "crop2_ship_channel": (5600, 3300, 1000, 750),
        "crop3_downtown": (4700, 3900, 1000, 750),
    }
    for name, (x0, y0, w, h) in crop_windows.items():
        orig_crop = gray_full[y0:y0 + h, x0:x0 + w]
        class_crop = out_full[y0:y0 + h, x0:x0 + w]
        color_c = np.zeros((h, w, 3), dtype=np.uint8)
        for code, rgb_c in palette.items():
            color_c[class_crop == code] = rgb_c
        orig3c = np.stack([orig_crop] * 3, axis=-1).astype(np.uint8)
        combo_c = np.concatenate([orig3c, color_c], axis=1)
        mem2 = mem_drv.Create("", combo_c.shape[1], combo_c.shape[0], 3, gdal.GDT_Byte)
        for i in range(3):
            mem2.GetRasterBand(i + 1).WriteArray(combo_c[..., i])
        gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/check_{name}_v2.png", mem2)
        log("wrote", f"{OUT_DIR}/check_{name}_v2.png")

    # ---- area report ----
    extent_px2 = int(extent_full.sum())
    area_report = {}
    for code in range(1, 11):
        letter = CODE_TO_LETTER[code]
        cnt = int((out_full == code).sum())
        area_report[letter] = {
            "full_res_px2": cnt,
            "pct_of_extent": 100.0 * cnt / extent_px2 if extent_px2 else 0.0,
        }
    unclassified = int(((out_full == 0) & extent_full).sum())
    meta = {
        "scale": SCALE,
        "output_size": [OUT_W, OUT_H],
        "full_size": [FULL_W, FULL_H],
        "geotransform": [0, SCALE, 0, 0, 0, -SCALE],
        "n_blocks": n_blocks,
        "n_trustworthy_blocks": int(trustworthy.sum()),
        "n_grid_patches_used": int(len(grid_y)),
        "extent_full_res_px2": extent_px2,
        "unclassified_within_extent_px2": unclassified,
        "unclassified_within_extent_pct": 100.0 * unclassified / extent_px2 if extent_px2 else 0.0,
        "area_by_district": area_report,
    }
    with open(f"{OUT_DIR}/zones_meta_v2.json", "w") as f:
        json.dump(meta, f, indent=2)
    log(json.dumps(meta, indent=2))

    # ---- accuracy sample contact sheet ----
    log("building accuracy sample...")
    rng = np.random.default_rng(0)
    sample_rows = []
    tiles = []
    tile_labels = []
    for code in range(1, 11):
        idx = np.where(pred == code)[0]
        if len(idx) == 0:
            continue
        take = rng.choice(idx, size=min(6, len(idx)), replace=False)
        for i in take:
            cy, cx = grid_y[i], grid_x[i]
            half_t = 40
            y0, y1 = max(0, cy - half_t), min(FULL_H, cy + half_t)
            x0, x1 = max(0, cx - half_t), min(FULL_W, cx + half_t)
            tile = gray_full[y0:y1, x0:x1]
            if tile.shape != (80, 80):
                tmp = np.full((80, 80), 200, dtype=np.uint8)
                tmp[:tile.shape[0], :tile.shape[1]] = tile
                tile = tmp
            tiles.append(tile)
            tile_labels.append(CODE_TO_LETTER[code])
            sample_rows.append((int(cx), int(cy), CODE_TO_LETTER[code]))

    ncols = 10
    nrows = int(np.ceil(len(tiles) / ncols))
    sheet = np.full((nrows * 90, ncols * 90), 255, dtype=np.uint8)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, ncols)
        sheet[r * 90:r * 90 + 80, c * 90:c * 90 + 80] = tile
    mem3 = mem_drv.Create("", sheet.shape[1], sheet.shape[0], 1, gdal.GDT_Byte)
    mem3.GetRasterBand(1).WriteArray(sheet)
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT_DIR}/accuracy_contact_sheet_v2.png", mem3)
    log("wrote", f"{OUT_DIR}/accuracy_contact_sheet_v2.png", "labels order:", tile_labels)

    with open(f"{OUT_DIR}/accuracy_sample_v2.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x", "y", "pred", "judged"])
        for row in sample_rows:
            w.writerow([row[0], row[1], row[2], ""])
    log("wrote", f"{OUT_DIR}/accuracy_sample_v2.csv")

    log("DONE")


if __name__ == "__main__":
    main()
