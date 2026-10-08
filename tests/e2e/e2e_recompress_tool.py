"""End to end for Tool 13 on a temp folder: two real 50 cm tiles (LZW, no predictor), one 2 m tile
(uncompressed), one JPEG orthophoto like raster, nested in subfolders. Dry run, real run, re-run."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import numpy as np
from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_rc")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(os.path.join(WS, "A", "MDT-50cm")); os.makedirs(os.path.join(WS, "B", "MDT-2m")); os.makedirs(os.path.join(WS, "B", "ORTOS-2021"))
for n in ("MDT-50cm-183444-07-2025.tif", "MDT-50cm-183445-07-2025.tif"):
    shutil.copy2(os.path.join(r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm", n), os.path.join(WS, "A", "MDT-50cm", n))
shutil.copy2(r"D:\LiDAR\Data\MDT-2m\MDT-2m-140067-04-2024.tif", os.path.join(WS, "B", "MDT-2m", "MDT-2m-140067-04-2024.tif"))
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(WS, "B", "ORTOS-2021", "block.tif"), 256, 256, 3, gdal.GDT_Byte, options=["COMPRESS=JPEG"])
ds.SetGeoTransform((0, 1, 0, 0, 0, -1))
for i in range(3):
    ds.GetRasterBand(i + 1).WriteArray((np.arange(256 * 256).reshape(256, 256) % 255).astype("uint8"))
ds = None

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tool = mod.RecompressRasters()


def sizes():
    out = {}
    for dp, _d, fns in os.walk(WS):
        for fn in fns:
            if fn.endswith(".tif"):
                p = os.path.join(dp, fn); d = gdal.Open(p); md = d.GetMetadata("IMAGE_STRUCTURE"); d = None
                out[os.path.relpath(p, WS)] = (round(os.path.getsize(p) / 1e6, 1), md.get("COMPRESSION", "none"), md.get("PREDICTOR"))
    return out


def run(dry):
    p = tool.getParameterInfo()
    p[0].value = WS; p[1].value = True; p[2].value = dry
    tool.execute(p, None)


before = sizes()
checks = {}
for p, (mb, comp, pred) in before.items():
    d = gdal.Open(os.path.join(WS, p)); checks[p] = (d.ReadAsArray().tobytes(), d.GetRasterBand(1).GetNoDataValue(), d.GetGeoTransform()); d = None
print("== dry run"); run(True)
after_dry = sizes()
ok = after_dry == before
if not ok:
    print("FAIL dry run changed files")
print("== real run"); run(False)
after = sizes()
for p in sorted(before):
    print("  %-40s %6.1f MB %-7s pred %-4s -> %6.1f MB %-7s pred %s" % (p, before[p][0], before[p][1], before[p][2], after[p][0], after[p][1], after[p][2]))
    d = gdal.Open(os.path.join(WS, p))
    same = d.ReadAsArray().tobytes() == checks[p][0] and d.GetRasterBand(1).GetNoDataValue() == checks[p][1] and d.GetGeoTransform() == checks[p][2]
    d = None
    if not same:
        ok = False; print("FAIL values changed in", p)
    if "ORTOS" in p and after[p][1] != "JPEG":
        ok = False; print("FAIL JPEG touched")
    if "ORTOS" not in p and (after[p][1] != "DEFLATE" or after[p][2] != "3"):
        ok = False; print("FAIL not DEFLATE/3:", p)
print("== re-run"); run(False)
if sizes() != after:
    ok = False; print("FAIL re-run changed files")
print("E2E RECOMPRESS TOOL:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
