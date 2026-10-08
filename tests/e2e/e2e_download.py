"""End to end on the live CDD: Tool 1's asset collection with the latest-version filter, the MIME
mapping of an orthophoto asset, and one real tile download with recompression on arrival."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_dl")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS)

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tool = mod.DownloadDGTData()
ok = True

# pure-ish: the version filter on a synthetic asset dict
assets = {("MDT-50cm", "MDT-50cm-1-07-2025", ".tif"): ["u1"],
          ("MDT-50cm", "MDT-50cm-1-07-2025_v01", ".tif"): ["u2"],
          ("MDT-50cm", "MDT-50cm-2-07-2025", ".tif"): ["u3"],
          ("LAZ", "LO-1-07-2025", ".laz"): ["u4"]}
kept = tool._drop_old_versions(assets)
print("drop_old_versions kept:", sorted(k[1] for k in kept))
if set(k[1] for k in kept) != {"MDT-50cm-1-07-2025_v01", "MDT-50cm-2-07-2025", "LO-1-07-2025"}:
    ok = False; print("FAIL: version filter")

user, pw = mod.read_dgt_credentials(mod.dgt_credentials_path())
session = mod.requests.Session()
ca = mod._requests_ca_bundle()
if ca:
    session.verify = ca
if not tool._authenticate(session, user, pw):
    print("AUTH FAILED"); sys.exit(3)
tool._creds = (user, pw)
bbox = (-8.3325, 40.9645, -8.3305, 40.9660)

# orthophoto asset now maps to .tif
ortos = tool._collect_assets(session, bbox, ["ORTOS-1995"], 0)
print("ORTOS-1995 keys:", list(ortos.keys()))
if not all(k[2] == ".tif" for k in ortos):
    ok = False; print("FAIL: orthophoto asset extension")

# one real tile, recompressed on arrival
tool._recompress = True
lidar = tool._collect_assets(session, bbox, ["MDT-50cm"], 0, latest_only=True)
print("MDT-50cm tiles found:", sorted(k[1] for k in lidar))
one = {k: v for k, v in lidar.items() if k[1].endswith("183444-07-2025") or k[1].endswith("183444-07-2025_v01")}
if not one:
    one = dict(list(lidar.items())[:1])
n_ok, n_skip, n_fail, failed = tool._download_assets(session, one, WS, False, 0)
print("download ok/skip/fail:", n_ok, n_skip, n_fail, failed)
for k in one:
    p = os.path.join(WS, k[0], k[1] + k[2])
    ds = gdal.Open(p)
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    print("  %s %.1f MB %s pred=%s nodata=%s size %dx%d" % (os.path.basename(p), os.path.getsize(p) / 1e6,
          md.get("COMPRESSION"), md.get("PREDICTOR"), ds.GetRasterBand(1).GetNoDataValue(), ds.RasterXSize, ds.RasterYSize))
    if md.get("COMPRESSION") != "DEFLATE" or md.get("PREDICTOR") != "3":
        ok = False; print("FAIL: downloaded tile not recompressed")
    # re-run: skipped (valid) and left alone
    n_ok2, n_skip2, _f, _l = tool._download_assets(session, one, WS, False, 0)
    if n_skip2 != 1:
        ok = False; print("FAIL: re-run did not skip the existing tile")
session.close()
print("E2E DOWNLOAD:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
