"""One-time (idempotent) prep pass over parcels_2026 in houston1947.gpkg.

Adds/refreshes three fields used by overlay_lib.py:
  vacant_neutral   INTEGER 0/1  -- landuse_dscr startswith "Vacant" (task 3)
  dup_wkb_hash     TEXT         -- md5 of the geometry WKB, for exact-duplicate detection
  burn_group_fwd   TEXT         -- group_dscr to use when burning, forward dedupe rule:
                                    for an exact-duplicate geometry cluster, keep the most
                                    frequent group_dscr in that cluster; ties broken by
                                    non-neutral-first, then alphabetical (task 4)
  burn_group_rev   TEXT         -- same but ties broken by neutral-first, then alphabetical
                                    reverse-alphabetical (the "reverse order" dedupe sensitivity)

Also writes duplicate_parcels_report.json with counts/acres of affected parcels.

Run standalone or via overlay_lib.ensure_parcel_fields() (which skips work if the
fields already exist and --force is not given).
"""
import hashlib
import json
import sys
from collections import Counter, defaultdict

from osgeo import ogr

ogr.UseExceptions()

import config

NEUTRAL_GROUPS = {"Undeveloped", "Unknown"}


def is_vacant(landuse_dscr):
    return bool(landuse_dscr) and landuse_dscr.strip().startswith("Vacant")


def is_neutral(group_dscr, landuse_dscr):
    if group_dscr in NEUTRAL_GROUPS or group_dscr is None:
        return True
    if config.VACANT_AS_NEUTRAL and is_vacant(landuse_dscr):
        return True
    return False


def pick_winner(members, neutral_first_tiebreak):
    """members: list of (group_dscr, neutral_bool). Returns winning group_dscr."""
    cnt = Counter(g for g, n in members)
    maxc = max(cnt.values())
    tied = sorted([g for g, c in cnt.items() if c == maxc])
    if len(tied) == 1:
        return tied[0]
    neutral_of = {g: n for g, n in members}
    if neutral_first_tiebreak:
        # neutral wins ties
        tied.sort(key=lambda g: (not neutral_of[g], g))
    else:
        # non-neutral wins ties (forward/default rule)
        tied.sort(key=lambda g: (neutral_of[g], g))
    return tied[0]


def main(force=False):
    ds = ogr.Open(config.GPKG, update=1)
    lyr = ds.GetLayerByName("parcels_2026")
    defn = lyr.GetLayerDefn()
    have = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
    needed = {"vacant_neutral", "dup_wkb_hash", "burn_group_fwd", "burn_group_rev"}
    if needed.issubset(have) and not force:
        print("parcel fields already present, skipping (use force=True to rebuild)")
        return

    for fname, ftype in [("vacant_neutral", ogr.OFTInteger), ("dup_wkb_hash", ogr.OFTString),
                          ("burn_group_fwd", ogr.OFTString), ("burn_group_rev", ogr.OFTString)]:
        if fname not in have:
            fd = ogr.FieldDefn(fname, ftype)
            if ftype == ogr.OFTString:
                fd.SetWidth(64)
            lyr.CreateField(fd)

    print("pass 1: hashing geometries + neutral flags...")
    hash_of = {}
    group_of = {}
    neutral_of = {}
    vacant_of = {}
    lyr.ResetReading()
    n = 0
    for feat in lyr:
        oid = feat.GetFID()
        geom = feat.GetGeometryRef()
        wkb = geom.ExportToWkb() if geom is not None else b""
        h = hashlib.md5(wkb).hexdigest()
        g = feat.GetField("group_dscr")
        lud = feat.GetField("landuse_dscr")
        neu = is_neutral(g, lud)
        hash_of[oid] = h
        group_of[oid] = g if g is not None else "(null)"
        neutral_of[oid] = neu
        vacant_of[oid] = is_vacant(lud)
        n += 1
        if n % 50000 == 0:
            print(f"  {n} features hashed")

    clusters = defaultdict(list)
    for oid, h in hash_of.items():
        clusters[h].append(oid)

    dup_clusters = {h: oids for h, oids in clusters.items() if len(oids) > 1}
    print(f"{n} parcels, {len(dup_clusters)} exact-duplicate geometry clusters "
          f"covering {sum(len(v) for v in dup_clusters.values())} parcels")

    winner_fwd = {}
    winner_rev = {}
    for h, oids in dup_clusters.items():
        members = [(group_of[o], neutral_of[o]) for o in oids]
        wf = pick_winner(members, neutral_first_tiebreak=False)
        wr = pick_winner(members, neutral_first_tiebreak=True)
        for o in oids:
            winner_fwd[o] = wf
            winner_rev[o] = wr

    print("pass 2: writing fields...")
    lyr.ResetReading()
    lyr.StartTransaction()
    n = 0
    dup_acres = 0.0
    for feat in lyr:
        oid = feat.GetFID()
        h = hash_of[oid]
        g = group_of[oid]
        is_dup = oid in winner_fwd
        feat.SetField("vacant_neutral", 1 if vacant_of[oid] else 0)
        feat.SetField("dup_wkb_hash", h)
        feat.SetField("burn_group_fwd", winner_fwd.get(oid, g))
        feat.SetField("burn_group_rev", winner_rev.get(oid, g))
        lyr.SetFeature(feat)
        n += 1
        if is_dup:
            geom = feat.GetGeometryRef()
            if geom is not None:
                dup_acres += geom.GetArea() / 43560.0
        if n % 50000 == 0:
            print(f"  {n} features written")
    lyr.CommitTransaction()
    ds = None

    report = {
        "n_parcels": n,
        "n_duplicate_clusters": len(dup_clusters),
        "n_parcels_in_duplicate_clusters": sum(len(v) for v in dup_clusters.values()),
        "approx_acres_in_duplicate_clusters_double_counted": round(dup_acres, 1),
        "note": "acres above sum each duplicate member's own area (i.e. double/multi counted "
                "at the geometry level prior to dedupe); after dedupe the burn uses a single "
                "winning group_dscr per stacked-identical cluster.",
    }
    with open("duplicate_parcels_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(force="--force" in sys.argv)
