"""End to end for the recompression in Tools 3 to 6 and the internal pyramids of Tool 2: a real
0.5 m tile through Tool 2 (gdal, pyramids on), then Tool 3 (all surfaces), Tool 5 (aspect and
slope), Tool 6 (5 m), Tool 4 (solar at a coarse 10 m cell to keep it short). Every output must be
DEFLATE with the right predictor; the Tool 2 mosaic must carry internal overviews and no .ovr;
the arcpy path with pyramids off must leave no .ovr."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import time

import arcpy
from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
TILE = (r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm"
        r"\MDT-50cm-183444-07-2025.tif")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_t36")
BOX = (-16800.40, 143200.12, -16200.10, 143800.18)
arcpy.env.overwriteOutput = True
if os.path.isdir(WS):
    shutil.rmtree(WS)
tile_dir = os.path.join(WS, "lidar_root", "T", "MDT-50cm")
os.makedirs(tile_dir)
shutil.copy2(TILE, os.path.join(tile_dir, os.path.basename(TILE)))
sr = arcpy.SpatialReference(3763)
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
x0, y0, x1, y1 = BOX
poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x0, y1), arcpy.Point(x1, y1),
                                  arcpy.Point(x1, y0), arcpy.Point(x0, y0)]), sr)
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([poly, "T"])

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
ok = True


def fail(msg):
    global ok
    ok = False
    print("FAIL:", msg)


def structure(path):
    d = gdal.Open(path)
    md = d.GetMetadata("IMAGE_STRUCTURE")
    b = d.GetRasterBand(1)
    info = (md.get("COMPRESSION"), md.get("PREDICTOR"), gdal.GetDataTypeName(b.DataType), b.GetOverviewCount(),
            os.path.exists(path + ".ovr"), round(os.path.getsize(path) / 1e6, 1))
    d = None
    return info


def tool2(engine, pyramids, out):
    t = mod.BuildMosaicsByPolygon()
    p = t.getParameterInfo()
    p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "lidar_root"); p[3].value = out
    p[4].value = "flat"; p[5].value = "DEM"; p[8].value = True; p[9].value = True; p[10].value = False
    p[15].value = "by name"; p[16].value = "extent"; p[17].value = pyramids; p[18].value = True; p[19].value = True; p[20].value = engine
    t0 = time.time(); t.execute(p, None)
    path = os.path.join(out, "T_DEM.tif")
    print("Tool 2 %-5s pyramids=%-5s %.1f s -> (comp, pred, type, overviews, .ovr, MB) = %s" % (engine, pyramids, time.time() - t0, structure(path)))
    return path


# Tool 2 gdal with pyramids: overviews present (a .ovr sidecar, this gdal build writes them externally)
mos = os.path.join(WS, "mosaic"); os.makedirs(mos)
dem = tool2("gdal", True, mos)
comp, pred, dtype, ovr, has_ovr, mb = structure(dem)
if comp != "DEFLATE" or pred != "3" or ovr < 1:
    fail("Tool 2 gdal pyramids: expected DEFLATE/3 with overviews")
# arcpy path with pyramids off: no .ovr
mos2 = os.path.join(WS, "mosaic_arcpy"); os.makedirs(mos2)
dem2 = tool2("arcpy", False, mos2)
if structure(dem2)[4]:
    fail("Tool 2 arcpy with pyramids off still wrote a .ovr")
# arcpy with pyramids on: a .ovr sidecar is fine
mos3 = os.path.join(WS, "mosaic_arcpy_pyr"); os.makedirs(mos3)
dem3 = tool2("arcpy", True, mos3)

# Tool 3: all surfaces, same_as_input
t3 = mod.DeriveSurfaces(); p = t3.getParameterInfo()
p[0].value = mos; p[1].value = True; p[15].value = True; p[16].value = True
t0 = time.time(); t3.execute(p, None); print("Tool 3: %.1f s" % (time.time() - t0))
for fn in sorted(os.listdir(mos)):
    if fn.endswith(".tif") and fn != "T_DEM.tif":
        s = structure(os.path.join(mos, fn)); print("   %-28s %s" % (fn, s))
        want = "2" if s[2] == "Byte" else "3"        # the hillshade is 8 bit
        if s[0] != "DEFLATE" or s[1] != want:
            fail("Tool 3 output not DEFLATE/%s: %s" % (want, fn))

# Tool 5: aspect and slope
t5 = mod.ReclassifyFactor(); p = t5.getParameterInfo()
p[0].value = mos; p[1].value = True; p[2].value = ["ASPECT", "SLOPE"]; p[3].value = True; p[4].value = True
t0 = time.time(); t5.execute(p, None); print("Tool 5: %.1f s" % (time.time() - t0))
rc = os.path.join(mos, "Reclass")
for fn in sorted(os.listdir(rc)):
    if fn.endswith(".tif"):
        s = structure(os.path.join(rc, fn)); print("   %-28s %s" % (fn, s))
        if s[0] != "DEFLATE" or s[1] != "2":
            fail("Tool 5 output not DEFLATE/2: " + fn)

# Tool 6: DEM and SLOPE to 5 m
t6 = mod.Resample(); p = t6.getParameterInfo()
offered = p[2].filter.list
types = [v for v in ("DEM", "SLOPE", "SLOPE_RCL") if v in offered] or offered[:2]
p[0].value = mos; p[1].value = True; p[2].value = types; p[3].value = 5.0; p[4].value = "auto"; p[5].value = "finest"; p[6].value = True; p[7].value = True
t0 = time.time(); t6.execute(p, None); print("Tool 6 (%s): %.1f s" % (types, time.time() - t0))
rs = os.path.join(mos, "Resample")
print("   Resample folder exists:", os.path.isdir(rs), sorted(os.listdir(rs)) if os.path.isdir(rs) else "")
found6 = 0
for dp, dn, fns in os.walk(rs) if os.path.isdir(rs) else []:
    for fn in sorted(fns):
        if fn.endswith(".tif"):
            found6 += 1
            s = structure(os.path.join(dp, fn)); print("   %-28s %s" % (fn, s))
            if s[0] != "DEFLATE" or s[1] not in ("2", "3"):
                fail("Tool 6 output not DEFLATE: " + fn)
if found6 == 0:
    fail("Tool 6 produced no outputs under " + rs)

# Tool 4: solar at 10 m
t4 = mod.SolarRadiation(); p = t4.getParameterInfo()
p[0].value = mos; p[1].value = True; p[5].value = 10.0; p[20].value = True; p[21].value = True
t0 = time.time()
try:
    t4.execute(p, None); print("Tool 4 (10 m): %.1f s" % (time.time() - t0))
    for fn in sorted(os.listdir(mos)):
        if "SOLAR" in fn and fn.endswith(".tif"):
            s = structure(os.path.join(mos, fn)); print("   %-28s %s" % (fn, s))
            if s[0] != "DEFLATE" or s[1] != "3":
                fail("Tool 4 output not DEFLATE/3: " + fn)
except Exception as exc:
    fail("Tool 4 raised: %r" % exc)
print("E2E TOOLS 3-6:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
