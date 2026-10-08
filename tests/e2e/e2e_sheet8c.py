"""Sheet scale timing of the two Tool 2 engines: the real 8C cartogram sheet at 2 m from the
2542 MDT-2m tiles in D:\\LiDAR\\Data (reached through a junction named like the area), DEM only,
extent cut with cover on. gdal one pass vs arcpy MosaicToNewRaster plus recompression; the
outputs must agree cell for cell."""
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import time

import arcpy
import numpy as np
from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_8c")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(os.path.join(WS, "root"))
junction = os.path.join(WS, "root", "S8C")
subprocess.check_call(["cmd", "/c", "mklink", "/J", junction, r"D:\LiDAR\Data"], stdout=subprocess.DEVNULL)
arcpy.env.overwriteOutput = True

sr = arcpy.SpatialReference(3763)
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
with arcpy.da.SearchCursor(r"D:\LiDAR\Entregavel\8C\Cartograma_8C_ETRS.shp", ["SHAPE@"]) as cur:
    geom = next(cur)[0]
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([geom, "S8C"])
e = geom.extent
print("8C sheet extent: %.4f %.4f %.4f %.4f" % (e.XMin, e.YMin, e.XMax, e.YMax))

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tool = mod.BuildMosaicsByPolygon()


def run(engine):
    out = os.path.join(WS, "out_" + engine)
    os.makedirs(out)
    p = tool.getParameterInfo()
    p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "root"); p[3].value = out
    p[4].value = "flat"; p[5].value = "DEM"; p[7].value = "FIRST"; p[8].value = True; p[9].value = True; p[10].value = False
    p[15].value = "by name"; p[16].value = "extent"; p[17].value = False; p[18].value = True; p[19].value = True; p[20].value = engine
    t0 = time.time()
    tool.execute(p, None)
    dt = time.time() - t0
    path = os.path.join(out, "S8C_DEM.tif")
    ds = gdal.Open(path)
    md = ds.GetMetadata("IMAGE_STRUCTURE"); gt = ds.GetGeoTransform()
    ext = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
    print("%-5s: %.0f s | %d x %d px | %.0f MB | %s pred %s | nodata %s | ext %s" % (
        engine, dt, ds.RasterXSize, ds.RasterYSize, os.path.getsize(path) / 1e6, md.get("COMPRESSION"),
        md.get("PREDICTOR"), ds.GetRasterBand(1).GetNoDataValue(), tuple(round(v, 2) for v in ext)))
    return path, ext


pg, eg = run("gdal")
pa, ea = run("arcpy")
# gdal keeps the AOI box (NoData beyond the tiles, the sheet reaches past the border), arcpy shrinks
# to the tiles: compare on the common window and check the gdal only collar is NoData. The known
# residue is a few dozen cells at the seams of the survey's off grid edge tiles, NoData with arcpy
# and valued with gdal (81 on 8C), so the check tolerates a small count of arcpy NoData cells.
dg = gdal.Open(pg); da = gdal.Open(pa)
ok = eg[0] == ea[0] and eg[3] == ea[3]
w = min(dg.RasterXSize, da.RasterXSize); rows = min(dg.RasterYSize, da.RasterYSize)
ndg = dg.GetRasterBand(1).GetNoDataValue(); nda = da.GetRasterBand(1).GetNoDataValue()
value_diff = 0; arcpy_only_nodata = 0; gdal_only_nodata = 0; collar_valued = 0
for y in range(0, rows, 1000):
    n = min(1000, rows - y)
    a = dg.GetRasterBand(1).ReadAsArray(0, y, w, n)
    b = da.GetRasterBand(1).ReadAsArray(0, y, w, n)
    va = a != ndg; vb = b != nda
    value_diff += int((a[va & vb] != b[va & vb]).sum())
    arcpy_only_nodata += int((va & ~vb).sum()); gdal_only_nodata += int((~va & vb).sum())
    if dg.RasterXSize > w:
        collar_valued += int((dg.GetRasterBand(1).ReadAsArray(w, y, dg.RasterXSize - w, n) != ndg).sum())
print("common window %d x %d: value differences %d | NoData only in arcpy %d | NoData only in gdal %d | gdal collar cells with a value %d" % (
    w, rows, value_diff, arcpy_only_nodata, gdal_only_nodata, collar_valued))
if value_diff or gdal_only_nodata or collar_valued or arcpy_only_nodata > 500:
    ok = False
dg = None; da = None
print("E2E SHEET 8C:", "ok" if ok else "FAILED")
subprocess.call(["cmd", "/c", "rmdir", junction])
sys.exit(0 if ok else 1)
