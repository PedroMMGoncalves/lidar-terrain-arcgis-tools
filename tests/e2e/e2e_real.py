"""Real end to end of the orthophoto flow: Tool 1 execute (ORTOS-1995 and ORTOS-2021 for a small
polygon inside block 144-4), then Tool 12 execute on the downloaded blocks (extent cut, cover on,
RGB+NIR, JPEG 85), plus a polygon cut on the 1995 series. Reports timings, sizes and the output
structure as gdal and arcpy see it."""
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
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_real")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS)
arcpy.env.overwriteOutput = True

loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

# AOI: 2 km x 1.5 km inside block 144-4 (x -24000..-16000, y 140000..145000), edges off the 25 cm grid
BOX = (-19000.13, 142000.07, -17000.11, 143500.19)
sr = arcpy.SpatialReference(3763)
aoi = os.path.join(WS, "aoi.shp")
arcpy.management.CreateFeatureclass(WS, "aoi.shp", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(aoi, "Area", "TEXT", field_length=20)
x0, y0, x1, y1 = BOX
poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x0, y1), arcpy.Point(x1, y1),
                                  arcpy.Point(x1, y0), arcpy.Point(x0, y0)]), sr)
with arcpy.da.InsertCursor(aoi, ["SHAPE@", "Area"]) as cur:
    cur.insertRow([poly, "T_REAL"])

# ---- Tool 1
dl = os.path.join(WS, "download")
t1 = mod.DownloadDGTData()
p = t1.getParameterInfo()
p[0].value = aoi; p[1].value = "Area"; p[2].value = dl; p[3].value = 5000
p[4].value = ["ORTOS-1995", "ORTOS-2021"]; p[5].value = False
p[6].value = ""; p[7].value = ""; p[8].value = False; p[9].value = 1.0; p[10].value = False
p[11].value = False; p[12].value = False; p[13].value = mod.LAYOUT_PER_AREA; p[14].value = True; p[15].value = True
t0 = time.time()
t1.execute(p, None)
print("TOOL 1: %.0f s" % (time.time() - t0))
for col in ("ORTOS-1995", "ORTOS-2021"):
    d = os.path.join(dl, "T_REAL", col)
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        pth = os.path.join(d, fn)
        ds = gdal.Open(pth)
        md = ds.GetMetadata("IMAGE_STRUCTURE") if ds else {}
        print("  %s/%s  %.0f MB  %s bands %s" % (col, fn, os.path.getsize(pth) / 1e6, md.get("COMPRESSION"), ds.RasterCount if ds else "?"))
        ds = None

# ---- Tool 12, extent cut, both collections
mo = os.path.join(WS, "mosaics")
t12 = mod.BuildOrthoMosaics()
q = t12.getParameterInfo()
q[0].value = aoi; q[1].value = "Area"; q[2].value = dl; q[3].value = mo; q[4].value = "per_area_subfolder"
q[6].value = "RGB+NIR"; q[7].value = "JPEG"; q[8].value = 85; q[9].value = True; q[10].value = "extent"; q[11].value = True
t0 = time.time()
t12.execute(q, None)
print("TOOL 12 (extent, both collections): %.0f s" % (time.time() - t0))

ok = True
exp = {"T_ORTOS2021.tif": (4, 0.25, (-19000.25, 142000.0, -17000.0, 143500.25)),
       "T_ORTOS1995.tif": (3, 1.0, (-19001.0, 142000.0, -17000.0, 143501.0))}
for name, (nb, cell, ext_exp) in exp.items():
    pth = os.path.join(mo, "T_REAL", name.replace("T_", "T_REAL_"))
    if not os.path.exists(pth):
        ok = False; print("FAIL missing", pth); continue
    ds = gdal.Open(pth)
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    gt = ds.GetGeoTransform()
    ext = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
    b1 = ds.GetRasterBand(1)
    mask = b1.GetMaskBand().ReadAsArray(0, 0, 200, 200)
    print("  %s: %d x %d px, %d bands, cell %.2f, %s %s %s q%s, overviews %d, maskflags %d, %.0f MB" % (
        os.path.basename(pth), ds.RasterXSize, ds.RasterYSize, ds.RasterCount, gt[1], md.get("LAYOUT"),
        md.get("COMPRESSION"), md.get("INTERLEAVE"), md.get("JPEG_QUALITY"), b1.GetOverviewCount(), b1.GetMaskFlags(),
        os.path.getsize(pth) / 1e6))
    print("     extent %s | expected %s" % (tuple(round(v, 3) for v in ext), ext_exp))
    print("     bands: %s | corner mask mean %.0f" % (
        [gdal.GetColorInterpretationName(ds.GetRasterBand(i + 1).GetColorInterpretation()) for i in range(ds.RasterCount)], mask.mean()))
    if ds.RasterCount != nb or abs(gt[1] - cell) > 1e-9 or tuple(round(v, 3) for v in ext) != ext_exp:
        ok = False; print("FAIL structure of", name)
    if b1.GetOverviewCount() < 1:
        ok = False; print("FAIL no overviews in", name)
    ds = None
    d = arcpy.Describe(pth); r = arcpy.Raster(pth)
    print("     arcpy: %d bands, %s, cell %.2f, extent %.2f %.2f %.2f %.2f" % (
        d.bandCount, r.pixelType, r.meanCellWidth, r.extent.XMin, r.extent.YMin, r.extent.XMax, r.extent.YMax))

# ---- Tool 12, polygon cut on the 1995 series only (fast), flat output
mo2 = os.path.join(WS, "mosaics_poly")
q = t12.getParameterInfo()
q[0].value = aoi; q[1].value = "Area"; q[2].value = dl; q[3].value = mo2; q[4].value = "flat"; q[5].value = ["ORTOS-1995"]
q[6].value = "RGB"; q[7].value = "JPEG"; q[8].value = 85; q[9].value = True; q[10].value = "polygon"; q[11].value = True
t0 = time.time()
t12.execute(q, None)
print("TOOL 12 (polygon, 1995, RGB): %.0f s" % (time.time() - t0))
pth = os.path.join(mo2, "T_REAL_ORTOS1995.tif")
ds = gdal.Open(pth)
print("  %s: %d x %d px, %d bands, %.0f MB, leftovers: %s" % (os.path.basename(pth), ds.RasterXSize, ds.RasterYSize, ds.RasterCount,
      os.path.getsize(pth) / 1e6, [f for f in os.listdir(mo2) if f.endswith((".vrt", ".shp", ".dbf", ".shx", ".prj", ".cpg"))]))
ds = None
print("E2E REAL:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
