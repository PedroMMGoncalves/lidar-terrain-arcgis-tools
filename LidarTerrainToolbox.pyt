# -*- coding: utf-8 -*-
#
# Mining Terrain Factor Toolbox - derives topographic factors from DGT LiDAR.
# Copyright (C) 2026 Pedro Gonçalves
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Mining Terrain Factor Toolbox

Single file ArcGIS Pro Python Toolbox (.pyt) that derives topographic factors from
DGT LiDAR elevation data (DEM/DSM) in batch, per area of interest (mining polygons),
for a downstream renewables suitability MCDA. Factor engine only. The weighted overlay
and the REN/RAN/PDM exclusions are external steps, outside this toolbox.

Project CRS: ETRS89 / PT-TM06, EPSG:3763. Horizontal and vertical units in meters,
so Z factor = 1.

Everything lives in this one file by design (easier to maintain and share, and it
removes the fragile sys.path import of a separate helpers module):
  - shared helpers (sanitize, naming, CRS, value table validation)
  - the Toolbox class and its Tool classes (added incrementally)
  - pure unit tests in the __main__ block

Run the pure tests outside ArcGIS with:
    python MiningTerrainToolbox.pyt
Under ArcGIS the __main__ block never runs (ArcGIS imports the module, it does not
execute it as a script).
"""

import os
import re
import unicodedata
import xml.etree.ElementTree as ET

try:
    import arcpy
except ImportError:
    # Lets the pure helpers and the self tests run outside ArcGIS. Under ArcGIS,
    # arcpy is always importable. numpy is imported lazily inside ReclassifyFactor
    # (Tool 3) so this module loads without numpy when only the pure tests are run.
    arcpy = None


# ===========================================================================
# Constants
# ===========================================================================

PROJECT_EPSG = 3763                                  # ETRS89 / PT-TM06, meters
SOURCES = ("DEM", "DSM")
PRODUCTS = ("SLOPE", "ASPECT", "HILLSHADE", "PROFC", "PLANC", "SOLAR")
RECLASS_SUFFIX = "RCL"
MAX_NAME_LEN = 40                                    # margin for suffixes like _DEM_ASPECT_RCL
RECLASS_NODATA = 9999                                # NoData for reclassified integer outputs


# ===========================================================================
# Logging wrappers (no op outside ArcGIS, so helpers stay testable)
# ===========================================================================

def _msg(text):
    if arcpy is not None:
        arcpy.AddMessage(text)


def _warn(text):
    if arcpy is not None:
        arcpy.AddWarning(text)


def _err(text):
    if arcpy is not None:
        arcpy.AddError(text)


# ===========================================================================
# Helpers (single source of truth for naming, sanitization, validation)
# ===========================================================================

def sanitize_name(raw_name):
    """Convert a Mina attribute value into a name safe for ArcGIS and the file system.

    Steps (per spec 3.1):
      1. NFD normalize and drop combining marks (handles accents; c cedilha becomes c).
      2. Replace whitespace and separators with underscore.
      3. Remove anything that is not [A-Za-z0-9_].
      4. Collapse repeated underscores and trim.
      5. If it starts with a digit, prefix M_.
      6. Truncate to MAX_NAME_LEN.

    Collision handling is NOT done here. The caller keeps a set of used names and
    calls dedupe_name (spec 3.7). Logs via AddMessage when it changes a name.
    Non string input is coerced via str() (ArcGIS field values may be numeric).
    Raises ValueError (fail loud) if the result is empty.
    """
    if raw_name is None:
        raise ValueError("Mina name is None. Cannot build an output name.")
    original = str(raw_name)

    s = unicodedata.normalize("NFD", original)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s/\\\-.,;:|]+", "_", s)           # whitespace and separators to underscore
    s = re.sub(r"[^A-Za-z0-9_]", "", s)              # drop everything else
    s = re.sub(r"_+", "_", s).strip("_")             # collapse and trim underscores

    if not s:
        msg = "Mina name '{}' sanitizes to an empty string. Provide a usable name.".format(original)
        _err(msg)
        raise ValueError(msg)

    if s[0].isdigit():
        s = "M_" + s
    if len(s) > MAX_NAME_LEN:
        s = s[:MAX_NAME_LEN].strip("_")

    if s != original:
        _msg("Sanitized mina name '{}' to '{}'.".format(original, s))
    return s


def dedupe_name(base, used):
    """Return a name not present in `used`, appending _2, _3, ... on collision.

    The caller is responsible for adding the returned name to `used` afterwards
    (spec 3.7). Does not mutate `used`.
    """
    if base not in used:
        return base
    i = 2
    while "{}_{}".format(base, i) in used:
        i += 1
    return "{}_{}".format(base, i)


def build_output_name(mina, source, product=None, reclass=False):
    """Centralize the naming convention (spec 3.4). `mina` must already be sanitized.

    Mosaic:        {Mina}_{SOURCE}                  e.g. MinaA_DEM
    Surface:       {Mina}_{SOURCE}_{PRODUCT}        e.g. MinaA_DEM_SLOPE
    Reclassified:  {Mina}_{SOURCE}_{PRODUCT}_RCL    e.g. MinaA_DEM_SLOPE_RCL

    The .tif extension is added on write, not here.
    """
    source = source.upper()
    if source not in SOURCES:
        raise ValueError("Unknown source '{}'. Expected one of {}.".format(source, SOURCES))

    parts = [mina, source]
    if product is not None:
        product = product.upper()
        if product not in PRODUCTS:
            raise ValueError("Unknown product '{}'. Expected one of {}.".format(product, PRODUCTS))
        parts.append(product)
    elif reclass:
        raise ValueError("reclass=True requires a product. A base mosaic cannot be reclassified.")

    name = "_".join(parts)
    if reclass:
        name = name + "_" + RECLASS_SUFFIX
    return name


def parse_source_and_product(filename):
    """Inverse of build_output_name (spec 3.5). Returns a dict with keys
    mina, source, product, reclass.

    Parses right to left against the closed SOURCES and PRODUCTS sets. This is
    deliberate: sanitize_name collapses spaces into underscores, so a mina name
    can itself contain underscores (for example Sao_Domingos). A left to right
    split would be ambiguous; anchoring on the rightmost known tokens is robust,
    because build_output_name always appends the real source last.

    Matching is case sensitive (canonical), since build_output_name emits uppercase
    tokens. For a name that does not follow the convention, source comes back as
    None. Callers decide conformance on source is None (a valid mosaic or surface
    always has a source), not on mina.
    """
    base = os.path.basename(filename)
    stem = base[:-4] if base.lower().endswith(".tif") else base
    tokens = stem.split("_")

    # Canonical, case sensitive matching. build_output_name only ever emits
    # uppercase SOURCE/PRODUCT/RCL tokens, so exact matching keeps the round trip
    # exact and stops a mina name that merely ends in a lower or mixed case word
    # (for example "Vale_Dem") from being mistaken for a real token.
    reclass = False
    if tokens and tokens[-1] == RECLASS_SUFFIX:
        reclass = True
        tokens = tokens[:-1]

    product = None
    if tokens and tokens[-1] in PRODUCTS:
        product = tokens[-1]
        tokens = tokens[:-1]

    source = None
    if tokens and tokens[-1] in SOURCES:
        source = tokens[-1]
        tokens = tokens[:-1]

    mina = "_".join(tokens) if tokens else None
    if not mina:
        mina = None                                  # normalize "" (junk input) to None
    return {"mina": mina, "source": source, "product": product, "reclass": reclass}


def _as_class_id(value):
    """Coerce a class_id to int, failing loud on anything that is not a whole
    number. Silently truncating (for example 1.4 to 1) would merge distinct
    classes and mask a real overlap, which the fail loud rule forbids.
    """
    if isinstance(value, bool):
        raise ValueError("class_id must be a whole number, got bool: {}".format(value))
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("class_id must be a whole number, got: {}".format(value))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("class_id must be a whole number, got: {!r}".format(value))


def validate_value_table(rows):
    """Validate reclassification classes with [min, max) semantics (spec 6).

    `rows` is an iterable of (class_id, min_value, max_value).

    Raises ValueError (fail loud) on:
      - an empty table,
      - any row whose min is not strictly less than its max,
      - overlap between DIFFERENT class_ids,
      - a gap inside the global domain [min_global, max_global].

    Overlap between rows of the SAME class_id is allowed and intentional: it lets
    the user split a circular Aspect class (for example North as 315..360 and
    0..45, both class_id 1). This function does not detect wraparound; the user
    partitions the circle into linear rows.

    The "last class is [min, max] inclusive" rule is applied at reclassify time
    (numpy), not here. This validation is purely structural and returns the parsed
    rows sorted by (min, max) on success.
    """
    parsed = []
    for r in rows:
        if len(r) != 3:
            raise ValueError("Each class row must have 3 values (class_id, min, max), got: {}".format(r))
        cid = _as_class_id(r[0])
        lo, hi = float(r[1]), float(r[2])
        if not (lo < hi):
            raise ValueError("Class {} has min ({}) not strictly less than max ({}).".format(cid, lo, hi))
        parsed.append((cid, lo, hi))

    if not parsed:
        raise ValueError("Reclassification table is empty. Define at least one class.")

    # 1) Overlap between different class_ids. Pairwise, the class count is small.
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            cid_i, lo_i, hi_i = parsed[i]
            cid_j, lo_j, hi_j = parsed[j]
            if cid_i == cid_j:
                continue                              # same class_id overlap is allowed
            if lo_i < hi_j and lo_j < hi_i:           # [min, max) overlap test
                raise ValueError(
                    "Overlap between different classes {} [{}, {}) and {} [{}, {}). "
                    "Different class_ids may not overlap.".format(
                        cid_i, lo_i, hi_i, cid_j, lo_j, hi_j))

    # 2) Gaps. Merge the union of all intervals; it must be one contiguous span.
    ordered = sorted(parsed, key=lambda t: (t[1], t[2]))
    global_min = ordered[0][1]
    global_max = max(t[2] for t in ordered)
    cur_hi = ordered[0][2]
    for _, lo, hi in ordered[1:]:
        if lo > cur_hi:
            raise ValueError(
                "Gap in coverage between {} and {}. Classes must tile [{}, {}] "
                "with no holes.".format(cur_hi, lo, global_min, global_max))
        if hi > cur_hi:
            cur_hi = hi

    return ordered


def build_mina_groups(rows):
    """Group AOI features by mina so each mina yields one output.

    `rows` is an iterable of (fid, raw_mina). A mina can have several AOI polygons,
    and therefore several download folders, so features sharing the same raw `Mina`
    value are merged into one group that keeps every fid. The caller then gathers
    tiles from all of that mina's folders into a single mosaic.

    Returns a list of (final_name, [fids]) ordered by the group's smallest fid. Two
    DIFFERENT raw names that sanitize to the same safe name get a numeric suffix via
    dedupe_name (a genuine collision, not a merge). Raises ValueError (via
    sanitize_name) on an empty mina value.
    """
    groups = {}
    for fid, raw in rows:
        key = "" if raw is None else str(raw).strip()
        groups.setdefault(key, []).append(int(fid))

    result = []
    used = set()
    for raw in sorted(groups, key=lambda r: min(groups[r])):
        try:
            base = sanitize_name(raw)
        except ValueError as exc:
            raise ValueError("AOI feature(s) with FID {} have an unusable Mina value '{}': {}".format(
                sorted(groups[raw]), raw, exc))
        final = dedupe_name(base, used)
        used.add(final)
        result.append((final, sorted(groups[raw])))
    return result


def _parse_vrt_extent(xml_text):
    """Parse a GDAL .vrt XML string. Returns (xmin, ymin, xmax, ymax).

    Uses rasterXSize/rasterYSize and the GeoTransform (origin plus pixel size). Pure
    function, unit tested. Coordinates are in the VRT's own CRS, which for this data
    is the project CRS (tiles are ETRS89). Raises ValueError on a malformed VRT.
    """
    root = ET.fromstring(xml_text)
    try:
        w = int(root.attrib["rasterXSize"])
        h = int(root.attrib["rasterYSize"])
    except (KeyError, ValueError):
        raise ValueError("VRT has no valid rasterXSize/rasterYSize.")
    gt_el = root.find("GeoTransform")
    if gt_el is None or not gt_el.text:
        raise ValueError("VRT has no GeoTransform.")
    gt = [float(v) for v in gt_el.text.replace(",", " ").split()]
    if len(gt) != 6:
        raise ValueError("VRT GeoTransform must have 6 values, got {}.".format(len(gt)))
    x0, dx, _, y0, _, dy = gt
    xs = (x0, x0 + w * dx)
    ys = (y0, y0 + h * dy)
    return (min(xs), min(ys), max(xs), max(ys))


def detect_folder_prefix(names):
    """Infer the common folder name prefix that precedes the trailing FID integer.

    `names` is the list of data folder names (those that hold tiles). For each name that
    ends in digits, the prefix is the part before those digits. Returns the single common
    prefix. Raises ValueError if no name ends in digits, or if the names imply more than
    one prefix (ambiguous), so the caller can ask for an explicit prefix instead.
    """
    prefixes = set()
    for name in names:
        m = re.search(r"\d+$", name)
        if m:
            prefixes.add(name[:m.start()])
    if not prefixes:
        raise ValueError("Cannot auto-detect a folder prefix: no data folder name ends in a "
                         "number. Set the folder prefix explicitly.")
    if len(prefixes) > 1:
        raise ValueError("Cannot auto-detect a folder prefix: found more than one ({}). Set "
                         "the folder prefix explicitly.".format(sorted(prefixes)))
    return next(iter(prefixes))


def reclassify_array(arr, classes, nodata_out, flat_value=None, flat_class=None):
    """Apply [min, max) class intervals to a numpy float array, returning an int32 array.

    classes is the sorted list of (class_id, lo, hi) returned by validate_value_table. The
    class with the largest hi is inclusive at the top ([lo, hi]); all others are [lo, hi).
    A class_id may repeat across rows (the Aspect North split) and works naturally. Cells in
    no class, and NaN cells (input NoData), become nodata_out. If flat_value is not None,
    cells equal to it map to flat_class (the Aspect Flat = -1 case); this override runs after
    the class masks, so flat_value wins over any class interval that happens to contain it.
    nodata_out is reserved end to end (the caller guards class ids and the flat class against it).
    """
    import numpy as np
    out = np.full(arr.shape, nodata_out, dtype=np.int32)
    if classes:
        global_max = max(hi for _, _, hi in classes)
        with np.errstate(invalid="ignore"):           # NaN comparisons stay False, so NoData
            for class_id, lo, hi in classes:
                if hi == global_max:
                    mask = (arr >= lo) & (arr <= hi)   # last class inclusive at the top
                else:
                    mask = (arr >= lo) & (arr < hi)
                out[mask] = class_id
    if flat_value is not None and flat_class is not None:
        with np.errstate(invalid="ignore"):
            out[arr == flat_value] = flat_class
    return out


# ===========================================================================
# Toolbox
# ===========================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "LiDAR Terrain Toolbox"
        self.alias = "LidarTerrain"
        # Tool labels are numbered so they sort in pipeline order at the toolbox root
        # (no toolset categories): 01 Build Mosaics by Polygon, 02 Generate Surfaces,
        # 03 Solar Radiation, 04 Reclassify Slope and Aspect.
        self.tools = [BuildMosaicsByPolygon, DeriveSurfaces, SolarRadiation, ReclassifyFactor]


# ===========================================================================
# Tools
# ===========================================================================
#   01 BuildMosaicsByPolygon, 02 DeriveSurfaces, 03 SolarRadiation, 04 ReclassifyFactor.
#   SolarRadiation uses arcpy.sa.RasterSolarRadiation (GPU); ReclassifyFactor imports numpy lazily.


def _crs_label(sr):
    """Human readable CRS label for logs. Uses EPSG/WKID when present, else the name."""
    if sr is None:
        return "undefined"
    return "EPSG:{}".format(sr.factoryCode) if sr.factoryCode else "{} (no EPSG/WKID)".format(sr.name)


def _same_crs(sr_a, sr_b):
    """True if two spatial references are the same CRS. Compares EPSG/WKID when both
    have one, else falls back to the full WKT, so a CRS defined only by WKT
    (factoryCode 0) is not wrongly treated as equal to a different WKT only CRS.
    """
    if sr_a is None or sr_b is None:
        return sr_a is sr_b
    if sr_a.factoryCode and sr_b.factoryCode:
        return sr_a.factoryCode == sr_b.factoryCode
    return sr_a.exportToString() == sr_b.exportToString()


def _is_data_folder(folder):
    """True if `folder` directly contains a tile or tile subfolder for either product, i.e.
    an immediate entry whose name starts with MDT or MDS (case insensitive). Cheap shallow
    check used to tell real LiDAR folders from base data or scratch folders.
    """
    try:
        for entry in os.listdir(folder):
            upper = entry.upper()
            if upper.startswith("MDT") or upper.startswith("MDS"):
                return True
    except OSError:
        return False
    return False


def _gather_product_tiles(folders, product_prefix):
    """Collect the .tif tiles for one product across a mina's download folders.

    product_prefix is "MDT" (DEM) or "MDS" (DSM). Recurses each folder, keeps files
    whose name starts with product_prefix (case insensitive), and dedups by file name
    (adjacent AOIs of the same mina share tiles, the tile code identifies them).
    Returns (paths, tile_sr). Returns ([], None) if no tile matches.

    CRS is validated once per folder (tiles within a DGT download are homogeneous):
    fail loud on a geographic, undefined, or mixed CRS across the mina's folders.
    """
    seen = set()
    paths = []
    ref_sr = None
    for folder in folders:
        folder_checked = False
        for dirpath, _dirnames, filenames in os.walk(folder):
            for fn in filenames:
                if not fn.lower().endswith(".tif"):
                    continue
                if not fn.upper().startswith(product_prefix.upper()):
                    continue
                path = os.path.join(dirpath, fn)
                if not folder_checked:
                    folder_checked = True
                    sr = arcpy.Describe(path).spatialReference
                    if sr is None or sr.type != "Projected":
                        msg = ("Tile '{}' has a geographic or undefined CRS ('{}'). A projected "
                               "CRS in meters is required (project CRS EPSG:{}).").format(
                                   path, sr.name if sr else "Unknown", PROJECT_EPSG)
                        _err(msg)
                        raise ValueError(msg)
                    if ref_sr is None:
                        ref_sr = sr
                        if not sr.factoryCode:
                            _warn("Tiles use a projected CRS without an EPSG/WKID ('{}'). "
                                  "Project CRS is EPSG:{}.".format(sr.name, PROJECT_EPSG))
                        elif int(sr.factoryCode) != PROJECT_EPSG:
                            _warn("Tiles are EPSG:{} ('{}'), not the project EPSG:{}. Proceeding.".format(
                                sr.factoryCode, sr.name, PROJECT_EPSG))
                    elif not _same_crs(ref_sr, sr):
                        msg = ("Tile '{}' CRS ({}) differs from the others ({}). All tiles of a "
                               "mina must share one projected CRS.").format(
                                   path, _crs_label(sr), _crs_label(ref_sr))
                        _err(msg)
                        raise ValueError(msg)
                if fn in seen:
                    continue
                seen.add(fn)
                paths.append(path)
    return paths, ref_sr


def _folder_extent_polygon(folder, sr):
    """Coverage extent of a download folder as a Polygon in `sr`, read from the
    folder's .vrt (one cheap XML read). Returns None if the folder has no .vrt.

    Used only as a spatial sanity check that a folder maps to the right mina. `sr` must
    be the tiles CRS, since the VRT coordinates are in the tiles CRS. The caller projects
    the AOI geometry into that CRS before comparing.
    """
    vrt = None
    for fn in os.listdir(folder):
        if fn.lower().endswith(".vrt"):
            vrt = os.path.join(folder, fn)
            break
    if vrt is None:
        return None
    with open(vrt, "r", encoding="utf-8") as fh:
        xmin, ymin, xmax, ymax = _parse_vrt_extent(fh.read())
    corners = arcpy.Array([arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
                           arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin),
                           arcpy.Point(xmin, ymin)])
    return arcpy.Polygon(corners, sr)


class BuildMosaicsByPolygon(object):
    def __init__(self):
        self.label = "01 - Build Mosaics by Polygon"
        self.description = ("Build one DEM and one DSM mosaic per mina from the DGT LiDAR "
                            "download folders. Each folder 02_DGT_LiDAR_Data_<FID> holds the "
                            "tiles of one AOI; folders are matched to minas by FID and merged "
                            "per mina. The spatial selection was already done at download time "
                            "(5 km buffer per AOI), so no buffering or tile intersection is needed.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_aoi = arcpy.Parameter(
            displayName="AOI layer (FID maps to download folder)", name="in_aoi",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_aoi.filter.list = ["Polygon"]

        p_field = arcpy.Parameter(
            displayName="Mina name field", name="mina_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_field.parameterDependencies = [p_aoi.name]
        p_field.filter.list = ["Text", "Short", "Long"]

        p_root = arcpy.Parameter(
            displayName="LiDAR root folder", name="lidar_root",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Output")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["per_mina_subfolder", "flat"]
        p_struct.value = "per_mina_subfolder"

        p_products = arcpy.Parameter(
            displayName="Products", name="products",
            datatype="GPString", parameterType="Required", direction="Input")
        p_products.filter.type = "ValueList"
        p_products.filter.list = ["BOTH", "DEM", "DSM"]
        p_products.value = "BOTH"

        p_pixel = arcpy.Parameter(
            displayName="Pixel type", name="pixel_type",
            datatype="GPString", parameterType="Required", direction="Input")
        p_pixel.filter.type = "ValueList"
        p_pixel.filter.list = ["8_BIT_UNSIGNED", "16_BIT_SIGNED", "16_BIT_UNSIGNED",
                               "32_BIT_SIGNED", "32_BIT_UNSIGNED", "32_BIT_FLOAT", "64_BIT"]
        p_pixel.value = "32_BIT_FLOAT"

        p_method = arcpy.Parameter(
            displayName="Mosaic method (overlaps)", name="mosaic_method",
            datatype="GPString", parameterType="Required", direction="Input")
        p_method.filter.type = "ValueList"
        p_method.filter.list = ["FIRST", "LAST", "BLEND", "MEAN", "MINIMUM", "MAXIMUM"]
        p_method.value = "FIRST"

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        p_skip = arcpy.Parameter(
            displayName="Skip minas with missing folders", name="skip_incomplete",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_skip.value = True

        p_verify = arcpy.Parameter(
            displayName="Verify folder extent against AOI polygon", name="verify_extent",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_verify.value = True

        p_prefix = arcpy.Parameter(
            displayName="Folder name prefix (leave blank to find the data automatically)", name="folder_prefix",
            datatype="GPString", parameterType="Optional", direction="Input")

        return [p_aoi, p_field, p_root, p_out, p_struct, p_products,
                p_pixel, p_method, p_overwrite, p_skip, p_verify, p_prefix]

    def isLicensed(self):
        # Uses core Mosaic To New Raster, no Spatial Analyst needed.
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        in_aoi = parameters[0].valueAsText
        mina_field = parameters[1].valueAsText
        lidar_root = parameters[2].valueAsText
        out_folder = parameters[3].valueAsText
        output_structure = parameters[4].valueAsText
        products_param = parameters[5].valueAsText
        pixel_type = parameters[6].valueAsText
        mosaic_method = parameters[7].valueAsText
        overwrite_existing = bool(parameters[8].value)
        skip_incomplete = bool(parameters[9].value)
        verify_extent = bool(parameters[10].value)
        folder_prefix = parameters[11].valueAsText

        arcpy.env.overwriteOutput = overwrite_existing

        if pixel_type != "32_BIT_FLOAT":
            _warn("pixel_type is {}, but DGT LiDAR is floating point. Non float types "
                  "lose elevation precision.".format(pixel_type))

        products = [("DEM", "MDT"), ("DSM", "MDS")]
        if products_param == "DEM":
            products = [("DEM", "MDT")]
        elif products_param == "DSM":
            products = [("DSM", "MDS")]

        aoi_desc = arcpy.Describe(in_aoi)
        aoi_sr = aoi_desc.spatialReference
        if aoi_sr is None or aoi_sr.name in (None, "", "Unknown"):
            msg = "AOI layer has no spatial reference. Define its CRS first."
            _err(msg)
            raise ValueError(msg)
        _msg("AOI layer CRS: {}.".format(_crs_label(aoi_sr)))
        if aoi_sr.factoryCode and int(aoi_sr.factoryCode) != PROJECT_EPSG:
            _warn("AOI layer is {}, not the project EPSG:{}. Polygons are projected for the "
                  "extent check only.".format(_crs_label(aoi_sr), PROJECT_EPSG))

        # The download folder number equals the AOI FID. The folders were named at download
        # time from the original shapefile FID (0 based), which OID@ returns.
        oid_field = getattr(aoi_desc, "OIDFieldName", "") or ""
        if oid_field.upper() != "FID":
            _warn("AOI OID field is '{}', not 'FID'. Folders were numbered by the original "
                  "shapefile FID. If this layer is not that shapefile, the folder to mina "
                  "mapping may be wrong.".format(oid_field))

        # Read fid -> (raw name, geometry).
        fid_to_mina = {}
        fid_to_geom = {}
        with arcpy.da.SearchCursor(in_aoi, ["OID@", "SHAPE@", mina_field]) as cursor:
            for oid, shape, raw in cursor:
                fid_to_mina[int(oid)] = raw
                fid_to_geom[int(oid)] = shape

        if not fid_to_mina:
            msg = "AOI layer '{}' has no features.".format(in_aoi)
            _err(msg)
            raise ValueError(msg)

        # One output per mina. Features sharing a Mina value are merged.
        groups = build_mina_groups([(fid, fid_to_mina[fid]) for fid in fid_to_mina])

        # Find data folders (immediate subdirs that hold MDT/MDS tiles), resolve the name
        # prefix (explicit parameter, or auto-detected from those folder names), and map each
        # folder to its FID (the trailing integer). Fail loud on an ambiguous mapping.
        data_folders = []
        for entry in sorted(os.listdir(lidar_root)):
            full = os.path.join(lidar_root, entry)
            if os.path.isdir(full) and _is_data_folder(full):
                data_folders.append((entry, full))
        if not data_folders:
            msg = "No LiDAR data folders (with MDT/MDS tiles) found under '{}'.".format(lidar_root)
            _err(msg)
            raise ValueError(msg)

        if folder_prefix:
            prefix = folder_prefix
        else:
            prefix = detect_folder_prefix([name for name, _ in data_folders])
            _msg("Auto-detected folder prefix: '{}'.".format(prefix))

        fid_to_folder = {}
        for name, full in data_folders:
            if not name.startswith(prefix):
                _warn("Data folder '{}' does not match prefix '{}'. Skipping it.".format(name, prefix))
                continue
            suffix = name[len(prefix):]
            if not suffix.isdigit():
                _warn("Data folder '{}' has no FID number after the prefix. Skipping it.".format(name))
                continue
            fid = int(suffix)
            if fid in fid_to_folder:
                msg = ("Two data folders map to FID {}: '{}' and '{}'. Ambiguous mapping; set "
                       "an explicit folder prefix.").format(
                           fid, os.path.basename(fid_to_folder[fid]), name)
                _err(msg)
                raise ValueError(msg)
            fid_to_folder[fid] = full

        total = len(groups)
        built_minas = 0
        present_minas = 0
        created_count = 0
        skipped_no_data = []
        skipped_incomplete = []
        skipped_no_tiles = []
        skipped_mismatch = []

        arcpy.SetProgressor("step", "Building one mosaic per mina...", 0, total, 1)
        for final_mina, fids in groups:
            arcpy.SetProgressorPosition()
            present = [f for f in fids if f in fid_to_folder]
            missing = [f for f in fids if f not in fid_to_folder]

            if not present:
                _warn("Mina '{}': no download folders present yet (FIDs {}). Skipping.".format(
                    final_mina, fids))
                skipped_no_data.append(final_mina)
                continue
            if missing and skip_incomplete:
                _warn("Mina '{}': missing folders for FIDs {}. Skipping (incomplete). Re-run "
                      "when the download finishes.".format(final_mina, missing))
                skipped_incomplete.append(final_mina)
                continue

            present_folders = [fid_to_folder[f] for f in present]

            # Gather tiles per product first. This validates the tile CRS (fail loud) and
            # yields the tile spatial reference used by the extent check and the mosaic.
            gathered = {}              # source -> (tiles, tile_sr)
            ref_tile_sr = None
            for source, prefix in products:
                tiles, sr = _gather_product_tiles(present_folders, prefix)
                if tiles:
                    gathered[source] = (tiles, sr)
                    if ref_tile_sr is None:
                        ref_tile_sr = sr
            if not gathered:
                _warn("Mina '{}': folders present but no DEM/DSM tiles found. Skipping.".format(final_mina))
                skipped_no_tiles.append(final_mina)
                continue

            # Spatial sanity check: each folder's VRT extent must contain its FID's AOI
            # polygon centroid. Catches a wrong FID to folder mapping (disjoint was too weak,
            # an adjacent folder still overlaps).
            if verify_extent:
                mismatch = False
                for f in present:
                    folder_poly = _folder_extent_polygon(fid_to_folder[f], ref_tile_sr)
                    if folder_poly is None:
                        _warn("Mina '{}': folder for FID {} has no .vrt, cannot verify extent.".format(
                            final_mina, f))
                        continue
                    geom = fid_to_geom[f]
                    if geom is None:
                        continue
                    geom_t = geom if _same_crs(aoi_sr, ref_tile_sr) else geom.projectAs(ref_tile_sr)
                    centroid = arcpy.PointGeometry(geom_t.centroid, ref_tile_sr)
                    if not folder_poly.contains(centroid):
                        _warn("Mina '{}': folder for FID {} does not contain its AOI polygon "
                              "centroid. Possible wrong FID mapping. Skipping this mina.".format(
                                  final_mina, f))
                        mismatch = True
                        break
                if mismatch:
                    skipped_mismatch.append(final_mina)
                    continue

            if missing:
                _warn("Mina '{}': building a PARTIAL mosaic, missing FIDs {}. Re-run with "
                      "overwrite enabled after the download finishes to refresh it.".format(
                          final_mina, missing))

            created_here = 0
            existing_here = 0
            for source, (tiles, sr) in gathered.items():
                out_name = build_output_name(final_mina, source) + ".tif"
                location = out_folder
                if output_structure == "per_mina_subfolder":
                    location = os.path.join(out_folder, final_mina)
                if not os.path.isdir(location):
                    os.makedirs(location)
                out_path = os.path.join(location, out_name)

                if os.path.exists(out_path) and not overwrite_existing:
                    _msg("Mina '{}' ({}): output exists, skipping ({}).".format(
                        final_mina, source, out_name))
                    existing_here += 1
                    continue
                if os.path.exists(out_path) and overwrite_existing:
                    _msg("Mina '{}' ({}): overwriting existing {}.".format(final_mina, source, out_name))

                arcpy.management.MosaicToNewRaster(
                    input_rasters=tiles,
                    output_location=location,
                    raster_dataset_name_with_extension=out_name,
                    coordinate_system_for_the_raster=sr,
                    pixel_type=pixel_type,
                    number_of_bands=1,
                    mosaic_method=mosaic_method,
                )
                _msg("Mina '{}' ({}): mosaicked {} tiles from {} folder(s) -> {}".format(
                    final_mina, source, len(tiles), len(present_folders), out_name))
                created_here += 1

            created_count += created_here
            if created_here:
                built_minas += 1
            elif existing_here:
                present_minas += 1

        arcpy.ResetProgressor()

        _msg("Done. Minas: {}. Built now: {}. Already present: {}. Mosaics created: {}. Skipped "
             "(no data: {}, incomplete: {}, no tiles: {}, extent mismatch: {}).".format(
                 total, built_minas, present_minas, created_count,
                 len(skipped_no_data), len(skipped_incomplete),
                 len(skipped_no_tiles), len(skipped_mismatch)))
        if skipped_incomplete:
            _warn("Incomplete, re-run after download finishes: " + ", ".join(skipped_incomplete))
        if skipped_no_tiles:
            _warn("Folders present but no tiles found: " + ", ".join(skipped_no_tiles))
        if skipped_mismatch:
            _warn("Extent mismatch, check FID mapping: " + ", ".join(skipped_mismatch))
        return


def _assert_projected_raster(path):
    """Return the raster spatial reference, failing loud if it is geographic or undefined.
    Slope and aspect in degrees on a geographic CRS are invalid (spec 3.2). Warns if the
    CRS is projected but not the project EPSG.
    """
    sr = arcpy.Describe(path).spatialReference
    if sr is None or sr.type != "Projected":
        msg = ("Mosaic '{}' has a geographic or undefined CRS ('{}'). A projected CRS in "
               "meters is required for slope and aspect (project CRS EPSG:{}).").format(
                   path, sr.name if sr else "Unknown", PROJECT_EPSG)
        _err(msg)
        raise ValueError(msg)
    if sr.factoryCode and int(sr.factoryCode) != PROJECT_EPSG:
        _warn("Mosaic '{}' is {}, not the project EPSG:{}. Proceeding.".format(
            path, _crs_label(sr), PROJECT_EPSG))
    return sr


class DeriveSurfaces(object):
    def __init__(self):
        self.label = "02 - Generate Surfaces"
        self.description = ("Derive topographic surfaces (slope, aspect, hillshade, profile "
                            "and plan curvature) in batch from the per mina DEM and DSM mosaics "
                            "produced by Build Mosaics By Polygon. Each surface has its own "
                            "checkbox. Only slope and aspect feed the reclassification tool.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Input mosaics folder", name="in_mosaics_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_out = arcpy.Parameter(
            displayName="Output folder (per_mina_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Output")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_mina_subfolder", "flat"]
        p_struct.value = "same_as_input"

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["BOTH", "DEM", "DSM"]
        p_source.value = "BOTH"

        p_slope = arcpy.Parameter(
            displayName="Slope (degrees)", name="do_slope",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_slope.value = True

        p_aspect = arcpy.Parameter(
            displayName="Aspect", name="do_aspect",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_aspect.value = True

        p_hill = arcpy.Parameter(
            displayName="Hillshade", name="do_hillshade",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_hill.value = True

        p_profc = arcpy.Parameter(
            displayName="Profile curvature", name="do_profile_curvature",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_profc.value = True

        p_planc = arcpy.Parameter(
            displayName="Plan curvature", name="do_plan_curvature",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_planc.value = True

        p_z = arcpy.Parameter(
            displayName="Z factor", name="z_factor",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_z.value = 1

        p_htype = arcpy.Parameter(
            displayName="Hillshade type", name="hillshade_type",
            datatype="GPString", parameterType="Required", direction="Input")
        p_htype.filter.type = "ValueList"
        p_htype.filter.list = ["Multidirectional", "Traditional"]
        p_htype.value = "Multidirectional"

        p_az = arcpy.Parameter(
            displayName="Hillshade azimuth (Traditional)", name="hillshade_azimuth",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_az.value = 315

        p_alt = arcpy.Parameter(
            displayName="Hillshade altitude (Traditional)", name="hillshade_altitude",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_alt.value = 45

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_out, p_struct, p_source, p_slope, p_aspect, p_hill,
                p_profc, p_planc, p_z, p_htype, p_az, p_alt, p_overwrite]

    def isLicensed(self):
        # Needs Spatial Analyst (slope/aspect/curvature/traditional hillshade). Image Analyst
        # is checked out at run time only for multidirectional hillshade.
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def updateParameters(self, parameters):
        # Output folder is only needed for per_mina_subfolder or flat output.
        parameters[2].enabled = parameters[3].valueAsText != "same_as_input"
        # Azimuth/altitude only matter for a Traditional hillshade.
        traditional = bool(parameters[7].value) and parameters[11].valueAsText == "Traditional"
        parameters[12].enabled = traditional
        parameters[13].enabled = traditional
        return

    def updateMessages(self, parameters):
        if parameters[3].valueAsText != "same_as_input" and not parameters[2].valueAsText:
            parameters[2].setErrorMessage("Output folder is required unless output structure is "
                                          "same_as_input.")
        return

    def execute(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        out_folder = parameters[2].valueAsText
        output_structure = parameters[3].valueAsText
        source_filter = parameters[4].valueAsText
        do_slope = bool(parameters[5].value)
        do_aspect = bool(parameters[6].value)
        do_hillshade = bool(parameters[7].value)
        do_profc = bool(parameters[8].value)
        do_planc = bool(parameters[9].value)
        z_factor = float(parameters[10].value)
        hillshade_type = parameters[11].valueAsText
        azimuth = float(parameters[12].value) if parameters[12].value is not None else 315.0
        altitude = float(parameters[13].value) if parameters[13].value is not None else 45.0
        overwrite_existing = bool(parameters[14].value)

        arcpy.env.overwriteOutput = overwrite_existing
        multidirectional = do_hillshade and hillshade_type == "Multidirectional"

        if output_structure != "same_as_input" and not out_folder:
            msg = "Output folder is required unless output structure is same_as_input."
            _err(msg)
            raise ValueError(msg)

        wanted = []
        if do_slope:
            wanted.append("SLOPE")
        if do_aspect:
            wanted.append("ASPECT")
        if do_hillshade:
            wanted.append("HILLSHADE")
        if do_profc:
            wanted.append("PROFC")
        if do_planc:
            wanted.append("PLANC")
        if not wanted:
            _warn("No surfaces selected. Nothing to do.")
            return

        # Spatial Analyst is required. Image Analyst only for the multidirectional hillshade.
        if arcpy.CheckExtension("Spatial") != "Available":
            msg = "Spatial Analyst extension is not available. It is required for this tool."
            _err(msg)
            raise RuntimeError(msg)
        arcpy.CheckOutExtension("Spatial")
        ia_checked_out = False
        try:
            if multidirectional:
                if arcpy.CheckExtension("ImageAnalyst") != "Available":
                    msg = ("Multidirectional hillshade needs the Image Analyst extension, which is "
                           "not available. Choose Traditional, or turn hillshade off.")
                    _err(msg)
                    raise RuntimeError(msg)
                arcpy.CheckOutExtension("ImageAnalyst")
                ia_checked_out = True

            allowed_sources = SOURCES if source_filter == "BOTH" else (source_filter,)

            # Discover base mosaics (source set, product None) matching the source filter.
            mosaics = []
            if recurse:
                walker = os.walk(in_folder)
            else:
                only_files = [n for n in os.listdir(in_folder)
                              if os.path.isfile(os.path.join(in_folder, n))]
                walker = [(in_folder, [], only_files)]
            for dirpath, _dirs, files in walker:
                for fn in files:
                    if not fn.lower().endswith(".tif"):
                        continue
                    info = parse_source_and_product(fn)
                    if info["source"] is None or info["product"] is not None or info["mina"] is None:
                        continue                          # not a base mosaic, skip
                    if info["source"] not in allowed_sources:
                        continue
                    mosaics.append((os.path.join(dirpath, fn), info["mina"], info["source"]))

            if not mosaics:
                msg = "No base mosaics ({}) found in '{}'.".format("/".join(allowed_sources), in_folder)
                _err(msg)
                raise ValueError(msg)

            total = len(mosaics)
            created = 0
            arcpy.SetProgressor("step", "Deriving surfaces...", 0, total, 1)
            for path, mina, source in mosaics:
                arcpy.SetProgressorPosition()
                _assert_projected_raster(path)

                if output_structure == "same_as_input":
                    location = os.path.dirname(path)
                elif output_structure == "per_mina_subfolder":
                    location = os.path.join(out_folder, mina)
                else:
                    location = out_folder
                if not os.path.isdir(location):
                    os.makedirs(location)

                # Resolve which selected surfaces still need writing (idempotency).
                targets = {}
                todo = {}
                for product in wanted:
                    target = os.path.join(location, build_output_name(mina, source, product) + ".tif")
                    targets[product] = target
                    if os.path.exists(target) and not overwrite_existing:
                        _msg("{} ({}): {} exists, skipping.".format(mina, source, os.path.basename(target)))
                        todo[product] = False
                    else:
                        todo[product] = True

                if todo.get("SLOPE"):
                    arcpy.sa.Slope(path, "DEGREE", z_factor).save(targets["SLOPE"])
                    created += 1
                if todo.get("ASPECT"):
                    # Aspect takes no z_factor (it is direction only); z_factor applies to
                    # slope, hillshade and curvature.
                    arcpy.sa.Aspect(path).save(targets["ASPECT"])
                    created += 1
                if todo.get("HILLSHADE"):
                    if multidirectional:
                        # arcpy.ia.Hillshade hillshade_type is an integer: 1 = multidirectional,
                        # 0 = single direction. The UI string is mapped to this integer here.
                        arcpy.ia.Hillshade(path, hillshade_type=1,
                                           z_factor=z_factor).save(targets["HILLSHADE"])
                    else:
                        arcpy.sa.Hillshade(path, azimuth, altitude, "SHADOWS", z_factor).save(
                            targets["HILLSHADE"])
                    created += 1

                # Profile and plan curvature share one Curvature call; pass only the wanted outputs.
                want_profc = todo.get("PROFC", False)
                want_planc = todo.get("PLANC", False)
                if want_profc or want_planc:
                    # "#" skips an unwanted optional output. The returned standard curvature
                    # is intentionally discarded; only profile and plan are kept.
                    arcpy.sa.Curvature(path, z_factor,
                                       targets["PROFC"] if want_profc else "#",
                                       targets["PLANC"] if want_planc else "#")
                    created += (1 if want_profc else 0) + (1 if want_planc else 0)

                _msg("{} ({}): surfaces done.".format(mina, source))

            arcpy.ResetProgressor()
            _msg("Done. Mosaics processed: {}. Surfaces created: {}.".format(total, created))
        finally:
            arcpy.CheckInExtension("Spatial")
            if ia_checked_out:
                arcpy.CheckInExtension("ImageAnalyst")
        return


class ReclassifyFactor(object):
    def __init__(self):
        self.label = "04 - Reclassify Slope and Aspect"
        self.description = ("Reclassify Slope and Aspect factor rasters into ordinal integer "
                            "classes defined in a value table, in batch, with [min, max) "
                            "semantics (the last class inclusive at the top). Uses numpy for "
                            "deterministic boundaries. Curvature and hillshade are not "
                            "reclassified.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Input factor rasters folder", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_factor = arcpy.Parameter(
            displayName="Factor to process", name="factor_to_process",
            datatype="GPString", parameterType="Required", direction="Input")
        p_factor.filter.type = "ValueList"
        p_factor.filter.list = ["BOTH", "SLOPE", "ASPECT"]
        p_factor.value = "BOTH"

        p_slope = arcpy.Parameter(
            displayName="Slope classes (class_id, min, max)", name="slope_classes",
            datatype="GPValueTable", parameterType="Optional", direction="Input")
        p_slope.columns = [["GPLong", "class_id"], ["GPDouble", "min_value"], ["GPDouble", "max_value"]]

        p_aspect = arcpy.Parameter(
            displayName="Aspect classes (class_id, min, max)", name="aspect_classes",
            datatype="GPValueTable", parameterType="Optional", direction="Input")
        p_aspect.columns = [["GPLong", "class_id"], ["GPDouble", "min_value"], ["GPDouble", "max_value"]]

        p_flat = arcpy.Parameter(
            displayName="Flat class value (Aspect -1)", name="flat_class_value",
            datatype="GPLong", parameterType="Optional", direction="Input")

        p_out = arcpy.Parameter(
            displayName="Output folder (per_mina_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Output")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_mina_subfolder", "flat"]
        p_struct.value = "same_as_input"

        p_nodata = arcpy.Parameter(
            displayName="Unmapped values to NoData", name="nodata_for_unmapped",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_nodata.value = True

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_factor, p_slope, p_aspect, p_flat, p_out,
                p_struct, p_nodata, p_overwrite]

    def isLicensed(self):
        # Pure numpy reclassification (RasterToNumPyArray / NumPyArrayToRaster). No extension.
        return True

    def updateParameters(self, parameters):
        factor = parameters[2].valueAsText
        inc_slope = factor in ("SLOPE", "BOTH")
        inc_aspect = factor in ("ASPECT", "BOTH")
        parameters[3].enabled = inc_slope
        parameters[4].enabled = inc_aspect
        parameters[5].enabled = inc_aspect
        # Output folder is only needed for per_mina_subfolder or flat output.
        parameters[6].enabled = parameters[7].valueAsText != "same_as_input"
        return

    def updateMessages(self, parameters):
        # Validate the class tables early, so gaps/overlaps surface in the dialog.
        factor = parameters[2].valueAsText
        to_check = []
        if factor in ("SLOPE", "BOTH") and parameters[3].value:
            to_check.append((3, parameters[3].value))
        if factor in ("ASPECT", "BOTH") and parameters[4].value:
            to_check.append((4, parameters[4].value))
        for idx, table in to_check:
            try:
                validate_value_table(table)
            except Exception as exc:
                parameters[idx].setErrorMessage(str(exc))
        if parameters[7].valueAsText != "same_as_input" and not parameters[6].valueAsText:
            parameters[6].setErrorMessage("Output folder is required unless output structure is "
                                          "same_as_input.")
        return

    def execute(self, parameters, messages):
        import numpy as np
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        factor = parameters[2].valueAsText
        slope_vt = parameters[3].value
        aspect_vt = parameters[4].value
        flat_param = parameters[5].value
        out_folder = parameters[6].valueAsText
        output_structure = parameters[7].valueAsText
        nodata_for_unmapped = bool(parameters[8].value)
        overwrite_existing = bool(parameters[9].value)

        arcpy.env.overwriteOutput = overwrite_existing

        if output_structure != "same_as_input" and not out_folder:
            msg = "Output folder is required unless output structure is same_as_input."
            _err(msg)
            raise ValueError(msg)

        inc_slope = factor in ("SLOPE", "BOTH")
        inc_aspect = factor in ("ASPECT", "BOTH")

        # Validate the class tables BEFORE any write (fail loud).
        slope_classes = None
        aspect_classes = None
        if inc_slope:
            if not slope_vt:
                msg = "Slope classes table is required when processing SLOPE."
                _err(msg)
                raise ValueError(msg)
            slope_classes = validate_value_table(slope_vt)
        if inc_aspect:
            if not aspect_vt:
                msg = "Aspect classes table is required when processing ASPECT."
                _err(msg)
                raise ValueError(msg)
            aspect_classes = validate_value_table(aspect_vt)

        flat_class = int(flat_param) if flat_param is not None else None
        if flat_class is not None and not inc_aspect:
            _warn("flat_class_value is set but ASPECT is not being processed; the flat class "
                  "applies only to Aspect and will be ignored.")

        # The NoData sentinel must not collide with a real class id, or the flat class when used.
        used_ids = [cid for cid, _, _ in (slope_classes or [])]
        used_ids += [cid for cid, _, _ in (aspect_classes or [])]
        if flat_class is not None and inc_aspect:
            used_ids.append(flat_class)
        if RECLASS_NODATA in used_ids:
            msg = ("Class id {} collides with the NoData value used for reclassified outputs. "
                   "Use a different class id.").format(RECLASS_NODATA)
            _err(msg)
            raise ValueError(msg)

        wanted_products = []
        if inc_slope:
            wanted_products.append("SLOPE")
        if inc_aspect:
            wanted_products.append("ASPECT")

        # Discover factor rasters (Slope/Aspect, not already reclassified).
        rasters = []
        if recurse:
            walker = os.walk(in_folder)
        else:
            only_files = [n for n in os.listdir(in_folder)
                          if os.path.isfile(os.path.join(in_folder, n))]
            walker = [(in_folder, [], only_files)]
        for dirpath, _dirs, files in walker:
            for fn in files:
                if not fn.lower().endswith(".tif"):
                    continue
                info = parse_source_and_product(fn)
                if (info["product"] not in wanted_products or info["mina"] is None
                        or info["source"] is None or info["reclass"]):
                    continue
                rasters.append((os.path.join(dirpath, fn), info["mina"], info["source"], info["product"]))

        if not rasters:
            msg = "No {} factor rasters found in '{}'.".format("/".join(wanted_products), in_folder)
            _err(msg)
            raise ValueError(msg)

        total = len(rasters)
        created = 0
        skipped_existing = 0
        arcpy.SetProgressor("step", "Reclassifying factors...", 0, total, 1)
        for path, mina, source, product in rasters:
            arcpy.SetProgressorPosition()
            out_name = build_output_name(mina, source, product, reclass=True) + ".tif"
            if output_structure == "same_as_input":
                location = os.path.dirname(path)
            elif output_structure == "per_mina_subfolder":
                location = os.path.join(out_folder, mina)
            else:
                location = out_folder
            if not os.path.isdir(location):
                os.makedirs(location)
            out_path = os.path.join(location, out_name)

            if os.path.exists(out_path) and not overwrite_existing:
                _msg("{} ({}): {} exists, skipping.".format(mina, source, out_name))
                skipped_existing += 1
                continue

            src = arcpy.Raster(path)
            sr = src.spatialReference
            if sr is None or sr.name in (None, "", "Unknown"):
                _warn("{} ({}): input {} has an undefined CRS; the output CRS will be undefined "
                      "too.".format(mina, source, product))
            lower_left = arcpy.Point(src.extent.XMin, src.extent.YMin)
            cell_w = src.meanCellWidth
            cell_h = src.meanCellHeight

            # Read as float (Slope/Aspect are float; an integer array cannot hold NaN) and map the
            # raster's own NoData to NaN explicitly, so reclassify_array sends it to output NoData.
            try:
                arr = arcpy.RasterToNumPyArray(path).astype("float32")
            except MemoryError:
                msg = ("{} ({}) {} is too large to load into memory for reclassification. Reduce "
                       "the AOI extent or process fewer rasters at a time.").format(mina, source, product)
                _err(msg)
                raise
            nodata_val = src.noDataValue
            if nodata_val is not None:
                arr[arr == nodata_val] = float("nan")

            classes = slope_classes if product == "SLOPE" else aspect_classes
            use_flat = product == "ASPECT" and flat_class is not None
            out_arr = reclassify_array(arr, classes, RECLASS_NODATA,
                                       flat_value=-1 if use_flat else None,
                                       flat_class=flat_class if use_flat else None)

            # Strict coverage: with the option off, any real-data cell that fell outside all
            # classes is an error (the table does not cover the value range).
            if not nodata_for_unmapped:
                with np.errstate(invalid="ignore"):
                    unmapped = (out_arr == RECLASS_NODATA) & ~np.isnan(arr)
                    if use_flat:
                        unmapped &= (arr != -1)
                n_unmapped = int(unmapped.sum())
                if n_unmapped:
                    msg = ("{} ({}) {}: {} cells fall outside all classes and 'Unmapped values to "
                           "NoData' is off. Extend the class table to cover the full value range, "
                           "or turn the option on.").format(mina, source, product, n_unmapped)
                    _err(msg)
                    raise ValueError(msg)

            out_raster = arcpy.NumPyArrayToRaster(out_arr, lower_left, cell_w, cell_h,
                                                  value_to_nodata=RECLASS_NODATA)
            out_raster.save(out_path)
            arcpy.management.DefineProjection(out_path, sr)        # NumPyArrayToRaster leaves CRS undefined
            _msg("{} ({}): reclassified {} ({}) -> {}".format(
                mina, source, product, _crs_label(sr), out_name))
            created += 1

        arcpy.ResetProgressor()
        _msg("Done. Factor rasters: {}. Reclassified: {}. Skipped existing: {}. Output NoData: {}.".format(
            total, created, skipped_existing, RECLASS_NODATA))
        return


class SolarRadiation(object):
    def __init__(self):
        self.label = "03 - Solar Radiation"
        self.description = ("Compute annual incoming solar radiation (global, kWh/m2) per mina from the "
                            "DEM mosaics, in batch, with arcpy.sa.RasterSolarRadiation (GPU accelerated). "
                            "The DEM is resampled to a coarser solar cell size first, because annual "
                            "insolation is a smooth field, to keep the heavy whole year computation "
                            "tractable. This is the heavy tool; expect long run times.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Input mosaics folder", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_out = arcpy.Parameter(
            displayName="Output folder (per_mina_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Output")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_mina_subfolder", "flat"]
        p_struct.value = "same_as_input"

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM", "BOTH"]
        p_source.value = "DEM"

        p_cell = arcpy.Parameter(
            displayName="Solar cell size in meters (0 = native)", name="solar_cell_size",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_cell.value = 10

        p_method = arcpy.Parameter(
            displayName="Resample method", name="resample_method",
            datatype="GPString", parameterType="Required", direction="Input")
        p_method.filter.type = "ValueList"
        p_method.filter.list = ["BILINEAR", "CUBIC", "NEAREST"]
        p_method.value = "BILINEAR"

        p_year = arcpy.Parameter(
            displayName="Year (whole year; only sets leap year)", name="year",
            datatype="GPLong", parameterType="Required", direction="Input")
        p_year.value = 2023

        p_neigh = arcpy.Parameter(
            displayName="Shadow neighborhood distance", name="neighborhood_distance",
            datatype="GPLinearUnit", parameterType="Required", direction="Input")
        p_neigh.value = "1000 Meters"

        p_trans = arcpy.Parameter(
            displayName="Transmittivity (0-1)", name="transmittivity",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_trans.value = 0.6

        p_diff = arcpy.Parameter(
            displayName="Diffuse proportion (0-1)", name="diffuse_proportion",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_diff.value = 0.3

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_out, p_struct, p_source, p_cell, p_method,
                p_year, p_neigh, p_trans, p_diff, p_overwrite]

    def isLicensed(self):
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def updateParameters(self, parameters):
        parameters[2].enabled = parameters[3].valueAsText != "same_as_input"
        return

    def updateMessages(self, parameters):
        if parameters[3].valueAsText != "same_as_input" and not parameters[2].valueAsText:
            parameters[2].setErrorMessage("Output folder is required unless output structure is "
                                          "same_as_input.")
        return

    def execute(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        out_folder = parameters[2].valueAsText
        output_structure = parameters[3].valueAsText
        source_filter = parameters[4].valueAsText
        solar_cell_size = float(parameters[5].value)
        resample_method = parameters[6].valueAsText
        year = int(parameters[7].value)
        neighborhood = parameters[8].valueAsText
        transmittivity = float(parameters[9].value)
        diffuse_proportion = float(parameters[10].value)
        overwrite_existing = bool(parameters[11].value)

        # Idempotency is handled by an explicit os.path.exists skip below, so overwrite is
        # left on to let the scratch resample step overwrite a stale temp cleanly.
        arcpy.env.overwriteOutput = True

        if arcpy.CheckExtension("Spatial") != "Available":
            msg = "Spatial Analyst extension is not available. It is required for this tool."
            _err(msg)
            raise RuntimeError(msg)
        if not hasattr(arcpy.sa, "RasterSolarRadiation"):
            msg = "arcpy.sa.RasterSolarRadiation is not available in this ArcGIS Pro build."
            _err(msg)
            raise RuntimeError(msg)
        if output_structure != "same_as_input" and not out_folder:
            msg = "Output folder is required unless output structure is same_as_input."
            _err(msg)
            raise ValueError(msg)

        allowed_sources = SOURCES if source_filter == "BOTH" else (source_filter,)
        start_date = "1/1/{}".format(year)
        end_date = "12/31/{}".format(year)

        # Discover base mosaics (source set, product None) matching the source filter.
        mosaics = []
        if recurse:
            walker = os.walk(in_folder)
        else:
            only_files = [n for n in os.listdir(in_folder)
                          if os.path.isfile(os.path.join(in_folder, n))]
            walker = [(in_folder, [], only_files)]
        for dirpath, _dirs, files in walker:
            for fn in files:
                if not fn.lower().endswith(".tif"):
                    continue
                info = parse_source_and_product(fn)
                if info["source"] is None or info["product"] is not None or info["mina"] is None:
                    continue
                if info["source"] not in allowed_sources:
                    continue
                mosaics.append((os.path.join(dirpath, fn), info["mina"], info["source"]))

        if not mosaics:
            msg = "No base mosaics ({}) found in '{}'.".format("/".join(allowed_sources), in_folder)
            _err(msg)
            raise ValueError(msg)

        total = len(mosaics)
        _warn("Solar radiation is a heavy whole year computation. {} mosaic(s) at a {} solar cell size "
              "may take a long time; benchmark one mina first.".format(
                  total, "{} m".format(solar_cell_size) if solar_cell_size > 0 else "native"))

        arcpy.CheckOutExtension("Spatial")
        created = 0
        skipped_existing = 0
        failed = []
        try:
            arcpy.SetProgressor("step", "Computing solar radiation...", 0, total, 1)
            for path, mina, source in mosaics:
                arcpy.SetProgressorPosition()
                if source == "DSM":
                    _warn("{}: solar is computed on the DSM (canopy and building surface), not the "
                          "bare ground a PV plant would sit on.".format(mina))

                out_name = build_output_name(mina, source, "SOLAR") + ".tif"
                location = out_folder
                if output_structure == "same_as_input":
                    location = os.path.dirname(path)
                elif output_structure == "per_mina_subfolder":
                    location = os.path.join(out_folder, mina)
                if not os.path.isdir(location):
                    os.makedirs(location)
                out_path = os.path.join(location, out_name)

                if os.path.exists(out_path) and not overwrite_existing:
                    _msg("{} ({}): {} exists, skipping.".format(mina, source, out_name))
                    skipped_existing += 1
                    continue

                resampled = None
                try:
                    sr = _assert_projected_raster(path)
                    native = arcpy.Raster(path).meanCellWidth
                    surface = path
                    if solar_cell_size and solar_cell_size > native:
                        resampled = os.path.join(arcpy.env.scratchFolder, "solar_{}.tif".format(mina))
                        arcpy.management.Resample(
                            path, resampled, "{0} {0}".format(solar_cell_size), resample_method)
                        surface = resampled

                    rad = arcpy.sa.RasterSolarRadiation(
                        in_surface_raster=surface,
                        start_date_time=start_date,
                        end_date_time=end_date,
                        use_time_interval="NO_INTERVAL",
                        neighborhood_distance=neighborhood,
                        use_adaptive_neighborhood="ADAPTIVE_NEIGHBORHOOD",
                        diffuse_model_type="UNIFORM_SKY",
                        diffuse_proportion=diffuse_proportion,
                        transmittivity=transmittivity,
                        analysis_target_device="GPU_THEN_CPU",
                    )
                    rad.save(out_path)
                    _msg("{} ({}): solar radiation (kWh/m2, {}) -> {}".format(
                        mina, source, _crs_label(sr), out_name))
                    created += 1
                except Exception as exc:
                    _warn("{} ({}): solar radiation failed: {}".format(mina, source, exc))
                    failed.append(mina)
                finally:
                    if resampled and arcpy.Exists(resampled):
                        arcpy.management.Delete(resampled)

            arcpy.ResetProgressor()
            _msg("Done. Mosaics: {}. Solar rasters created: {}. Skipped existing: {}. Failed: {}.".format(
                total, created, skipped_existing, len(failed)))
            if failed:
                _warn("Failed minas: " + ", ".join(failed))
        finally:
            arcpy.CheckInExtension("Spatial")
        return


# ===========================================================================
# Self tests (pure functions; numpy tests run only if numpy is importable).
# Run: python MiningTerrainToolbox.pyt
# ===========================================================================

def _run_self_tests():
    failures = []

    def check(desc, cond):
        print(("  ok   " if cond else "  FAIL ") + desc)
        if not cond:
            failures.append(desc)

    def check_raises(desc, fn):
        try:
            fn()
        except Exception:
            print("  ok   " + desc)
            return
        print("  FAIL " + desc + " (expected an exception)")
        failures.append(desc)

    print("sanitize_name")
    check("accents removed", sanitize_name("Sao Joao") == "Sao_Joao")
    check("accents removed (with diacritics)", sanitize_name("São João") == "Sao_Joao")
    check("c cedilha to c", sanitize_name("Mação") == "Macao")
    check("spaces to underscore", sanitize_name("Mina Norte") == "Mina_Norte")
    check("hyphen separator to underscore", sanitize_name("Sao-Chico") == "Sao_Chico")
    check("leading digit prefixed", sanitize_name("2 Minas").startswith("M_"))
    check("collapses repeated underscores", "__" not in sanitize_name("Mina   Norte"))
    check("truncated to limit", len(sanitize_name("A" * 100)) <= MAX_NAME_LEN)
    check_raises("empty result raises", lambda: sanitize_name("***"))
    check_raises("None raises", lambda: sanitize_name(None))

    print("dedupe_name")
    used = {"MinaA", "MinaA_2"}
    check("collision appends next free suffix", dedupe_name("MinaA", used) == "MinaA_3")
    check("no collision unchanged", dedupe_name("MinaB", used) == "MinaB")

    print("build_output_name / parse_source_and_product round trip")
    cases = [
        ("Sao_Domingos", "DEM", "SLOPE", False),
        ("Sao_Domingos", "DEM", "SLOPE", True),
        ("Mina_Norte", "DSM", "ASPECT", False),
        ("MinaA", "DEM", None, False),               # base mosaic
        ("MinaA", "DEM", "SOLAR", False),
    ]
    for mina, source, product, reclass in cases:
        name = build_output_name(mina, source, product, reclass)
        p = parse_source_and_product(name + ".tif")
        check("round trip {}".format(name),
              p["mina"] == mina and p["source"] == source
              and p["product"] == product and p["reclass"] == reclass)

    check("mina with underscore not misparsed",
          parse_source_and_product("Sao_Domingos_DEM_SLOPE.tif")["mina"] == "Sao_Domingos")
    check("unknown name yields source None",
          parse_source_and_product("random_file.tif")["source"] is None)
    check("mina ending in mixed case token kept (case sensitive parse)",
          parse_source_and_product("Vale_Dem.tif")["mina"] == "Vale_Dem"
          and parse_source_and_product("Vale_Dem.tif")["source"] is None)
    check("lowercase tokens not recognized",
          parse_source_and_product("MinaA_dem_slope.tif")["source"] is None)
    check("empty name yields mina None", parse_source_and_product("")["mina"] is None)
    check("build rejects unknown source", _raises(lambda: build_output_name("X", "DTM")))
    check("build rejects reclass without product", _raises(lambda: build_output_name("X", "DEM", None, True)))

    print("validate_value_table")
    check("valid tiling returns parsed rows",
          validate_value_table([(1, 0, 10), (2, 10, 20), (3, 20, 30)]) is not None)
    check("same class_id repeated passes (Aspect north split)",
          validate_value_table([(1, 0, 45), (2, 45, 135), (3, 135, 225),
                                 (4, 225, 315), (1, 315, 360)]) is not None)
    check_raises("gap raises", lambda: validate_value_table([(1, 0, 10), (2, 20, 30)]))
    check_raises("different id overlap raises", lambda: validate_value_table([(1, 0, 15), (2, 10, 20)]))
    check_raises("min not less than max raises", lambda: validate_value_table([(1, 10, 10)]))
    check_raises("empty table raises", lambda: validate_value_table([]))
    check_raises("fractional class_id raises (would mask overlap)",
                 lambda: validate_value_table([(1.4, 0, 10), (1.9, 5, 15)]))
    check_raises("bool class_id raises", lambda: validate_value_table([(True, 0, 10)]))
    check("whole number float class_id accepted",
          validate_value_table([(1.0, 0, 10), (2.0, 10, 20)]) is not None)

    print("build_mina_groups")
    groups = build_mina_groups([(3, "Cortes Pereira"), (4, "Cortes Pereira"), (0, "Alcaria Queimada")])
    check("merges same mina across fids",
          ("Cortes_Pereira", [3, 4]) in groups and ("Alcaria_Queimada", [0]) in groups)
    check("ordered by smallest fid", groups[0][0] == "Alcaria_Queimada")
    g2 = build_mina_groups([(0, "São João"), (1, "Sao Joao")])
    check("different names that collide get deduped",
          [n for n, _ in g2] == ["Sao_Joao", "Sao_Joao_2"])
    check("each colliding group keeps its own fid", g2[0][1] == [0] and g2[1][1] == [1])

    print("_parse_vrt_extent")
    vrt = ('<VRTDataset rasterXSize="100" rasterYSize="50">'
           '<SRS>EPSG:3763</SRS>'
           '<GeoTransform>1000.0, 2.0, 0.0, 5000.0, 0.0, -2.0</GeoTransform>'
           '</VRTDataset>')
    check("extent from geotransform",
          _parse_vrt_extent(vrt) == (1000.0, 4900.0, 1200.0, 5000.0))
    check_raises("vrt without geotransform raises",
                 lambda: _parse_vrt_extent('<VRTDataset rasterXSize="1" rasterYSize="1"></VRTDataset>'))

    print("detect_folder_prefix")
    check("common prefix detected",
          detect_folder_prefix(["02_DGT_LiDAR_Data_0", "02_DGT_LiDAR_Data_1",
                                 "02_DGT_LiDAR_Data_21"]) == "02_DGT_LiDAR_Data_")
    check("padding does not change prefix",
          detect_folder_prefix(["X_007", "X_7"]) == "X_")
    check_raises("inconsistent prefixes raise",
                 lambda: detect_folder_prefix(["a_7", "b_8"]))
    check_raises("no trailing number raises",
                 lambda: detect_folder_prefix(["nodigits", "also_none"]))

    print("reclassify_array")
    try:
        import numpy as _np
    except ImportError:
        print("  skip (numpy not available)")
    else:
        cl = [(1, 0, 10), (2, 10, 20), (3, 20, 30)]
        out = reclassify_array(_np.array([[0.0, 9.9, 10.0], [20.0, 29.9, 30.0]]), cl, 9999)
        check("[min, max) bins and last class inclusive",
              out.tolist() == [[1, 1, 2], [3, 3, 3]])
        out2 = reclassify_array(_np.array([[40.0, float("nan")]]), cl, 9999)
        check("out of range and NaN go to NoData", out2.tolist() == [[9999, 9999]])
        asp = [(1, 0, 45), (2, 45, 135), (3, 135, 225), (4, 225, 315), (1, 315, 360)]
        out3 = reclassify_array(_np.array([[0.0, 350.0, 360.0, -1.0]]), asp, 9999,
                                flat_value=-1, flat_class=9)
        check("aspect split classes plus flat", out3.tolist() == [[1, 1, 1, 9]])

    print("")
    if failures:
        print("{} test(s) FAILED".format(len(failures)))
        return 1
    print("all tests passed")
    return 0


def _raises(fn):
    """Helper for inline assertions: True if fn() raises."""
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    import sys
    sys.exit(_run_self_tests())
