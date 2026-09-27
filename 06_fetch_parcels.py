"""
Step 6: fetch current (2026) land-use parcels from the City of Houston GIS
MapServer, clipped to the 1947 zoning-map extent (extent_1947 layer of
houston1947.gpkg, EPSG:2278), and write them as layer `parcels_2026` in the
same GeoPackage.

Service:
    https://mycity2.houstontx.gov/gisweb01/rest/services/HoustonMap/Landuse/MapServer/0/query
    (maxRecordCount 2000, supports paging via objectIds)

Flow:
    1. GET/POST returnIdsOnly=true with the extent envelope (+ simplified
       extent polygon as esriGeometryPolygon, inSR=2278) to get all matching
       OBJECTIDs. Print count and number of 2000-id pages.
    2. POST fetch each chunk of <=2000 objectIds (outFields=OBJECTID,GROUP_CD,
       GROUP_DSCR,LANDUSE_CD,LANDUSE_DSCR, outSR=2278, returnGeometry=true,
       f=json), 4 threads, retries with exponential backoff, small delay
       between requests (politeness).
    3. Parse each ESRI JSON response with ogr.Open(json_string) (same pattern
       as the reference script scratchpad/dl.py) and append features to
       parcels_2026 (MultiPolygon, EPSG:2278).

Resumable: if parcels_2026 already exists and its feature count equals the
expected total OBJECTID count, the fetch is skipped entirely.

IMPORTANT: the real fetch (no --test-bbox) can be tens of thousands of
requests-worth of parcels across ~150+ pages. Do not run it casually --
run with --test-bbox first to validate the code path.

Usage:
    python 06_fetch_parcels.py --test-bbox --out C:/temp/test.gpkg   # tiny, ~1-2 requests
    python 06_fetch_parcels.py                                       # full run (real data)
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from osgeo import gdal, ogr, osr

gdal.UseExceptions()

SERVICE_URL = "https://mycity2.houstontx.gov/gisweb01/rest/services/HoustonMap/Landuse/MapServer/0/query"
OUT_FIELDS = "OBJECTID,GROUP_CD,GROUP_DSCR,LANDUSE_CD,LANDUSE_DSCR"
BUFFER_FT = 0  # set by --buffer; pads the extent so later georef refinements stay covered
DEFAULT_GPKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "houston1947.gpkg")
CHUNK = 2000
MAX_WORKERS = 4

# Test-bbox: a 2000x2000 ft box around downtown, EPSG:2278
TEST_BBOX = (3_115_000.0, 13_837_000.0, 3_117_000.0, 13_839_000.0)  # xmin,ymin,xmax,ymax


def post(params, tries=6, timeout=120):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(SERVICE_URL, data=data, method="POST",
                                  headers={"User-Agent": "houston1947-research/1.0"})
    last_err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            if isinstance(d, dict) and "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:
            last_err = e
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    raise last_err


def polygon_rings(poly):
    rings = []
    for i in range(poly.GetGeometryCount()):
        ring = poly.GetGeometryRef(i)
        pts = [list(ring.GetPoint_2D(j)) for j in range(ring.GetPointCount())]
        rings.append(pts)
    return rings


def geom_to_esri_rings(geom):
    gtype = geom.GetGeometryName()
    rings = []
    if gtype == "MULTIPOLYGON":
        for i in range(geom.GetGeometryCount()):
            rings.extend(polygon_rings(geom.GetGeometryRef(i)))
    elif gtype == "POLYGON":
        rings.extend(polygon_rings(geom))
    else:
        raise ValueError(f"unexpected geometry type {gtype}")
    return rings


def bbox_polygon_esri(xmin, ymin, xmax, ymax):
    ring = [[xmin, ymin], [xmin, ymax], [xmax, ymax], [xmax, ymin], [xmin, ymin]]
    return {"rings": [ring], "spatialReference": {"wkid": 2278}}


def load_extent_geom(gpkg_path):
    ds = ogr.Open(gpkg_path)
    if ds is None:
        raise RuntimeError(f"cannot open {gpkg_path}")
    lyr = ds.GetLayerByName("extent_1947")
    if lyr is None:
        raise RuntimeError(f"no extent_1947 layer in {gpkg_path}")
    feat = lyr.GetNextFeature()
    geom = feat.GetGeometryRef().Clone()
    if BUFFER_FT:
        geom = geom.Buffer(BUFFER_FT).SimplifyPreserveTopology(200)
    ds = None
    return geom


def get_object_ids(esri_geom):
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps(esri_geom),
        "geometryType": "esriGeometryPolygon",
        "inSR": "2278",
        "spatialRel": "esriSpatialRelIntersects",
        "returnIdsOnly": "true",
    }
    d = post(params)
    ids = d.get("objectIds") or []
    ids = sorted(ids)
    return ids


def fetch_chunk(chunk):
    params = {
        "f": "json",
        "objectIds": ",".join(str(i) for i in chunk),
        "outFields": OUT_FIELDS,
        "outSR": "2278",
        "returnGeometry": "true",
    }
    return post(params)


def toint(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def ensure_layer(ds, srs):
    lyr = ds.GetLayerByName("parcels_2026")
    if lyr is not None:
        return lyr, True
    lyr = ds.CreateLayer("parcels_2026", srs, ogr.wkbMultiPolygon)
    for n, t in [("objectid", ogr.OFTInteger), ("group_cd", ogr.OFTInteger),
                 ("group_dscr", ogr.OFTString), ("landuse_cd", ogr.OFTInteger),
                 ("landuse_dscr", ogr.OFTString)]:
        lyr.CreateField(ogr.FieldDefn(n, t))
    return lyr, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_GPKG, help="output gpkg (default houston1947.gpkg)")
    ap.add_argument("--test-bbox", action="store_true",
                     help="use a tiny 2000x2000 ft downtown test box instead of the real extent")
    ap.add_argument("--sleep", type=float, default=0.15, help="polite delay between page requests (s)")
    ap.add_argument("--buffer", type=float, default=0, help="buffer the extent by this many feet before querying")
    args = ap.parse_args()
    global BUFFER_FT
    BUFFER_FT = args.buffer

    if args.test_bbox:
        xmin, ymin, xmax, ymax = TEST_BBOX
        esri_geom = bbox_polygon_esri(xmin, ymin, xmax, ymax)
        print(f"[TEST MODE] bbox {TEST_BBOX} -> {args.out}")
    else:
        gpkg_in = DEFAULT_GPKG
        geom = load_extent_geom(gpkg_in)
        env = geom.GetEnvelope()  # minx maxx miny maxy
        xmin, xmax, ymin, ymax = env
        esri_geom = geom_to_esri_rings(geom)
        esri_geom = {"rings": esri_geom, "spatialReference": {"wkid": 2278}}
        print(f"extent envelope: x[{xmin:.0f},{xmax:.0f}] y[{ymin:.0f},{ymax:.0f}]")

    print("requesting object ids (returnIdsOnly)...")
    ids = get_object_ids(esri_geom)
    n_pages = (len(ids) + CHUNK - 1) // CHUNK
    print(f"OBJECTIDs matched: {len(ids)}  pages(<=2000 each): {n_pages}")
    if not ids:
        print("no features matched -- nothing to fetch")
        return

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2278)

    gdrv = ogr.GetDriverByName("GPKG")
    if os.path.exists(args.out):
        ds = ogr.Open(args.out, update=1)
    else:
        ds = gdrv.CreateDataSource(args.out)

    lyr, existed = ensure_layer(ds, srs)
    if existed:
        existing_count = lyr.GetFeatureCount()
        if existing_count == len(ids):
            print(f"parcels_2026 already complete ({existing_count} features) -- skipping fetch")
            ds = None
            return
        else:
            print(f"parcels_2026 exists but incomplete ({existing_count} != {len(ids)}) -- refetching all")
            ds.DeleteLayer("parcels_2026")
            lyr, _ = ensure_layer(ds, srs)

    defn = lyr.GetLayerDefn()
    chunks = [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]

    total = 0
    nogeom = 0
    done = 0
    ds.StartTransaction()
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        for chunk, d in zip(chunks, ex.map(fetch_chunk, chunks)):
            src = ogr.Open(json.dumps(d))
            if src is not None:
                slyr = src.GetLayer(0)
                for f in slyr:
                    o = ogr.Feature(defn)
                    g = f.GetGeometryRef()
                    if g is None:
                        nogeom += 1
                    else:
                        o.SetGeometry(ogr.ForceToMultiPolygon(g.Clone()))
                    o["objectid"] = toint(f["OBJECTID"])
                    o["group_cd"] = toint(f["GROUP_CD"])
                    o["group_dscr"] = f["GROUP_DSCR"]
                    o["landuse_cd"] = toint(f["LANDUSE_CD"])
                    o["landuse_dscr"] = f["LANDUSE_DSCR"]
                    lyr.CreateFeature(o)
                    total += 1
            done += 1
            if done % 25 == 0 or done == len(chunks):
                ds.CommitTransaction()
                ds.StartTransaction()
                print(f"{done}/{len(chunks)} pages, {total} features so far", flush=True)
            time.sleep(args.sleep)
    ds.CommitTransaction()
    ds = None
    print(f"DONE: {total} features written to parcels_2026 in {args.out}; no-geometry: {nogeom}")


if __name__ == "__main__":
    main()
