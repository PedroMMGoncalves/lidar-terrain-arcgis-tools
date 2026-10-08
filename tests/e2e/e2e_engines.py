"""End to end for the Tool 2 mosaic engines: the same inputs (a real 0.5 m tile plus its _v01
copy, a box AOI with edges off the grid) through the gdal one pass path and the arcpy path, for
extent and polygon cuts with cover on and off. The outputs must agree cell for cell."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import time

import arcpy
import numpy as np
from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
TILE = (r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm"
        r"\MDT-50cm-183444-07-2025.tif")
TILE2 = (r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm"
         r"\MDT-50cm-184444-07-2025.tif")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_eng")
BOX = (-16300.40, 143200.12, -15700.10, 143800.18)   # crosses the two tiles (x = -16000)
arcpy.env.overwriteOutput = True

if os.path.isdir(WS):
    shutil.rmtree(WS)
tile_dir = os.path.join(WS, "lidar_root", "T", "MDT-50cm")
os.makedirs(tile_dir)
shutil.copy2(TILE, os.path.join(tile_dir, os.path.basename(TILE)))
shutil.copy2(TILE2, os.path.join(tile_dir, os.path.basename(TILE2)))
# v01 copy of the first tile with values + 100 (must win in both engines)
src = gdal.Open(TILE)
arr = src.ReadAsArray(); nd = src.GetRasterBand(1).GetNoDataValue()
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(tile_dir, "MDT-50cm-183444-07-2025_v01.tif"),
                                          src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"])
ds.SetGeoTransform(src.GetGeoTransform()); ds.SetProjection(src.GetProjection())
b = ds.GetRasterBand(1); b.SetNoDataValue(nd); b.WriteArray(np.where(arr == nd, nd, arr + 100).astype("float32")); b = None; ds = None; src = None

sr = arcpy.SpatialReference(3763)
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
x0, y0, x1, y1 = BOX
# an irregular polygon inside the box for the polygon mode (a triangle plus the box corner)
pts = [arcpy.Point(x0, y0), arcpy.Point(x0, y1), arcpy.Point(x1, y1), arcpy.Point(x1 - 200, y0 + 150), arcpy.Point(x1, y0), arcpy.Point(x0, y0)]
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([arcpy.Polygon(arcpy.Array(pts), sr), "T"])

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tool = mod.BuildMosaicsByPolygon()


def run(engine, clip, cover, method="FIRST"):
    out = os.path.join(WS, "out_%s_%s_%s_%s" % (engine, clip, "on" if cover else "off", method))
    os.makedirs(out)
    p = tool.getParameterInfo()
    p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "lidar_root"); p[3].value = out
    p[4].value = "flat"; p[5].value = "DEM"; p[7].value = method; p[8].value = True; p[9].value = True; p[10].value = False
    p[15].value = "by name"; p[16].value = clip; p[17].value = False; p[18].value = cover; p[19].value = True; p[20].value = engine
    t0 = time.time()
    tool.execute(p, None)
    dt = time.time() - t0
    path = os.path.join(out, "T_DEM.tif")
    ds = gdal.Open(path)
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    gt = ds.GetGeoTransform()
    a = ds.ReadAsArray()
    ndv = ds.GetRasterBand(1).GetNoDataValue()
    ext = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
    ds = None
    leftovers = [f for f in os.listdir(out) if f.endswith((".vrt", ".shp", "_full_T_DEM.tif"))]
    print("  %-5s %-7s cover=%-5s %s | %4.1f s | %5.1f MB | %s pred %s | nodata %s | ext %s | valid %d | leftovers %s" % (
        engine, clip, cover, method, dt, os.path.getsize(path) / 1e6, md.get("COMPRESSION"), md.get("PREDICTOR"),
        ndv, tuple(round(v, 2) for v in ext), int((a != ndv).sum()), leftovers))
    return a, ndv, ext, md


ok = True
for clip in ("extent", "polygon", "none"):
    for cover in (True, False):
        if clip == "none" and not cover:
            continue
        print("-- clip", clip, "cover", cover)
        ag, ndg, extg, mdg = run("gdal", clip, cover)
        aa, nda, exta, mda = run("arcpy", clip, cover)
        if tuple(round(v, 3) for v in extg) != tuple(round(v, 3) for v in exta):
            ok = False; print("FAIL extent differs:", extg, exta)
        elif ag.shape != aa.shape:
            ok = False; print("FAIL shape differs:", ag.shape, aa.shape)
        else:
            vg = ag != ndg; va = aa != nda
            same_mask = (vg == va).all()
            same_vals = np.array_equal(ag[vg & va], aa[vg & va])
            print("     same NoData mask: %s | same values: %s | nodata %s vs %s" % (same_mask, same_vals, ndg, nda))
            if not (same_mask and same_vals):
                ok = False; print("FAIL cells differ")
            if ndg != nda:
                print("     note: NoData value differs (gdal keeps the tiles' %s, arcpy declares %s); cells agree" % (ndg, nda))
        if mdg.get("COMPRESSION") != "DEFLATE" or mdg.get("PREDICTOR") != "3":
            ok = False; print("FAIL gdal output not DEFLATE/3")
# LAST method on gdal and arcpy (ordering of the VRT)
print("-- method LAST, extent, cover on")
ag, ndg, extg, _ = run("gdal", "extent", True, "LAST")
aa, nda, exta, _ = run("arcpy", "extent", True, "LAST")
if not (np.array_equal(ag, aa) and extg == exta):
    ok = False; print("FAIL LAST differs")
# v01 wins: mean over the west tile area should be about +100 vs the original tile
orig = gdal.Open(TILE); og = orig.GetGeoTransform(); oa = orig.ReadAsArray(); orig = None
print("E2E ENGINES:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
