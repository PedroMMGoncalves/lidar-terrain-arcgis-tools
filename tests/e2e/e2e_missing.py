"""Tool 1 against the live CDD on the 6C cells whose tiles are missing on the storage
(209515..209519, MDT-50cm): the missing ones must be reported at once with their object key, the
present one downloaded, the run must finish without retries or a traceback."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import time

import arcpy

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_missing")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS)
arcpy.env.overwriteOutput = True

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

# AOI: inside the 1 km cells x = 9 km, y = 215..219 km (EPSG:3763), away from the cell edges
sr = arcpy.SpatialReference(3763)
x0 = 9000.0 + 200; x1 = 9000.0 + 800; y0 = 215000.0 + 200; y1 = 219000.0 + 800
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x0, y1), arcpy.Point(x1, y1),
                                  arcpy.Point(x1, y0), arcpy.Point(x0, y0)]), sr)
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([poly, "T6C"])

t1 = mod.DownloadDGTData()
p = t1.getParameterInfo()
p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "download"); p[3].value = 5000
p[4].value = ["MDT-50cm"]; p[5].value = False; p[6].value = ""; p[7].value = ""; p[8].value = False
p[9].value = 0.5; p[10].value = False; p[11].value = False; p[12].value = False
p[13].value = mod.LAYOUT_PER_AREA; p[14].value = True; p[15].value = True
t0 = time.time()
try:
    t1.execute(p, None)
    raised = None
except Exception as exc:
    raised = exc
dt = time.time() - t0
print("Tool 1 finished in %.0f s, raised: %r" % (dt, raised))
folder = os.path.join(WS, "download", "T6C")
tiles = sorted(os.listdir(os.path.join(folder, "MDT-50cm"))) if os.path.isdir(os.path.join(folder, "MDT-50cm")) else []
print("downloaded tiles:", tiles)
failed_txt = os.path.join(folder, "T6C_failed.txt")
lines = open(failed_txt, encoding="utf-8").read().splitlines() if os.path.exists(failed_txt) else []
print("failed list:")
for ln in lines:
    print("   ", ln[:150])
ok = raised is None and dt < 90 and any("209517" in t for t in tiles) \
    and sum(1 for ln in lines if "missing on the CDD storage" in ln and "_v02.tif" in ln) >= 3
print("E2E MISSING:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
