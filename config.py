"""Central, version-controlled configuration for the houston1947 pipeline.

Switching to v3 zone-classification / v2 extent-extraction inputs (once another
process finishes producing them) should require editing ONLY this file -- ideally
nothing at all, since the defaults below auto-detect the newest available version.
Re-run `python run_all.py` afterwards.
"""
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

def _p(name):
    return os.path.join(DATA_DIR, name)

# --- versioned raster inputs -------------------------------------------------
# zones: prefer the newest classification the concurrent agent has produced:
# v3g (v3 + G-district post-filter) > v3 > v2.
if os.path.exists(_p("zones_px_v3g.tif")):
    ZONES_PX_VERSION = "v3g"
    ZONES_PX_TIF = _p("zones_px_v3g.tif")
    ZONES_SCALE = 2  # update here if a future version's geotransform scale differs
elif os.path.exists(_p("zones_px_v3.tif")):
    ZONES_PX_VERSION = "v3"
    ZONES_PX_TIF = _p("zones_px_v3.tif")
    ZONES_SCALE = 2
else:
    ZONES_PX_VERSION = "v2"
    ZONES_PX_TIF = _p("zones_px_v2.tif")
    ZONES_SCALE = 2

# extent: prefer v2 extraction if present, else the original.
if os.path.exists(_p("extent_px_v2.tif")):
    EXTENT_PX_VERSION = "v2"
    EXTENT_PX_TIF = _p("extent_px_v2.tif")
    EXTENT_SCALE = 8  # update here if extent_px_v2 uses a different scale
else:
    EXTENT_PX_VERSION = "v1"
    EXTENT_PX_TIF = _p("extent_px.tif")
    EXTENT_SCALE = 8

# --- ground control points ---------------------------------------------------
GCP_SOURCE_CSV = _p("houston1947_gcps.csv")
GCPS_FROZEN_CSV = _p("gcps_frozen_v2.csv")  # frozen snapshot used by 05_georef_zones.py
# Independent check points (NOT used for fitting) -- street-intersection GCPs held
# out to validate the fitted polynomial's accuracy, esp. in the periphery.
CHECKPOINTS_CSV = _p("checkpoints.csv")

# --- georeferencing ------------------------------------------------------------
DST_EPSG = 2278
ZONES_RES_FT = 10.0
EXTENT_RES_FT = 40.0
GEOREF_ORDER = 2         # primary polynomial order used for the headline warp
GEOREF_ALT = "tps"       # alternative warp produced for sensitivity: "tps"
# Preferred fit space: fit the pixel->EPSG:2278 polynomial directly (GDAL is told
# the GCPs' target SRS is EPSG:2278), so the reported RMSE matches the model
# actually used to warp. Set False to reproduce the old lon/lat-then-reproject path.
FIT_IN_TARGET_CRS = True

# --- outputs -------------------------------------------------------------------
GPKG = _p("houston1947.gpkg")
ZONES_2278 = _p("zones_2278.tif")
ZONES_2278_TPS = _p("zones_2278_tps.tif")
EXTENT_2278 = _p("extent_2278.tif")
GEOREF_REPORT = _p("georef_report.txt")

# --- crosswalk / neutral handling ----------------------------------------------
CROSSWALK_JSON = _p("crosswalk.json")
# Vacant* landuse_dscr values are treated as NEUTRAL regardless of their group_dscr
# (e.g. "Vacant Exempt Land" is coded under Transportation & Utility / Public &
# Institutional in HCAD's group scheme, but is undeveloped land, not that use).
VACANT_AS_NEUTRAL = True  # sensitivity variant: set False to keep HCAD's raw group

# --- duplicate / stacked parcel handling ----------------------------------------
# Burn order when parcels overlap or are exact duplicates: neutral groups first,
# then non-neutral -- so a developed use "wins" (paints last / on top) over a
# vacant/undeveloped one where geometries stack. Reverse-order is the sensitivity
# variant (see stats.py).
DEDUPE_BURN_ORDER = "neutral_first"  # or "neutral_last" for the reverse sensitivity

# --- statistics ------------------------------------------------------------------
N_NULL_SHIFTS = 400          # >= 200 required
NULL_SHIFT_MIN_FT = 3000.0
NULL_SHIFT_MAX_FT = 20000.0
NULL_WRAP_MODE = "toroidal"  # np.roll wrap; parcels/extent held fixed (see stats.py docstring)
DECAY_MAX_FT = 3000.0
DECAY_STEP_FT = 250.0
DECAY_N_DIRECTIONS = 8
BOOTSTRAP_BLOCK_FT = 2000.0
BOOTSTRAP_REPS = 600         # >= 500 required
RNG_SEED = 0

# --- service provenance (for run_manifest.json) ---------------------------------
PARCEL_SERVICE_URL = "https://mycity2.houstontx.gov/gisweb01/rest/services/HoustonMap/Landuse/MapServer/0/query"
FETCH_LOG = _p("fetch.log")
