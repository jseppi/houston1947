"""Task 1: end-to-end pipeline runner.
    05 (georef) -> overlay (crosstabs via overlay_lib) -> stats.py -> 09_compare_map.py
Writes run_manifest.json with timestamps, input hashes, provenance and package
versions, so a run is reproducible and auditable.

To switch to v3 zone-classification / v2 extent-extraction inputs once the
concurrent agent finishes producing zones_px_v3.tif / extent_px_v2.tif: nothing
to edit here -- config.py auto-detects them. Just re-run this script.
"""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import config


def sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd):
    print(f"\n=== running: {' '.join(cmd)} ===")
    t0 = datetime.now()
    r = subprocess.run([sys.executable] + cmd, cwd=config.DATA_DIR)
    dt = (datetime.now() - t0).total_seconds()
    print(f"=== finished {' '.join(cmd)} in {dt:.1f}s, exit {r.returncode} ===")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd} failed with exit {r.returncode}")
    return dt


def package_versions():
    versions = {}
    for mod in ("numpy", "scipy", "sklearn", "shapely", "osgeo.gdal", "matplotlib", "PIL"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            v = getattr(m, "__version__", getattr(m, "VersionInfo", None))
            if v is None and mod == "osgeo.gdal":
                v = m.__version__ if hasattr(m, "__version__") else m.VersionInfo(0)
            versions[mod] = str(v)
        except Exception as e:
            versions[mod] = f"unavailable ({e})"
    return versions


def gpkg_parcel_fetch_timestamp():
    """The task asks for the parcel fetch timestamp, taken from either fetch.log's
    mtime or the gpkg parcels layer -- report which. We use fetch.log's mtime since
    it's written once, right when 06_fetch_parcels.py finishes (the gpkg file itself
    gets touched again by every later script that opens it with update=1, so its own
    mtime is NOT a reliable proxy for the fetch time)."""
    if os.path.exists(config.FETCH_LOG):
        ts = datetime.fromtimestamp(os.path.getmtime(config.FETCH_LOG), tz=timezone.utc)
        return ts.isoformat(), "fetch.log mtime"
    ts = datetime.fromtimestamp(os.path.getmtime(config.GPKG), tz=timezone.utc)
    return ts.isoformat(), "houston1947.gpkg mtime (fetch.log missing)"


def script_hashes():
    names = ["config.py", "05_georef_zones.py", "06_fetch_parcels.py", "overlay_lib.py",
             "prep_parcel_fields.py", "stats.py", "09_compare_map.py", "run_all.py",
             "crosswalk.json"]
    return {n: sha256(os.path.join(config.DATA_DIR, n)) for n in names}


def input_hashes():
    paths = {
        "zones_px": config.ZONES_PX_TIF,
        "extent_px": config.EXTENT_PX_TIF,
        "gcps_frozen_csv": config.GCPS_FROZEN_CSV,
        "gcp_source_csv": config.GCP_SOURCE_CSV,
        "crosswalk_json": config.CROSSWALK_JSON,
        "houston1947_gpkg": config.GPKG,
    }
    return {k: {"path": v, "sha256": sha256(v)} for k, v in paths.items()}


def main():
    manifest = {"started": datetime.now(timezone.utc).isoformat()}
    manifest["input_versions"] = {"zones_px_version": config.ZONES_PX_VERSION,
                                   "extent_px_version": config.EXTENT_PX_VERSION,
                                   "vacant_as_neutral": config.VACANT_AS_NEUTRAL,
                                   "dedupe_burn_order": config.DEDUPE_BURN_ORDER,
                                   "georef_order": config.GEOREF_ORDER,
                                   "fit_in_target_crs": config.FIT_IN_TARGET_CRS}
    manifest["input_hashes_before_run"] = input_hashes()

    durations = {}
    durations["05_georef_zones.py"] = run(["05_georef_zones.py"])
    # overlay: crosstabs + landuse detail (task 1/3/6, replaces exec()-based 07b usage)
    t0 = datetime.now()
    import overlay_lib as ol
    cw = ol.load_crosswalk()
    grids = ol.load_grids()
    lu = ol.burn_parcels(grids)
    acres = ol.crosstab(grids, lu)
    ol.write_csvs(grids, lu, cw, acres)
    ol.landuse_detail_acres(grids)
    durations["overlay (overlay_lib)"] = (datetime.now() - t0).total_seconds()

    durations["stats.py"] = run(["stats.py"])
    durations["09_compare_map.py"] = run(["09_compare_map.py"])

    fetch_ts, fetch_src = gpkg_parcel_fetch_timestamp()
    manifest.update({
        "finished": datetime.now(timezone.utc).isoformat(),
        "durations_sec": durations,
        "input_hashes_after_run": input_hashes(),
        "script_hashes": script_hashes(),
        "parcel_service_url": config.PARCEL_SERVICE_URL,
        "parcel_fetch_timestamp": fetch_ts,
        "parcel_fetch_timestamp_source": fetch_src,
        "package_versions": package_versions(),
        "python_version": sys.version,
    })
    with open("run_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print("\nwrote run_manifest.json")


if __name__ == "__main__":
    main()
