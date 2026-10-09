"""End to end: Tool 2 on a folder holding a tile and its _v01 copy (values + 100), extent clip with
cover on, recompression on and off. Checks the newer version wins, the output is DEFLATE with
predictor 3, and the values/NoData are intact."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import arcpy
import numpy as np
from osgeo import gdal

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
TILE = (r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm"
        r"\MDT-50cm-183444-07-2025.tif")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_v")
BOX = (-16800.40, 143200.12, -16200.10, 143800.18)
arcpy.env.overwriteOutput = True

if os.path.isdir(WS):
    shutil.rmtree(WS)
tile_dir = os.path.join(WS, "lidar_root", "T", "MDT-50cm")
os.makedirs(tile_dir)
old_tile = os.path.join(tile_dir, os.path.basename(TILE))
shutil.copy2(TILE, old_tile)
# the "corrected" v01 copy: same grid, values + 100 where valid
new_tile = os.path.join(tile_dir, "MDT-50cm-183444-07-2025_v01.tif")
src = gdal.Open(TILE)
arr = src.ReadAsArray()
nd = src.GetRasterBand(1).GetNoDataValue()
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create(new_tile, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"])
ds.SetGeoTransform(src.GetGeoTransform())
ds.SetProjection(src.GetProjection())
b = ds.GetRasterBand(1)
b.SetNoDataValue(nd)
b.WriteArray(np.where(arr == nd, nd, arr + 100).astype("float32"))
b = None
ds = None
src = None

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
tool = mod.BuildMosaicsByPolygon()

# reference: mean of the v01 tile inside the box
ref = gdal.Open(new_tile)
gt = ref.GetGeoTransform()
c0 = int((x0 - gt[0]) / gt[1]); c1 = int((x1 - gt[0]) / gt[1])
r0 = int((gt[3] - y1) / -gt[5]); r1 = int((gt[3] - y0) / -gt[5])
sub = ref.ReadAsArray()[r0 + 1:r1 - 1, c0 + 1:c1 - 1]
ref_mean = float(sub[sub != nd].mean())
ref = None


def run(recompress):
    out = os.path.join(WS, "out_rc_%s" % ("on" if recompress else "off"))
    os.makedirs(out)
    p = tool.getParameterInfo()
    p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "lidar_root"); p[3].value = out
    p[4].value = "flat"; p[5].value = "DEM"; p[8].value = True; p[9].value = True; p[10].value = False
    p[15].value = "by name"; p[16].value = "extent"; p[17].value = False; p[18].value = True
    p[19].value = recompress
    p[20].value = "arcpy"   # the recompression option only matters on the arcpy path; gdal writes DEFLATE directly
    tool.execute(p, None)
    path = os.path.join(out, "T_DEM.tif")
    ds = gdal.Open(path)
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    a = ds.ReadAsArray()
    ndv = ds.GetRasterBand(1).GetNoDataValue()
    mean = float(a[a != ndv].mean())
    print("recompress=%-5s %s | %.1f MB | %s pred=%s | nodata %s | mean %.2f (v01 reference %.2f) | cell %.3f" % (
        recompress, os.path.basename(path), os.path.getsize(path) / 1e6, md.get("COMPRESSION"),
        md.get("PREDICTOR"), ndv, mean, ref_mean, ds.GetGeoTransform()[1]))
    return md, mean, ndv, os.path.getsize(path)


ok = True
md_off, mean_off, nd_off, size_off = run(False)
md_on, mean_on, nd_on, size_on = run(True)
if md_on.get("COMPRESSION") != "DEFLATE" or md_on.get("PREDICTOR") != "3":
    ok = False; print("FAIL: recompressed output is not DEFLATE/predictor 3")
if abs(mean_on - mean_off) > 1e-6 or nd_on != nd_off:
    ok = False; print("FAIL: recompression changed values or NoData")
if abs(mean_on - ref_mean) > 0.5:
    ok = False; print("FAIL: the mosaic does not come from the v01 tile (old version used?)")
if size_on >= size_off:
    ok = False; print("FAIL: recompressed file is not smaller")
print("E2E VERSIONS/RECOMPRESS:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
