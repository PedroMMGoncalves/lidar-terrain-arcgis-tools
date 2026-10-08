"""End to end run of Tool 2 (BuildMosaicsByPolygon) on one real 0.5 m DGT tile, with a box AOI
whose edges fall inside cells, for the four combinations of clip mode and cover option.
Reports the data edge of each output against the AOI box."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import arcpy
import numpy as np

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
TILE = (r"D:\Grupo_Trabalho_Minas_Abandonadas\02_LiDAR_EDM_EDMI_Patrimonio\REAL\MDT-50cm"
        r"\MDT-50cm-183444-07-2025.tif")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "e2e")
BOX = (-16800.40, 143200.12, -16200.10, 143800.18)   # xmin, ymin, xmax, ymax (EPSG:3763)

arcpy.env.overwriteOutput = True

# fresh workspace: lidar_root/T/MDT-50cm/<tile>, aoi.shp with Area = "T"
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

# load the toolbox file as a module (ArcGIS imports it the same way)
loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tool = mod.BuildMosaicsByPolygon()


def data_edges(path):
    r = arcpy.Raster(path)
    e = r.extent
    cw = r.meanCellWidth
    a = arcpy.RasterToNumPyArray(r, nodata_to_value=np.nan)
    rows = np.where(~np.all(np.isnan(a), axis=1))[0]
    cols = np.where(~np.all(np.isnan(a), axis=0))[0]
    dx0 = e.XMin + cols[0] * cw
    dx1 = e.XMin + (cols[-1] + 1) * cw
    dy1 = e.YMax - rows[0] * cw
    dy0 = e.YMax - (rows[-1] + 1) * cw
    return (dx0, dx1, dy0, dy1), cw


def run(clip_mode, cover):
    out = os.path.join(WS, "out_{}_{}".format(clip_mode, "on" if cover else "off"))
    os.makedirs(out)
    p = tool.getParameterInfo()
    p[0].value = aoi
    p[1].value = "Area"
    p[2].value = os.path.join(WS, "lidar_root")
    p[3].value = out
    p[4].value = "flat"
    p[5].value = "DEM"
    p[8].value = True      # overwrite
    p[9].value = True      # skip incomplete
    p[10].value = False    # verify extent
    p[15].value = "by name"
    p[16].value = clip_mode
    p[17].value = False    # pyramids
    p[18].value = cover
    tool.updateParameters(p)
    print("cover_aoi enabled in the dialog: {}".format(p[18].enabled))
    tool.execute(p, None)
    outs = [f for f in os.listdir(out) if f.lower().endswith(".tif")]
    assert outs, "no output in " + out
    path = os.path.join(out, outs[0])
    (dx0, dx1, dy0, dy1), cw = data_edges(path)
    print("clip={:7s} cover={:5s} -> {} cell {:.4f}".format(clip_mode, str(cover), outs[0], cw))
    print("   data edge vs AOI box (+ beyond the box, - gap inside): "
          "W {:+.3f} E {:+.3f} S {:+.3f} N {:+.3f}".format(x0 - dx0, dx1 - x1, y0 - dy0, dy1 - y1))
    return (x0 - dx0, dx1 - x1, y0 - dy0, dy1 - y1), cw


results = {}
for clip_mode in ("extent", "polygon"):
    for cover in (False, True):
        results[(clip_mode, cover)] = run(clip_mode, cover)

ok = True
for (clip_mode, cover), (offs, cw) in results.items():
    if abs(cw - 0.5) > 1e-9:
        ok = False
        print("FAIL cell size changed:", clip_mode, cover, cw)
    if cover:
        if min(offs) < -1e-6 or max(offs) >= 0.5 + 1e-6:
            ok = False
            print("FAIL coverage:", clip_mode, cover, offs)
    else:
        if min(offs) < -0.25 - 1e-6 or max(offs) > 0.25 + 1e-6:
            ok = False
            print("FAIL nearest rounding:", clip_mode, cover, offs)
print("E2E RESULT:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
