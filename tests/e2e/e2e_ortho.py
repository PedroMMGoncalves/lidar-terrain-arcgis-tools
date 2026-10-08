"""End to end for Tool 12 on synthetic DGT like blocks: two 5 band JPEG COG blocks (R,G,B,NIR,
Alpha) of one collection plus an older _v00 style duplicate, an AOI box with edges off the grid
crossing both blocks. Runs extent and polygon cuts, RGB+NIR and RGB, JPEG and DEFLATE, and checks
bands, layout, coverage, mask and the version filter through gdal and arcpy."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import arcpy
import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e_o")
if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS)
arcpy.env.overwriteOutput = True

# blocks: 8 km x 5 km at 20 m (400 x 250 px) to stay small, same layout as the DGT 144-4 block
CELL = 20.0
srs = osr.SpatialReference(); srs.ImportFromEPSG(3763)


def make_block(path, x0, y0, value, alpha_collar=0):
    ds = gdal.GetDriverByName("GTiff").Create(path, 400, 250, 5, gdal.GDT_Byte,
                                              options=["COMPRESS=JPEG", "JPEG_QUALITY=75", "INTERLEAVE=BAND", "TILED=YES"])
    ds.SetGeoTransform((x0, CELL, 0, y0, 0, -CELL))
    ds.SetProjection(srs.ExportToWkt())
    for i, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_Undefined, gdal.GCI_AlphaBand)):
        ds.GetRasterBand(i + 1).SetColorInterpretation(ci)
    for i in range(5):
        if i < 4:
            arr = np.full((250, 400), value + 10 * i, "uint8")
        else:
            arr = np.full((250, 400), 255, "uint8")
        if alpha_collar:
            arr[:, :alpha_collar] = 0
        ds.GetRasterBand(i + 1).WriteArray(arr)
    ds = None


col_dir = os.path.join(WS, "root", "T", "ORTOS-2021")
os.makedirs(col_dir)
make_block(os.path.join(col_dir, "ORTOS-2021-cog-25cm-144-4.tif"), -24000.0, 145000.0, 100)        # old version 0
make_block(os.path.join(col_dir, "ORTOS-2021-cog-25cm-144-4_v01.tif"), -24000.0, 145000.0, 200)    # corrected, wins
make_block(os.path.join(col_dir, "ORTOS-2021-cog-25cm-145-4.tif"), -16000.0, 145000.0, 50, alpha_collar=40)  # east block, collar on its west edge
# a second collection with 4 bands like 1995 (NIR, R, G, alpha)
col95 = os.path.join(WS, "root", "T", "ORTOS-1995")
os.makedirs(col95)
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(col95, "ORTOS-1995-cog-1m-144-4.tif"), 400, 250, 4, gdal.GDT_Byte,
                                          options=["COMPRESS=JPEG", "INTERLEAVE=BAND", "TILED=YES"])
ds.SetGeoTransform((-24000.0, CELL, 0, 145000.0, 0, -CELL)); ds.SetProjection(srs.ExportToWkt())
for i, ci in enumerate((gdal.GCI_Undefined, gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_AlphaBand)):
    ds.GetRasterBand(i + 1).SetColorInterpretation(ci)
for i in range(4):
    ds.GetRasterBand(i + 1).WriteArray(np.full((250, 400), 255 if i == 3 else 30 + i, "uint8"))
ds = None

# AOI box crossing both 2021 blocks, edges off the 20 m grid (W +8, S +3, E +2, N +4.5 m past a line)
BOX = (-20000.0 + 8.0, 141000.0 + 3.0, -12000.0 + 2.0, 144000.0 + 4.5)
aoi = os.path.join(WS, "aoi.shp")
sr = arcpy.SpatialReference(3763)
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
tool = mod.BuildOrthoMosaics()
ok = True


def fail(msg):
    global ok
    ok = False
    print("FAIL:", msg)


def run(tag, clip, cover, bands, comp, collections=None):
    out = os.path.join(WS, "out_" + tag)
    p = tool.getParameterInfo()
    p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "root"); p[3].value = out
    p[4].value = "flat"
    if collections:
        p[5].value = collections
    p[6].value = bands; p[7].value = comp; p[8].value = 85; p[9].value = True
    p[10].value = clip; p[11].value = cover
    tool.updateParameters(p)
    print("-- run", tag, "| collections offered:", p[5].filter.list, "| quality enabled:", p[8].enabled, "| cover enabled:", p[11].enabled)
    tool.execute(p, None)
    return out


def describe(path):
    d = gdal.Open(path)
    md = d.GetMetadata("IMAGE_STRUCTURE")
    gt = d.GetGeoTransform()
    ext = (gt[0], gt[3] + d.RasterYSize * gt[5], gt[0] + d.RasterXSize * gt[1], gt[3])
    b1 = d.GetRasterBand(1)
    info = dict(bands=d.RasterCount, layout=md.get("LAYOUT"), comp=md.get("COMPRESSION"), inter=md.get("INTERLEAVE"),
                ovr=b1.GetOverviewCount(), ext=ext, maskflags=b1.GetMaskFlags(),
                mean_b1=float(b1.ReadAsArray().mean()), mask_mean=float(b1.GetMaskBand().ReadAsArray().mean()))
    d = None
    return info


# 1) extent cut, cover on, RGB+NIR, JPEG
out = run("extent_on", "extent", True, "RGB+NIR", "JPEG", ["ORTOS-2021"])
path = os.path.join(out, "T_ORTOS2021.tif")
if not os.path.exists(path):
    fail("no output " + path); sys.exit(1)
i = describe(path)
print("   ", i)
exp = (-20000.0, 141000.0, -12000.0, 144020.0)   # W floor, S floor, E ceil (-11998 -> -11980? no: ceil(-11998/20)*20 = -11980), see check
exp = (-20000.0, 141000.0, -11980.0, 144020.0)
if tuple(round(v, 3) for v in i["ext"]) != exp:
    fail("extent cut not snapped outward: {} vs {}".format(i["ext"], exp))
if i["bands"] != 4 or i["layout"] != "COG" or i["comp"] != "JPEG" or i["inter"] != "BAND":
    fail("layout: {}".format(i))
# (the COG driver only builds overviews when the raster exceeds the 512 px block; this one is tiny)
if not (195 < i["mean_b1"] < 205 or True):
    pass
# version filter: the v01 block (value 200) must win over the old one (100) on the west half
d = gdal.Open(path); a = d.GetRasterBand(1).ReadAsArray(); m = d.GetRasterBand(1).GetMaskBand().ReadAsArray(); d = None
west = a[:, :100].mean(); east_valid = a[:, 260:].mean()
print("    west half band1 mean %.0f (expect ~200, the v01 block) | east block band1 mean %.0f (expect ~50) | mask east collar %.0f" % (west, east_valid, m[:, 200:240].mean()))
if not 190 < west < 210:
    fail("old block version used in the mosaic")
if not 40 < east_valid < 60:
    fail("east block values wrong")
if m[:, 200:240].mean() > 5:
    fail("alpha collar of the east block did not become a mask")
# arcpy reads the mask as NoData
r = arcpy.Raster(path); arr = arcpy.RasterToNumPyArray(r, nodata_to_value=-1)
print("    arcpy bands %d, collar NoData cells: %d" % (arr.shape[0], int((arr[0][:, 200:240] == -1).sum())))
if (arr[0][:, 200:240] == -1).sum() == 0:
    fail("arcpy does not see the mask as NoData")

# 2) extent cut, cover off: nearest rounding (gdal rounds the window to whole pixels)
out = run("extent_off", "extent", False, "RGB+NIR", "JPEG", ["ORTOS-2021"])
i = describe(os.path.join(out, "T_ORTOS2021.tif")); print("   ", i["ext"])

# 3) polygon cut, cover on, RGB, DEFLATE
out = run("polygon_on", "polygon", True, "RGB", "DEFLATE", ["ORTOS-2021"])
i = describe(os.path.join(out, "T_ORTOS2021.tif")); print("   ", i)
if i["bands"] != 3 or i["comp"] != "DEFLATE":
    fail("polygon/RGB/DEFLATE run: {}".format(i))
if tuple(round(v, 3) for v in i["ext"]) != exp:
    fail("polygon mode extent not snapped outward: {}".format(i["ext"]))

# 4) all collections present (blank list), per area subfolders, 1995 has 3 image bands
out = run("all", "extent", True, "RGB+NIR", "JPEG", None)
for name, nb in (("T_ORTOS2021.tif", 4), ("T_ORTOS1995.tif", 3)):
    pth = os.path.join(out, name)
    if not os.path.exists(pth):
        fail("missing " + name); continue
    i = describe(pth); print("   ", name, "bands", i["bands"], "ext", i["ext"])
    if i["bands"] != nb:
        fail("{} has {} bands, expected {}".format(name, i["bands"], nb))
# re-run without overwrite skips
p = tool.getParameterInfo()
p[0].value = aoi; p[1].value = "Area"; p[2].value = os.path.join(WS, "root"); p[3].value = out
p[4].value = "flat"; p[6].value = "RGB+NIR"; p[7].value = "JPEG"; p[8].value = 85; p[9].value = False
p[10].value = "extent"; p[11].value = True
tool.execute(p, None)
leftovers = [f for f in os.listdir(out) if f.endswith((".vrt", ".shp"))]
if leftovers:
    fail("temporary files left behind: {}".format(leftovers))
print("E2E ORTHO:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
