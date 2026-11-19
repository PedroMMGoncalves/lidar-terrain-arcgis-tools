"""Tools 1 and 2 against the live CDD on the Azores (Sao Miguel, 10 m models): the tiles download
through the generic machinery, and the mosaics keep the CRS of the tiles (PTRA08 / UTM zone 26N),
not EPSG:3763, with both engines and a polygon cut from an AOI in EPSG:4326."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import arcpy

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_azores")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS)
arcpy.env.overwriteOutput = True

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

# AOI in EPSG:4326 west of Ponta Delgada, across the edge between two 1600 m by 1000 m tiles
sr = arcpy.SpatialReference(4326)
x0, x1, y0, y1 = -25.705, -25.695, 37.744, 37.749
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x0, y1), arcpy.Point(x1, y1),
                                  arcpy.Point(x1, y0), arcpy.Point(x0, y0)]), sr)
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([poly, "SMG"])

t1 = mod.DownloadDGTData()
p = t1.getParameterInfo()
p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "download"); p[3].value = 5000
p[4].value = ["ACORES-MDE-10", "ACORES-MDS-10"]; p[5].value = False; p[6].value = ""; p[7].value = ""
p[8].value = False; p[9].value = 0.5; p[10].value = False; p[11].value = False; p[12].value = False
p[13].value = mod.LAYOUT_PER_AREA; p[14].value = True; p[15].value = True
t1.execute(p, None)
folder = os.path.join(WS, "download", "SMG")
for col in ("ACORES-MDE-10", "ACORES-MDS-10"):
    sub = os.path.join(folder, col)
    print(col, sorted(os.listdir(sub)) if os.path.isdir(sub) else "MISSING")

ok = True
for engine in ("gdal", "arcpy"):
    out = os.path.join(WS, "mosaics_" + engine)
    os.makedirs(out)
    t2 = mod.BuildMosaicsByPolygon()
    q = t2.getParameterInfo()
    q[0].value = aoi; q[1].value = "Area"; q[2].value = os.path.join(WS, "download"); q[3].value = out
    q[4].value = "flat"; q[5].value = "BOTH"; q[6].value = "32_BIT_FLOAT"; q[7].value = "FIRST"
    q[8].value = True; q[9].value = True; q[10].value = False; q[11].value = ""; q[12].value = None
    q[13].value = False; q[14].value = False; q[15].value = "by geometry"; q[16].value = "polygon"
    q[17].value = False; q[18].value = True; q[19].value = True; q[20].value = engine
    t2.execute(q, None)
    for name in sorted(f for f in os.listdir(out) if f.lower().endswith(".tif")):
        d = arcpy.Describe(os.path.join(out, name))
        r = arcpy.Raster(os.path.join(out, name))
        print("  %s %s: EPSG:%s (%s), cell %g m, %d x %d, min %.1f max %.1f, NoData %r" % (
            engine, name, d.spatialReference.factoryCode, d.spatialReference.name,
            d.meanCellWidth, d.width, d.height, r.minimum or float("nan"),
            r.maximum or float("nan"), r.noDataValue))
        ok = ok and d.spatialReference.type == "Projected" \
            and int(d.spatialReference.factoryCode or 0) != 3763 \
            and abs(d.meanCellWidth - 10.0) < 1e-6 and d.width > 50
    names = [f for f in os.listdir(out) if f.lower().endswith(".tif")]
    ok = ok and len(names) == 2
print("E2E AZORES:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
