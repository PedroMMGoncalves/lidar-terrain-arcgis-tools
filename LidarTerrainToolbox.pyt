# -*- coding: utf-8 -*-
#
# LiDAR Terrain Toolbox - derives topographic factors from LiDAR DEM and DSM.
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
LiDAR Terrain Toolbox

Single file ArcGIS Pro Python Toolbox (.pyt) that derives topographic factors from
LiDAR elevation data (DEM/DSM) in batch, per area of interest: slope, aspect, hillshade,
profile and plan curvature, and annual solar radiation. Factor engine only; any
downstream analysis is external, outside this toolbox.

Project CRS: ETRS89 / PT-TM06, EPSG:3763. Horizontal and vertical units in meters,
so Z factor = 1.

Everything lives in this one file by design (easier to maintain and share, and it
removes the fragile sys.path import of a separate helpers module):
  - shared helpers (sanitize, naming, CRS, value table validation)
  - the Toolbox class and its Tool classes (added incrementally)
  - pure unit tests in the __main__ block

Run the pure tests outside ArcGIS with:
    python LidarTerrainToolbox.pyt
Under ArcGIS the __main__ block never runs (ArcGIS imports the module, it does not
execute it as a script).
"""

import os
import math
import re
import json
import urllib.parse
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

try:
    import arcpy
except ImportError:
    # Lets the pure helpers and the self tests run outside ArcGIS. Under ArcGIS,
    # arcpy is always importable. numpy is imported lazily inside ReclassifyFactor
    # (Tool 05) so this module loads without numpy when only the pure tests are run.
    arcpy = None

try:
    import requests
except ImportError:
    # Only the download tool (Tool 01) needs requests; guard it so the module and the pure
    # tests load without it. The download tool fails loud if requests is missing.
    requests = None


# ===========================================================================
# Constants
# ===========================================================================

PROJECT_EPSG = 3763                                  # ETRS89 / PT-TM06, meters
SOURCES = ("DEM", "DSM")
PRODUCTS = ("SLOPE", "SLOPEP", "ASPECT", "ASPECT_DIR", "HILLSHADE", "PROFC", "PLANC", "SOLAR",
            "SOLARUNI", "SOLAROVC", "SOLARUNIDIR", "SOLARUNIDIF", "SOLARUNIDUR",
            "SOLAROVCDIR", "SOLAROVCDIF", "SOLAROVCDUR", "CONT", "APT", "APTCLS")
# Products whose values are classes, not a continuous measure: interpolating them would invent
# fractional classes, so they resample with nearest or majority, never bilinear or cubic.
CATEGORICAL_PRODUCTS = ("ASPECT_DIR", "APT", "APTCLS")
# ASPECT is continuous but must not be interpolated either: it is circular (averaging 355 and 5
# degrees gives 180, so north reads as south) and carries -1 for flat as a sentinel (interpolating
# it invents azimuths that fall in no class). Nearest keeps it honest; the right way to change its
# cell size is to recompute aspect from a resampled DEM.
CIRCULAR_PRODUCTS = ("ASPECT",)
# The products Tool 05 reclassifies, so the _RCL variants exist for exactly these.
RECLASSIFIED_PRODUCTS = ("SLOPE", "ASPECT", "SOLARUNI")
RECLASS_SUFFIX = "RCL"
MAX_NAME_LEN = 40                                    # margin for suffixes like _DEM_ASPECT_RCL
RECLASS_NODATA = 9999                                # NoData for reclassified integer outputs
RESAMPLE_DIRNAME = "Resample"                        # output subfolder created by the Resample tool
RECLASS_DIRNAME = "Reclass"                          # output subfolder created by the Reclassify tool
CONTOURS_DIRNAME = "Contours"                        # output subfolder created by the Contours tool

# Fixed project reclassification schemes (suitability classes). Each list is consumed by
# reclassify_array as (class_id, lo, hi) with [lo, hi) intervals, the class with the largest
# hi inclusive at the top. Aspect uses -1 for flat, handled by the flat_class override.
SLOPE_BREAK_DEG = math.degrees(math.atan(0.20))      # 20 percent grade equals 11.31 degrees
# Aspect by quadrants (categorical, 45 degree bins). North wraps around 0/360.
ASPECT_DIR_CLASSES = [(1, 0.0, 22.5), (2, 22.5, 67.5), (3, 67.5, 112.5), (4, 112.5, 157.5),
                      (5, 157.5, 202.5), (6, 202.5, 247.5), (7, 247.5, 292.5), (8, 292.5, 337.5),
                      (1, 337.5, 360.0)]
ASPECT_DIR_FLAT = 9                                   # Plano
# Aspect solar suitability (60 degree sectors; south and flat are best). North wraps.
ASPECT_RCL_CLASSES = [(1, 0.0, 30.0), (2, 30.0, 90.0), (3, 90.0, 150.0), (4, 150.0, 210.0),
                      (3, 210.0, 270.0), (2, 270.0, 330.0), (1, 330.0, 360.0)]
ASPECT_RCL_FLAT = 4                                   # Plano counts as best
# Slope suitability (degrees); the 20 percent break expressed in degrees (matches the Tool 9
# suitability mask default and the project rule of slope at or below 20 percent being suitable).
SLOPE_RCL_CLASSES = [(1, 0.0, SLOPE_BREAK_DEG), (0, SLOPE_BREAK_DEG, 90.0)]
# Annual global irradiation suitability (kWh/m2).
SOLAR_RCL_CLASSES = [(1, 0.0, 1200.0), (2, 1200.0, 1400.0), (3, 1400.0, 1600.0),
                     (4, 1600.0, 1800.0), (5, 1800.0, 2000.0), (6, 2000.0, 100000.0)]

# Legend written into each Reclass subfolder (the class matrix). ASCII to avoid path/encoding
# surprises. The per run header adds the file list above this fixed block.
RECLASS_LEGEND_TEXT = """\
ASPECT_DIR  -  orientacao por quadrantes (categorico, bins de 45 graus)
  ..._{DEM|DSM}_ASPECT_DIR.tif
  1 = N  (337.5-22.5)    2 = NE (22.5-67.5)    3 = E  (67.5-112.5)   4 = SE (112.5-157.5)
  5 = S  (157.5-202.5)   6 = SW (202.5-247.5)  7 = W  (247.5-292.5)  8 = NW (292.5-337.5)
  9 = Plano (-1)

ASPECT_RCL  -  aptidao por orientacao solar (setores de 60 graus; sul/plano = melhor)
  ..._{DEM|DSM}_ASPECT_RCL.tif
  1 = Norte (330-30)
  2 = NE e NW (30-90 e 270-330)
  3 = SE e SW (90-150 e 210-270)
  4 = Sul (150-210) e Plano (-1)

SLOPE_RCL  -  aptidao por declive (em graus; corte 20% = 11.31 graus)
  ..._{DEM|DSM}_SLOPE_RCL.tif
  1 = apto      (<= 11.31 graus, ou seja <= 20%)
  0 = nao apto  (>  11.31 graus, ou seja >  20%)   (0 e valor real, nao NoData)

SOLARUNI_RCL  -  aptidao por irradiacao global anual (kWh/m2, ceu uniforme)
  ..._DEM_SOLARUNI_RCL.tif
  1 = < 1200    2 = 1200-1400   3 = 1400-1600
  4 = 1600-1800 5 = 1800-2000   6 = >= 2000

Intervalos [a, b): o limite superior pertence a classe seguinte. NoData = 9999."""


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


def _delete_partial_raster(path):
    """Remove a raster output and its sidecars so a re-run does not skip a partial or corrupt write
    and feed it downstream. Tries arcpy first (handles locks and sidecars), then removes the files
    directly in case the raster is too truncated for arcpy to recognize."""
    try:
        if arcpy is not None and arcpy.Exists(path):
            arcpy.management.Delete(path)
    except Exception:
        pass
    for p in (path, path + ".aux.xml", path + ".ovr", path + ".vat.dbf", path + ".xml",
              os.path.splitext(path)[0] + ".tfw"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


class _CleanupOnError(object):
    """Context manager that deletes partial raster outputs if the wrapped write raises, so an
    interrupted or failed write never leaves a truncated file for a later run to skip."""
    def __init__(self, *out_paths):
        self._out_paths = out_paths

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            for path in self._out_paths:
                if path:
                    _delete_partial_raster(path)
        return False


def _build_pyramids_stats(path):
    """Build pyramids and statistics on a written raster so it displays fast in Pro. Best effort:
    a failure here does not invalidate the raster itself, so it warns instead of raising."""
    try:
        arcpy.management.BuildPyramids(path, skip_existing="SKIP_EXISTING")
        arcpy.management.CalculateStatistics(path, skip_existing="SKIP_EXISTING")
    except Exception as exc:
        _warn("Could not build pyramids/statistics for {}: {}".format(os.path.basename(path), exc))


def _build_class_rat(path):
    """Build statistics and a raster attribute table on a merged class raster, so ArcGIS lists its
    exact class values in the symbology. gdal writes the mosaic without a raster attribute table; on
    a large raster ArcGIS then enumerates the values from a pyramid sample and can miss a class (for
    example the 0 that fills most of a suitability mask), so the layer looks empty of it. The
    attribute table gives the exact values and counts instead. Best effort: a failure here does not
    invalidate the raster, so it warns instead of raising."""
    try:
        arcpy.management.CalculateStatistics(path)
        arcpy.management.BuildRasterAttributeTable(path, "Overwrite")
    except Exception as exc:
        _warn("Could not build statistics/attribute table for {}: {}".format(
            os.path.basename(path), exc))


def _save_raster(raster, out_path):
    """Save a Spatial Analyst / Image Analyst raster result, deleting the partial output on error.
    Retries a few times when the existing output cannot be deleted or is locked (often a transient
    lock from antivirus or a brief hold), which would otherwise abort a long batch."""
    import time
    for attempt in range(3):
        try:
            with _CleanupOnError(out_path):
                raster.save(out_path)
            return
        except Exception:
            if attempt == 2:
                raise
            import gc
            gc.collect()          # long batches can exhaust handles/memory; reclaim before retrying
            time.sleep(2)


# The fixed NoData for the categorical suitability masks. 255 is never a class of APT {0,1} or
# APTCLS {0,3,4,5,6}, so it cannot collide with real data, and being fixed it is identical across
# every mine, which lets the per mine rasters merge cleanly.
CLASS_MASK_NODATA = 255


def _save_class_mask(raster, out_path):
    """Save a categorical suitability mask (APT / APTCLS) forcing 8 bit unsigned storage and a
    fixed NoData of 255. Left to itself, raster.save() lets ArcGIS pick the pixel type and NoData
    from the content; on a small or degenerate mine (for example one that is entirely suitable, so
    the only value present is 1) it can pick a NoData that equals a real class (0 or 1), and every
    cell of that class then reads as empty. This must happen at creation, while ExtractByMask still
    marks the outside of the mask as true NoData: a raster already written with a colliding NoData
    cannot be recovered, because the class value and NoData are then indistinguishable.

    CopyRaster forces the pixel type and NoData deterministically: the masked cells are written as
    255 and flagged, the {0,1} / {0,3,4,5,6} data is preserved, and the storage is 8 bit unsigned
    for every mine. (NumPyArrayToRaster was avoided here: it promotes to 16 bit whenever the array
    holds both 0 and the 255 NoData, which is most mines, so the per mine masks would not share one
    pixel type.) CopyRaster keeps the CRS, so no reprojection is needed."""
    for attempt in range(3):
        try:
            with _CleanupOnError(out_path):
                arcpy.management.CopyRaster(raster, out_path, pixel_type="8_BIT_UNSIGNED",
                                            nodata_value=CLASS_MASK_NODATA)
            return
        except Exception:
            if attempt == 2:
                raise
            import gc
            import time
            gc.collect()
            time.sleep(2)


# ===========================================================================
# Helpers (single source of truth for naming, sanitization, validation)
# ===========================================================================

def _merge_sheet_code(s):
    """Join a leading Carta Militar 1:50000 sheet code with its quadrant letters.

    The sheet designation arrives with a separator, for example "1-A Valenca" which
    step 2 of sanitize_name turns into "1_A_Valenca". The cartographic convention writes
    the code closed up: "1A_Valenca". A multi sheet mosaic keeps the underscore between
    sheets: "25_CD_29_A_..." becomes "25CD_29A_...".

    Only a genuine leading run of "<digits>_<1-2 uppercase letters>" groups is affected,
    matched from the start and required to end at an underscore or the string end. So an
    ordinary name whose number runs into a lowercase word, like "Area_16_Artigo", never
    matches and is returned unchanged.
    """
    m = re.match(r"^(\d+_[A-Z]{1,2}(?:_\d+_[A-Z]{1,2})*)(?=_|$)", s)
    if not m:
        return s
    prefix = re.sub(r"(\d)_([A-Z]{1,2})", r"\1\2", m.group(1))
    return prefix + s[m.end():]


def sanitize_name(raw_name, to_geodatabase=False):
    """Convert an Area attribute value into a name safe for ArcGIS and the file system.

    Steps (per spec 3.1):
      1. NFD normalize and drop combining marks (handles accents; c cedilha becomes c).
      2. Replace whitespace and separators with underscore.
      3. Remove anything that is not [A-Za-z0-9_].
      4. Collapse repeated underscores and trim.
      5. Merge a leading cartographic sheet code with its quadrant letters, so
         "1-A Valenca" becomes "1A_Valenca" (see _merge_sheet_code).
      6. Only when to_geodatabase is True and the result starts with a digit, prefix M_.
         A geodatabase dataset name cannot start with a digit, but a file on disk can, so
         the default (file output) keeps the clean name. Every tool here writes .tif
         files, so the default is what they all use; the flag is there for a future
         caller that writes into a .gdb.
      7. Truncate to MAX_NAME_LEN.

    Collision handling is NOT done here. The caller keeps a set of used names and
    calls dedupe_name (spec 3.7). Logs via AddMessage when it changes a name.
    Non string input is coerced via str() (ArcGIS field values may be numeric).
    Raises ValueError (fail loud) if the result is empty.
    """
    if raw_name is None:
        raise ValueError("Area name is None. Cannot build an output name.")
    original = str(raw_name)

    s = unicodedata.normalize("NFD", original)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s/\\\-.,;:|]+", "_", s)           # whitespace and separators to underscore
    s = re.sub(r"[^A-Za-z0-9_]", "", s)              # drop everything else
    s = re.sub(r"_+", "_", s).strip("_")             # collapse and trim underscores

    if not s:
        msg = "Area name '{}' sanitizes to an empty string. Provide a usable name.".format(original)
        _err(msg)
        raise ValueError(msg)

    s = _merge_sheet_code(s)

    if to_geodatabase and s[0].isdigit():
        s = "M_" + s
    if len(s) > MAX_NAME_LEN:
        s = s[:MAX_NAME_LEN].strip("_")

    if s != original:
        _msg("Sanitized area name '{}' to '{}'.".format(original, s))
    return s


def find_sanitize_collisions(raw_names):
    """Map each sanitized name that more than one distinct raw name collapses to, to the sorted
    set of those raw names. Empty when there is no collision.

    Used where different inputs must NOT silently merge into one output (a download folder, a
    mosaic mapping, a mine mask), unlike output names that may be deduped. Pure, so it is tested.
    """
    by_sane = {}
    for raw in raw_names:
        if raw in (None, ""):
            continue
        by_sane.setdefault(sanitize_name(str(raw)), set()).add(str(raw).strip())
    return {name: sorted(raws) for name, raws in by_sane.items() if len(raws) > 1}


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


def build_output_name(area, source, product=None, reclass=False):
    """Centralize the naming convention (spec 3.4). `area` must already be sanitized.

    Mosaic:        {Area}_{SOURCE}                  e.g. AreaA_DEM
    Surface:       {Area}_{SOURCE}_{PRODUCT}        e.g. AreaA_DEM_SLOPE
    Reclassified:  {Area}_{SOURCE}_{PRODUCT}_RCL    e.g. AreaA_DEM_SLOPE_RCL

    The .tif extension is added on write, not here.
    """
    source = source.upper()
    if source not in SOURCES:
        raise ValueError("Unknown source '{}'. Expected one of {}.".format(source, SOURCES))

    parts = [area, source]
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
    area, source, product, reclass.

    Parses right to left against the closed SOURCES and PRODUCTS sets. This is
    deliberate: sanitize_name collapses spaces into underscores, so an area name
    can itself contain underscores (for example Sao_Domingos). A left to right
    split would be ambiguous; anchoring on the rightmost known tokens is robust,
    because build_output_name always appends the real source last.

    Matching is case sensitive (canonical), since build_output_name emits uppercase
    tokens. For a name that does not follow the convention, source comes back as
    None. Callers decide conformance on source is None (a valid mosaic or surface
    always has a source), not on area.
    """
    base = os.path.basename(filename)
    stem = base[:-4] if base.lower().endswith(".tif") else base
    tokens = stem.split("_")

    # Canonical, case sensitive matching. build_output_name only ever emits
    # uppercase SOURCE/PRODUCT/RCL tokens, so exact matching keeps the round trip
    # exact and stops an area name that merely ends in a lower or mixed case word
    # (for example "Vale_Dem") from being mistaken for a real token.
    reclass = False
    if tokens and tokens[-1] == RECLASS_SUFFIX:
        reclass = True
        tokens = tokens[:-1]

    # Try the last TWO tokens joined before the last one alone, so a product token that itself
    # contains an underscore (ASPECT_DIR) parses without renaming the files already on disk.
    product = None
    if len(tokens) >= 2 and "_".join(tokens[-2:]) in PRODUCTS:
        product = "_".join(tokens[-2:])
        tokens = tokens[:-2]
    elif tokens and tokens[-1] in PRODUCTS:
        product = tokens[-1]
        tokens = tokens[:-1]

    source = None
    if tokens and tokens[-1] in SOURCES:
        source = tokens[-1]
        tokens = tokens[:-1]

    area = "_".join(tokens) if tokens else None
    if not area:
        area = None                                  # normalize "" (junk input) to None
    return {"area": area, "source": source, "product": product, "reclass": reclass}


def _resample_type_token(info):
    """Type token used by the Resample type filter, from a parse_source_and_product dict.

    A base mosaic is its source (DEM, DSM), a surface is its product (SLOPE, ASPECT, ...),
    and a reclassified raster gets a _RCL suffix (SLOPE_RCL). Returns None when the name has
    no known source, so non conforming files are never selected.
    """
    if info["source"] is None:
        return None
    token = info["product"] or info["source"]
    if info["reclass"]:
        token = token + "_" + RECLASS_SUFFIX
    return token


def _walk_tifs(root, recurse, skip_dirs=()):
    """Yield (dirpath, filename) for every .tif under root, pruning skip_dirs (case insensitive).

    One place for the recurse/no-recurse walk that eight discovery loops used to inline, and one
    place for the pruning, so the skip policy is a visible argument instead of a per tool decision
    (which had drifted between case sensitive and case insensitive). Pass the subfolders a tool
    must not descend into, for example RESAMPLE_DIRNAME to avoid re-processing resampled copies.
    """
    skip = {d.lower() for d in skip_dirs}
    if recurse:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in skip]
            for fn in files:
                if fn.lower().endswith(".tif"):
                    yield dirpath, fn
    else:
        for fn in os.listdir(root):
            if fn.lower().endswith(".tif") and os.path.isfile(os.path.join(root, fn)):
                yield root, fn


def _find_base_mosaics(root, recurse, allowed_sources, skip_dirs=(RESAMPLE_DIRNAME,)):
    """Discover the base mosaics (a name with a source and no product) whose source is allowed.

    Returns a list of (path, area, source); raises the shared fail-loud error when none is found.
    Used by the surface, solar and contour tools, which all consume base mosaics the same way.
    """
    mosaics = []
    for dirpath, fn in _walk_tifs(root, recurse, skip_dirs):
        info = parse_source_and_product(fn)
        if info["source"] is None or info["product"] is not None or info["area"] is None:
            continue
        if info["source"] not in allowed_sources:
            continue
        mosaics.append((os.path.join(dirpath, fn), info["area"], info["source"]))
    if not mosaics:
        msg = "No base mosaics ({}) found in '{}'.".format("/".join(allowed_sources), root)
        _err(msg)
        raise ValueError(msg)
    return mosaics


def _resample_all_types():
    """The full list of resample data-type tokens, from the naming convention: the sources, the
    products (except CONT, which is a shapefile), and the _RCL reclassified variants. One place,
    used by the tool's dropdown and by the present-types scan."""
    return (list(SOURCES) + [p for p in PRODUCTS if p != "CONT"]
            + [p + "_" + RECLASS_SUFFIX for p in RECLASSIFIED_PRODUCTS])


def _present_type_tokens(folder, recurse, skip_dirs, allowed):
    """The subset of `allowed` type tokens that actually have a raster under `folder`. Reads file
    names only (no raster is opened), so it is cheap enough to fill a dialog dropdown from the real
    contents instead of the full static list. Pure (os + string parsing), so it is unit tested."""
    allowed_set = set(allowed)
    present = set()
    try:
        for _dirpath, fn in _walk_tifs(folder, recurse, skip_dirs):
            tok = _resample_type_token(parse_source_and_product(fn))
            if tok in allowed_set:
                present.add(tok)
    except OSError:
        pass
    return present


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


def build_area_groups(rows):
    """Group AOI features by area so each area yields one output.

    `rows` is an iterable of (fid, raw_area). An area can have several AOI polygons,
    and therefore several download folders, so features sharing the same raw `Area`
    value are merged into one group that keeps every fid. The caller then gathers
    tiles from all of that area's folders into a single mosaic.

    Returns a list of (final_name, [fids]) ordered by the group's smallest fid. Two
    DIFFERENT raw names that sanitize to the same safe name get a numeric suffix via
    dedupe_name (a genuine collision, not a merge). Raises ValueError (via
    sanitize_name) on an empty area value.
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
            raise ValueError("AOI feature(s) with FID {} have an unusable Area value '{}': {}".format(
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


def build_clusters(keys, pairs):
    """Connected components by union-find. `keys` is an iterable of hashable node ids;
    `pairs` is an iterable of (a, b) tuples meaning a and b belong to the same cluster.
    Returns a list of clusters, each a sorted list of keys, the clusters themselves ordered
    by their smallest key. A key that appears in no pair is its own one member cluster.
    """
    parent = {k: k for k in keys}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:                          # path compression
            parent[x], x = root, parent[x]
        return root

    for a, b in pairs:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups = {}
    for k in parent:
        groups.setdefault(find(k), []).append(k)
    comps = [sorted(g) for g in groups.values()]
    comps.sort(key=lambda g: g[0])
    return comps


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

    classes is a list of (class_id, lo, hi), one of the fixed scheme constants (validated by
    validate_value_table, in the tests and once at the start of the reclassify run). The class
    with the largest hi is inclusive at the top ([lo, hi]); all others are [lo, hi).
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
        # (no toolset categories): 01 Download DGT Data, 02 Build Mosaics by Polygon,
        # 03 Generate Surfaces, 04 Solar Radiation, 05 Reclassify Factors, 06 Resample,
        # 07 Contours (optional cartographic output), 08 Verify Outputs (read-only check),
        # 09 Suitability Mask (binary slope x solar mask per mine), 10 Merge Class Rasters and
        # 11 Vectorize Class Rasters (delivery: every area together as one raster or one shapefile).
        self.tools = [DownloadDGTData, BuildMosaicsByPolygon, DeriveSurfaces, SolarRadiation,
                      ReclassifyFactor, Resample, Contours, VerifyOutputs, SuitabilityMask,
                      MergeClasses, VectorizeClasses]


# ===========================================================================
# Tools
# ===========================================================================
#   The ordered tool list and their numbers live in Toolbox.__init__ above (single source).
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


# Datums that are coincident to sub-meter, so a projection between them without a datum shift is
# correct in practice and the mislabeled-CRS warning would be a false alarm. ETRS89 and WGS84 were
# identical in 1989 and diverge only about 2.5 cm/year (sub-meter today), and the AOI is projected
# to WGS84 only to compute a padded, kilometer-scale tile search box.
_COINCIDENT_DATUMS = {"D_ETRS_1989", "D_WGS_1984"}


def _datum_name(sr):
    """The datum name of a SpatialReference (projected or geographic), or None if unavailable."""
    try:
        return sr.datumName
    except Exception:
        return None


def _datums_coincident(datum_a, datum_b):
    """True when both datums are in the coincident set, so projecting between them without a datum
    shift introduces only a sub-meter error. Pure (names in, bool out) so it is unit tested."""
    return datum_a in _COINCIDENT_DATUMS and datum_b in _COINCIDENT_DATUMS


_DATUM_WARNED = set()                                 # (from wkid/name, to wkid/name) pairs warned


def _project_geom(geom, from_sr, to_sr):
    """Project a geometry between CRS, deliberately WITHOUT a geographic (datum) transformation,
    warning once per CRS pair when one would apply. Rationale: Geometry.projectAs with no
    transformation applies no datum shift, only the projection change. That is correct for data
    whose coordinates already sit on the target datum under a mislabeled CRS (common with
    Portuguese layers tagged Lisboa Hayford-Gauss IGeoE, ESRI:102164, whose coordinates are
    really ETRS89 TM06 plus the military false origin). Applying the Lisboa transformation to
    such data would shift everything about 300 m. When the source really is on another datum the
    user must fix the layer CRS instead; the warning makes the situation visible either way."""
    if _same_crs(from_sr, to_sr):
        return geom
    key = (from_sr.factoryCode or from_sr.name, to_sr.factoryCode or to_sr.name)
    if key not in _DATUM_WARNED:
        _DATUM_WARNED.add(key)
        try:
            differs = arcpy.ListTransformations(from_sr, to_sr)
        except Exception:
            differs = []
        # ETRS89 to WGS84 (the download bbox projection) lists a transformation but the shift is
        # sub-meter, so skip the mislabeled-CRS warning for coincident datums; still project below.
        coincident = _datums_coincident(_datum_name(from_sr), _datum_name(to_sr))
        if differs and not coincident:
            _warn("{} and {} are on different datums. Projecting WITHOUT a datum shift (the "
                  "transformation {} is NOT applied). Verify the outputs align with the terrain; "
                  "if they are offset by a few hundred meters, the layer CRS is likely correct "
                  "and you should project the layer properly first. If instead the LAYER draws "
                  "offset while the outputs align, the layer CRS is mislabeled; redefine it.".format(
                      _crs_label(from_sr), _crs_label(to_sr), differs[0]))
    return geom.projectAs(to_sr)


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


def _parse_tile_resolution(filename):
    """Resolution token from a DGT tile name, or None.

    DGT tiles are named like 'MDT-2m-<code>-MM-YYYY.tif' or 'MDS-50cm-...tif'; the token
    is the segment after the product code (here '2m' or '50cm'), returned as written (the
    product code match is case insensitive). Returns None when the name does not have the
    'MD[TS]-<token>-...' shape, so non DGT or unlabeled names are left alone.
    """
    if not filename:
        return None
    parts = filename.split("-")
    if len(parts) < 3 or parts[0].upper() not in ("MDT", "MDS"):
        return None
    token = parts[1].strip()
    return token or None


def _choose_resolution(tokens, requested):
    """Decide which resolution token a mosaic should use.

    `tokens` is the set of distinct resolution tokens (case folded) found among an area's
    tiles, `requested` is the user's Tile resolution value or None for auto. Returns the
    chosen token (lower case) or None, where None means no token filter (use every tile,
    for unlabeled data). Fails loud in auto mode when tiles span more than one resolution,
    so a mosaic never mixes cell sizes.
    """
    if requested:
        return requested.strip().lower()
    found = sorted(t for t in tokens if t)
    if len(found) > 1:
        raise ValueError("Tiles span more than one resolution ({}). Set the Tile "
                         "resolution parameter to choose one.".format(", ".join(found)))
    return found[0] if found else None


def _gather_product_tiles(folders, product_prefix, resolution=None):
    """Collect the .tif tiles for one product across an area's download folders.

    product_prefix is "MDT" (DEM) or "MDS" (DSM). Recurses each folder, keeps files whose
    name starts with product_prefix (case insensitive), and dedups by file name (adjacent
    AOIs of the same area share tiles, the tile code identifies them). Returns
    (paths, tile_sr, cell_size, resolution_used); returns ([], None, None, None) if no tile
    matches.

    Resolution: tiles carry a token in the name (MDT-2m-..., MDS-50cm-...). If `resolution`
    is given, only tiles with that token are kept (case insensitive). If it is None, the
    token is auto-detected and tiles of more than one resolution fail loud, so a mosaic
    never mixes cell sizes.

    Validated per folder (tiles within a DGT download are homogeneous): fail loud on a
    geographic, undefined, or mixed CRS, and, on the selected tiles, on a mixed cell size
    across the area's folders.
    """
    seen = set()
    per_folder = []        # list of [(token, path), ...], one entry per folder
    tokens = set()
    ref_sr = None
    for folder in folders:
        folder_tiles = []
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
                        msg = ("Tile '{}' CRS ({}) differs from the others ({}). All tiles of an "
                               "area must share one projected CRS.").format(
                                   path, _crs_label(sr), _crs_label(ref_sr))
                        _err(msg)
                        raise ValueError(msg)
                if fn in seen:
                    continue
                seen.add(fn)
                token = _parse_tile_resolution(fn)
                if token:
                    tokens.add(token.lower())
                folder_tiles.append((token, path))
        per_folder.append(folder_tiles)

    try:
        chosen = _choose_resolution(tokens, resolution)
    except ValueError as exc:
        msg = "{} (product {}).".format(exc, product_prefix)
        _err(msg)
        raise ValueError(msg)

    # Keep the chosen resolution, then validate cell size on the selected tiles only,
    # sampling the first selected tile of each folder (cheap, mirrors the CRS check). Doing
    # this after the filter is what lets two resolutions coexist when one is requested.
    paths = []
    ref_cell = None
    for folder_tiles in per_folder:
        selected = [p for t, p in folder_tiles
                    if chosen is None or (t and t.lower() == chosen)]
        if not selected:
            continue
        cell = float(arcpy.Describe(selected[0]).meanCellWidth)
        if ref_cell is None:
            ref_cell = cell
        elif abs(cell - ref_cell) > 1e-6:
            msg = ("Selected tiles span more than one cell size ({:g} m and {:g} m). All "
                   "tiles of an area must share one cell size.").format(ref_cell, cell)
            _err(msg)
            raise ValueError(msg)
        paths.extend(selected)
    return paths, ref_sr, ref_cell, chosen


def _folder_extent_polygon(folder, sr):
    """Coverage extent of a download folder as a Polygon in `sr`, read from the
    folder's .vrt (one cheap XML read). Returns None if the folder has no .vrt.

    Used only as a spatial sanity check that a folder maps to the right area. `sr` must
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


def _folder_extent_resolved(folder, sr):
    """Coverage extent of a download folder as a Polygon in `sr`, with a fallback chain: the
    folder's .vrt first (one cheap XML read), else the union of the MDT/MDS tile extents (more
    I/O, one header read per tile). Returns None only when there is neither a readable .vrt nor
    any readable tile. `sr` must be the tiles CRS.
    """
    ext = _folder_extent_polygon(folder, sr)
    if ext is not None:
        return ext
    xmin = ymin = xmax = ymax = None
    for dirpath, _dirs, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith(".tif"):
                continue
            if not (fn.startswith("MDT") or fn.startswith("MDS")):
                continue
            try:
                e = arcpy.Describe(os.path.join(dirpath, fn)).extent
            except Exception:
                continue
            if e is None:
                continue
            xmin = e.XMin if xmin is None else min(xmin, e.XMin)
            ymin = e.YMin if ymin is None else min(ymin, e.YMin)
            xmax = e.XMax if xmax is None else max(xmax, e.XMax)
            ymax = e.YMax if ymax is None else max(ymax, e.YMax)
    if xmin is None:
        return None
    corners = arcpy.Array([arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
                           arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin),
                           arcpy.Point(xmin, ymin)])
    return arcpy.Polygon(corners, sr)


# ===========================================================================
# Tool 01 - Download DGT Data (independent client of the public DGT CDD STAC API)
# ===========================================================================

DGT_CDD_BASE = "https://cdd.dgterritorio.gov.pt"
DGT_CDD_SEARCH = DGT_CDD_BASE + "/dgt-be/v1/search"
DGT_CDD_AUTH = "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/protocol/openid-connect"
DGT_CDD_CLIENT_ID = "aai-oidc-dgt"
DGT_CDD_REDIRECT = DGT_CDD_BASE + "/auth/callback"
DGT_STAC_LIMIT = 1000
DGT_MAX_CHUNK_KM2 = 200.0                 # split a WGS84 bbox larger than this before searching
DGT_DOWNLOAD_RETRIES = 4
DGT_SESSION_TIMEOUT = 25 * 60             # renew the CDD session before its token (about 30 min) expires
DGT_CRED_DIRNAME = "LidarTerrainToolbox"
DGT_CRED_FILENAME = "dgt_cdd_credentials.json"
WGS84_EPSG = 4326
DGT_ASSET_EXT = {                          # STAC asset MIME type (lowercased) to file extension
    "image/tiff; application=geotiff": ".tif",
    "image/tiff;application=geotiff": ".tif",
    "image/tiff": ".tif",
    "application/vnd.laszip": ".laz",
    "application/vnd.las": ".las",
}
DGT_AUTH_HOST = "https://auth.cdd.dgterritorio.gov.pt"
DGT_HEADERS = {                            # our client identity and the types we accept
    "User-Agent": "LidarTerrainToolbox/1.0 (ArcGIS Pro; +https://github.com/PedroMMGoncalves/lidar-terrain-arcgis-tools)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.8,en;q=0.6",
}
# Elevation products offered as a checklist in the tool. The orthophoto and Sentinel collections
# the portal also has are not relevant to this terrain toolbox; "List collections only" shows all.
DGT_ELEVATION_COLLECTIONS = ["LAZ", "MDT-2m", "MDS-2m", "MDT-50cm", "MDS-50cm"]
# Order in which products are downloaded, so each folder fills one product at a time, not mixed.
DGT_DOWNLOAD_ORDER = ["MDT-50cm", "MDS-50cm", "MDT-2m", "MDS-2m", "LAZ"]


def _download_order_key(collection):
    """Sort key so products download in the order above; any other collection goes last."""
    try:
        return (DGT_DOWNLOAD_ORDER.index(collection), collection)
    except ValueError:
        return (len(DGT_DOWNLOAD_ORDER), collection)


# The two output layouts the tool offers.
LAYOUT_PER_AREA = "One folder per area, products in subfolders"
LAYOUT_FLAT = "Single flat folder, all tiles together"

# Leading signatures of the files we download: GeoTIFF (little/big endian, classic and BigTIFF)
# and LAS/LAZ point clouds. An expired CDD session returns the HTML login page instead, which we
# must never save as a tile, so downloads and existing files are checked against these.
DGT_ASSET_SIGNATURES = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+", b"LASF")


def _looks_like_asset(head):
    """True when head starts with a GeoTIFF or LAS/LAZ signature (not an HTML error page)."""
    return any(head[:4] == sig[:4] for sig in DGT_ASSET_SIGNATURES)


def _file_looks_valid(path):
    """True when the file on disk starts with a valid asset signature."""
    try:
        with open(path, "rb") as fh:
            return _looks_like_asset(fh.read(4))
    except OSError:
        return False


def _requests_ca_bundle():
    """CA bundle for HTTPS. Some machines set REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / SSL_CERT_FILE
    to a file that does not exist (a PostgreSQL install is a common cause), which makes requests
    fail every HTTPS call with 'Could not find a suitable TLS CA certificate bundle'. When such a
    pointer is broken, drop it for this process and fall back to certifi's bundle. Returns the
    certifi path to use, or None to leave the requests default."""
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        val = os.environ.get(var)
        if val and not os.path.isfile(val):
            _warn("Ignoring {} (points to a missing CA bundle: {}).".format(var, val))
            os.environ.pop(var, None)
    try:
        import certifi
        return certifi.where()
    except Exception:
        return None


def _http_status(exc):
    """HTTP status code carried by a requests exception, or None."""
    return getattr(getattr(exc, "response", None), "status_code", None)


def _asset_extension(mime):
    """File extension for a STAC asset MIME type (.bin if unknown)."""
    if not mime:
        return ".bin"
    return DGT_ASSET_EXT.get(mime.strip().lower(), ".bin")


def divide_bbox(bbox, max_km2=DGT_MAX_CHUNK_KM2):
    """Split a WGS84 bbox (min_lon, min_lat, max_lon, max_lat) into a grid of sub bboxes, each
    at most about max_km2, so a single search stays under the service area or item limit.
    Returns the original bbox in a list when it is already small enough. Area is approximate
    (degrees to km at the mean latitude), enough to bound the request size."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = (min_lat + max_lat) / 2.0
    km_per_deg = 111.32
    width_km = abs(max_lon - min_lon) * km_per_deg * math.cos(math.radians(mean_lat))
    height_km = abs(max_lat - min_lat) * km_per_deg
    if width_km <= 0 or height_km <= 0 or width_km * height_km <= max_km2:
        return [(min_lon, min_lat, max_lon, max_lat)]
    side = math.sqrt(max_km2)
    ncols = max(1, int(math.ceil(width_km / side)))
    nrows = max(1, int(math.ceil(height_km / side)))
    dlon = (max_lon - min_lon) / ncols
    dlat = (max_lat - min_lat) / nrows
    cells = []
    for row in range(nrows):
        for col in range(ncols):
            cells.append((min_lon + col * dlon, min_lat + row * dlat,
                          min_lon + (col + 1) * dlon, min_lat + (row + 1) * dlat))
    return cells


def dgt_credentials_path():
    """Path to the DGT CDD credentials config file in the user profile."""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, DGT_CRED_DIRNAME, DGT_CRED_FILENAME)


def read_dgt_credentials(path):
    """Return (username, password) from the JSON config, or (None, None) if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("username"), data.get("password")
    except (OSError, ValueError):
        return None, None


def write_dgt_credentials(path, username, password):
    """Write the credentials JSON (creating the folder). Returns the path."""
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"username": username, "password": password}, fh)
    return path


class _LoginFormParser(HTMLParser):
    """Pull the login form action and input fields from a Keycloak login page. After feeding
    the HTML, `action` is the form target URL and `fields` is the input name to value map (the
    hidden fields plus the empty username/password inputs)."""
    def __init__(self):
        HTMLParser.__init__(self)
        self.action = None
        self.fields = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "kc-form-login":
            self._in_form = True
            self.action = a.get("action")
        elif tag == "input" and self._in_form:
            name = a.get("name")
            if name and a.get("type") == "hidden":
                self.fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._in_form:
            self._in_form = False


def parse_login_form(html):
    """Return (action_url, hidden_fields) for the CDD login form in `html`."""
    parser = _LoginFormParser()
    parser.feed(html or "")
    return parser.action, parser.fields


class DownloadDGTData(object):
    def __init__(self):
        self.label = "01 - Download DGT Data"
        self.description = (
            "Download DGT LiDAR data from the CDD portal, one folder per AOI feature (cartogram "
            "sheet or buffered point/area), ready for the mosaic tool. For each feature it takes "
            "the envelope (a clean square for points), searches the CDD STAC API by that bounding "
            "box, and downloads the tiles. Credentials are configured once into a user profile "
            "file. Needs network access and a CDD account; this is an independent client of the "
            "public CDD API.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_aoi = arcpy.Parameter(
            displayName="AOI layer (one folder per feature)", name="in_aoi",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_aoi.filter.list = ["Polygon", "Point"]

        p_field = arcpy.Parameter(
            displayName="Folder name field", name="name_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_field.parameterDependencies = [p_aoi.name]
        p_field.filter.list = ["Text", "Short", "Long"]

        p_out = arcpy.Parameter(
            displayName="Output root folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_point = arcpy.Parameter(
            displayName="Point footprint size (meters, half side)", name="point_size",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_point.value = 5000

        p_collections = arcpy.Parameter(
            displayName="Collections to download", name="collections",
            datatype="GPString", parameterType="Optional", direction="Input", multiValue=True)
        p_collections.filter.type = "ValueList"
        p_collections.filter.list = list(DGT_ELEVATION_COLLECTIONS)
        p_collections.value = ["MDT-2m", "MDS-2m"]

        p_list = arcpy.Parameter(
            displayName="List collections only (no download)", name="list_only",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_list.value = False

        p_user = arcpy.Parameter(
            displayName="CDD username (blank uses the saved config)", name="username",
            datatype="GPString", parameterType="Optional", direction="Input")

        p_pass = arcpy.Parameter(
            displayName="CDD password (blank uses the saved config)", name="password",
            datatype="GPStringHidden", parameterType="Optional", direction="Input")

        p_save = arcpy.Parameter(
            displayName="Save credentials to the config file", name="save_creds",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_save.value = False

        p_delay = arcpy.Parameter(
            displayName="Delay between requests (seconds)", name="delay",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_delay.value = 1.0

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing tiles", name="overwrite",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        p_vrt = arcpy.Parameter(
            displayName="Build a VRT per folder", name="build_vrt",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_vrt.value = False

        p_dry = arcpy.Parameter(
            displayName="Dry run (report tiles, no download)", name="dry_run",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_dry.value = False

        p_layout = arcpy.Parameter(
            displayName="Output layout", name="output_layout",
            datatype="GPString", parameterType="Required", direction="Input")
        p_layout.filter.type = "ValueList"
        p_layout.filter.list = [LAYOUT_PER_AREA, LAYOUT_FLAT]
        p_layout.value = LAYOUT_PER_AREA

        return [p_aoi, p_field, p_out, p_point, p_collections, p_list,
                p_user, p_pass, p_save, p_delay, p_overwrite, p_vrt, p_dry, p_layout]

    def isLicensed(self):
        return True

    # --- network (not pure; the user validates live) ---

    def _authenticate(self, session, username, password):
        """Log in to the CDD via the Keycloak OpenID flow, leaving the session authenticated.
        Returns True on success. Follows the public CDD login flow; isolated so it is easy to
        fix if the login page changes."""
        session.headers.update(DGT_HEADERS)
        session.get(DGT_CDD_BASE, timeout=30).raise_for_status()
        params = urllib.parse.urlencode({
            "client_id": DGT_CDD_CLIENT_ID, "response_type": "code",
            "redirect_uri": DGT_CDD_REDIRECT, "scope": "openid profile email"})
        page = session.get(DGT_CDD_AUTH + "/auth?" + params, timeout=30)
        page.raise_for_status()
        action, fields = parse_login_form(page.text)
        if not action:
            # No login form. If the authorize request redirected back to the app, the Keycloak SSO
            # session is still valid and we are already authenticated (common on a proactive renew).
            if page.url.startswith(DGT_CDD_BASE) and self._stac_ok(session):
                import time
                self._auth_time = time.time()
                return True
            _warn("Could not find the CDD login form; the login page may have changed.")
            return False
        if action.startswith("/"):
            action = DGT_AUTH_HOST + action
        fields = dict(fields)
        fields["username"] = username
        fields["password"] = password
        login_headers = {"Content-Type": "application/x-www-form-urlencoded",
                         "Origin": DGT_AUTH_HOST, "Referer": page.url}
        try:
            resp = session.post(action, data=fields, headers=login_headers,
                                allow_redirects=True, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            _warn("CDD login request failed: {}".format(exc))
            return False
        if not resp.url.startswith(DGT_CDD_BASE):
            _warn("CDD login did not redirect back to the site; check the credentials.")
            return False
        if not self._stac_ok(session):
            return False
        import time
        self._auth_time = time.time()      # for the proactive renewal in _ensure_session
        return True

    def _stac_ok(self, session):
        """True when the session can query the STAC search (HTTP 200), i.e. it is authenticated."""
        try:
            return session.post(DGT_CDD_SEARCH,
                                json={"bbox": [-9.0, 38.0, -8.0, 39.0], "limit": 1},
                                timeout=30).status_code == 200
        except Exception:
            return False

    def _search(self, session, bbox, collections):
        """STAC search for one bbox; returns the features list."""
        payload = {"bbox": list(bbox), "limit": DGT_STAC_LIMIT}
        if collections:
            payload["collections"] = collections
        r = session.post(DGT_CDD_SEARCH, json=payload, timeout=60)
        r.raise_for_status()
        return r.json().get("features", [])

    def _collections_via_endpoint(self, session):
        """Collections from the standard STAC /collections endpoint (empty list on failure)."""
        try:
            r = session.get(DGT_CDD_BASE + "/dgt-be/v1/collections", timeout=30)
            r.raise_for_status()
            data = r.json()
            cols = data.get("collections") if isinstance(data, dict) else data
            return sorted(c.get("id") for c in (cols or []) if isinstance(c, dict) and c.get("id"))
        except Exception:
            return []

    def _first_feature_bbox(self, in_aoi, point_size):
        """A small WGS84 bbox at the first AOI feature, to probe which collections cover the AOI."""
        wgs84 = arcpy.SpatialReference(WGS84_EPSG)
        aoi_sr = arcpy.Describe(in_aoi).spatialReference
        with arcpy.da.SearchCursor(in_aoi, ["SHAPE@"]) as cursor:
            for (shape,) in cursor:
                if shape is not None:
                    return self._feature_bbox(shape, "point", point_size or 500.0, aoi_sr, wgs84)
        return None

    def _item_id(self, feat):
        """Item id from the STAC self link (the file base name), else the feature id."""
        for link in feat.get("links", []) or []:
            if link.get("rel") == "self" and link.get("href"):
                return link["href"].rstrip("/").split("/")[-1]
        return feat.get("id") or "unknown"

    def _feature_bbox(self, shape, shp_type, point_size, aoi_sr, wgs84):
        """WGS84 (min_lon, min_lat, max_lon, max_lat) envelope for a feature; a square of half
        side point_size around a point feature."""
        if str(shp_type).lower() == "point":
            c = shape.centroid
            xmin, ymin = c.X - point_size, c.Y - point_size
            xmax, ymax = c.X + point_size, c.Y + point_size
        else:
            e = shape.extent
            xmin, ymin, xmax, ymax = e.XMin, e.YMin, e.XMax, e.YMax
        corners = arcpy.Array([arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
                               arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin),
                               arcpy.Point(xmin, ymin)])
        poly = arcpy.Polygon(corners, aoi_sr)
        ll = _project_geom(poly, aoi_sr, wgs84)
        we = ll.extent
        return (we.XMin, we.YMin, we.XMax, we.YMax)

    def _collect_assets(self, session, bbox, collections, delay):
        """Search a bbox (split if large) and return {(collection, item_id, extension): [urls]}.
        Keyed by the output file, with the list of candidate asset URLs for it: a feature can expose
        the same tile through more than one href, and one of a tile's per search download tokens can
        be forbidden (403) while another works, so the downloader tries them in turn. Keying by the
        file (not the URL) also keeps each tile listed and counted once."""
        import time
        assets = {}
        for chunk in divide_bbox(bbox):
            for feat in self._search(session, chunk, collections or None):
                col = feat.get("collection") or "unknown"
                item_id = self._item_id(feat)
                for asset in (feat.get("assets") or {}).values():
                    url = asset.get("href")
                    if not url:
                        continue
                    urls = assets.setdefault(
                        (col, item_id, _asset_extension(asset.get("type"))), [])
                    if url not in urls:
                        urls.append(url)
            if delay:
                time.sleep(delay)
        return assets

    def _download_assets(self, session, assets, dest_root, overwrite, delay, flat=False):
        """Download an assets dict into dest_root. With flat=False (the per area layout) each
        product goes in its own subfolder, dest_root/<collection>/<tile>; with flat=True every
        tile lands directly in dest_root. Tiles download grouped by product in a stable order
        (DGT_DOWNLOAD_ORDER), not mixed, and each tile is logged. Returns (ok, skip, fail, failed
        list). The
        subfolders mirror the DGT products (MDT-2m, MDS-2m, MDT-50cm, MDS-50cm, LAZ) and the
        mosaic tool reads MDT*/MDS*."""
        import time
        if not os.path.isdir(dest_root):
            os.makedirs(dest_root)
        by_col = {}
        for (col, item_id, ext), urls in assets.items():
            by_col.setdefault(col, []).append((item_id, ext, urls))
        ok = skip = fail = 0
        failed = []
        for col in sorted(by_col, key=_download_order_key):
            tiles = sorted(by_col[col], key=lambda t: t[0])
            total = len(tiles)
            if flat:
                sub_dir = dest_root
            else:
                sub_dir = os.path.join(dest_root, col)
                if not os.path.isdir(sub_dir):
                    os.makedirs(sub_dir)
            _msg("  {0}: {1} tiles".format(col, total))
            col_ok = col_skip = col_fail = 0
            for n, (item_id, ext, urls) in enumerate(tiles, 1):
                dest = os.path.join(sub_dir, item_id + ext)
                result = self._download(session, urls, dest, overwrite)
                # Log only the interesting tiles per line (downloaded or failed); a re-run over
                # existing data would otherwise print thousands of "skipped" lines. The skipped
                # count is reported in the per product summary below.
                if result == "ok":
                    ok += 1
                    col_ok += 1
                    _msg("    [{0}/{1}] {2} - downloaded".format(n, total, item_id + ext))
                elif result == "skip":
                    skip += 1
                    col_skip += 1
                else:
                    fail += 1
                    col_fail += 1
                    failed.append(item_id + ext)
                    _msg("    [{0}/{1}] {2} - FAILED".format(n, total, item_id + ext))
                if delay and result != "skip":     # a skip does not hit the server, so no delay
                    time.sleep(delay)
            _msg("  {0}: {1} downloaded, {2} skipped, {3} failed.".format(
                col, col_ok, col_skip, col_fail))
        return ok, skip, fail, failed

    def _download(self, session, urls, dest, overwrite):
        """Download a tile to dest (streamed), trying each candidate URL until one succeeds. A tile
        is often exposed by more than one asset href and one of a tile's per search tokens can be
        forbidden (403) while another works, so a 403 falls through to the next URL. Returns ok /
        skip / fail. Guards against an expired session: the CDD then serves the Keycloak login page
        with HTTP 200, so a response that is HTML, or whose body is not a GeoTIFF or LAZ, is never
        written, and the session is re-authenticated once. An existing valid file is skipped; an
        invalid one (an error page saved by an earlier run) is re-downloaded even without overwrite."""
        if os.path.exists(dest) and not overwrite:
            if _file_looks_valid(dest):
                return "skip"
            _warn("Existing file is not a valid tile (an earlier error page); re-downloading {}."
                  .format(os.path.basename(dest)))
        self._ensure_session(session)
        import time
        tmp = dest + ".part"
        reauthed = False
        throttled = False
        last_reason = None
        for cycle in range(DGT_DOWNLOAD_RETRIES):
            for url in urls:
                try:
                    with session.get(url, stream=True, timeout=120) as r:
                        if r.status_code in (401, 403):
                            # Lost authorization or a forbidden token; re-auth once per tile
                            # (attempted at most once, even if it fails, so a dead auth service
                            # is not hammered for every URL and cycle), then let the next
                            # candidate URL (or the next cycle) try.
                            throttled = throttled or r.status_code == 403
                            last_reason = "HTTP {}".format(r.status_code)
                            if not reauthed:
                                reauthed = True
                                self._reauthenticate(session)
                            continue
                        r.raise_for_status()
                        ctype = (r.headers.get("Content-Type") or "").lower()
                        stream = r.iter_content(chunk_size=1 << 20)
                        head = next(stream, b"")
                        if "text/html" in ctype or not _looks_like_asset(head):
                            last_reason = "not a tile (login page or unexpected content)"
                            if not reauthed:
                                reauthed = True
                                self._reauthenticate(session)
                            continue
                        with open(tmp, "wb") as fh:
                            fh.write(head)
                            for chunk in stream:
                                if chunk:
                                    fh.write(chunk)
                        os.replace(tmp, dest)
                    return "ok"
                except Exception as exc:
                    throttled = throttled or _http_status(exc) in (403, 429)
                    last_reason = str(exc)
            # Every candidate URL failed this cycle; back off before retrying the set. A 403/429 is
            # usually throttling that clears with time, so wait longer.
            if cycle < DGT_DOWNLOAD_RETRIES - 1:
                time.sleep((5 if throttled else 1) * (2 ** cycle))
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if last_reason is not None:
            _warn("{} failed after {} cycles over {} url(s); last: {}".format(
                os.path.basename(dest), DGT_DOWNLOAD_RETRIES, len(urls), last_reason))
        return "fail"

    def _reauthenticate(self, session):
        """Re-login the session after it expires mid-download. Returns True on success."""
        user, pwd = getattr(self, "_creds", (None, None))
        if not (user and pwd):
            return False
        _msg("Renewing the CDD session...")
        try:
            if self._authenticate(session, user, pwd):
                _msg("Re-authenticated.")
                return True
        except Exception:
            pass
        _warn("Re-authentication failed; the affected tiles will be retried on a re-run.")
        return False

    def _ensure_session(self, session):
        """Proactively keep the CDD session alive during long downloads (its token lasts about 30
        minutes; we check at 25). Following the DGT downloader plugin, this first tests whether the
        session is still valid and only re-authenticates when it is not, so a still-valid Keycloak
        SSO session is not mistaken for an expiry."""
        import time
        if time.time() - getattr(self, "_auth_time", 0) < DGT_SESSION_TIMEOUT:
            return
        if self._stac_ok(session):
            self._auth_time = time.time()   # still valid, no login needed
            return
        if not self._reauthenticate(session):
            self._auth_time = time.time()   # back off so the renewal is not retried every tile

    def _write_manifest(self, folder, area, bbox, collections, n):
        with open(os.path.join(folder, area + "_download.txt"), "w", encoding="utf-8") as fh:
            fh.write("Area: {}\n".format(area))
            fh.write("Source: DGT CDD (cdd.dgterritorio.gov.pt)\n")
            if bbox:
                fh.write("WGS84 bbox: {}\n".format(", ".join("{:.6f}".format(v) for v in bbox)))
            fh.write("Collections: {}\n".format(", ".join(collections) if collections else "(all)"))
            fh.write("Tiles: {}\n".format(n))

    def _write_failed(self, folder, area, failed):
        """List the tiles that could not be downloaded. A tile that stays forbidden (403) across a
        re-auth and every candidate URL is usually not served by the CDD yet; re-running later
        retries only these, since the downloaded tiles are skipped."""
        path = os.path.join(folder, area + "_failed.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Tiles that failed to download (re-run later to retry):\n")
            for name in failed:
                fh.write(name + "\n")
        _warn("{}: {} tile(s) failed; listed in {}".format(area, len(failed), os.path.basename(path)))

    def _build_vrt(self, folder):
        """Build a VRT per product subfolder that holds GeoTIFFs, if gdal (osgeo) is available."""
        try:
            from osgeo import gdal
        except ImportError:
            _warn("VRT requested but gdal (osgeo) is not available; skipping the VRT.")
            return
        for entry in sorted(os.listdir(folder)):
            sub = os.path.join(folder, entry)
            if not os.path.isdir(sub):
                continue
            tifs = [os.path.join(sub, f) for f in sorted(os.listdir(sub))
                    if f.lower().endswith(".tif")]
            if tifs:
                vrt_path = os.path.join(folder, entry + ".vrt")
                # gdal returns None on failure instead of raising; surface it.
                if gdal.BuildVRT(vrt_path, tifs) is None:
                    _warn("Could not build the VRT {}.".format(os.path.basename(vrt_path)))

    def execute(self, parameters, messages):
        import time
        if requests is None:
            msg = ("The 'requests' library is not available in this Python. Install it in the "
                   "ArcGIS Pro environment to use the download tool.")
            _err(msg)
            raise ImportError(msg)

        in_aoi = parameters[0].valueAsText
        name_field = parameters[1].valueAsText
        out_folder = parameters[2].valueAsText
        point_size = float(parameters[3].value or 0)
        collections = list(parameters[4].values) if parameters[4].values else []
        list_only = bool(parameters[5].value)
        username = (parameters[6].valueAsText or "").strip()
        password = parameters[7].valueAsText or ""
        save_creds = bool(parameters[8].value)
        delay = float(parameters[9].value or 0)
        overwrite = bool(parameters[10].value)
        build_vrt = bool(parameters[11].value)
        dry_run = bool(parameters[12].value)
        layout = parameters[13].valueAsText or LAYOUT_PER_AREA
        per_feature = (layout == LAYOUT_PER_AREA)

        cred_path = dgt_credentials_path()
        if username and password:
            if save_creds:
                write_dgt_credentials(cred_path, username, password)
                _msg("Saved credentials to '{}'.".format(cred_path))
        else:
            username, password = read_dgt_credentials(cred_path)
            if not (username and password):
                msg = ("No CDD credentials. Configure them on first use: enter the username and "
                       "password and turn on 'Save credentials to the config file'.")
                _err(msg)
                raise ValueError(msg)

        session = requests.Session()
        ca_bundle = _requests_ca_bundle()
        if ca_bundle:
            session.verify = ca_bundle
        _msg("Authenticating with the CDD...")
        if not self._authenticate(session, username, password):
            session.close()
            msg = "CDD authentication failed; check the credentials and the service."
            _err(msg)
            raise RuntimeError(msg)
        _msg("Authenticated.")
        self._creds = (username, password)   # kept so a mid-download expiry can re-authenticate

        if list_only:
            cols = self._collections_via_endpoint(session)
            if not cols:
                probe = self._first_feature_bbox(in_aoi, point_size)
                if probe:
                    assets = self._collect_assets(session, probe, None, 0)
                    cols = sorted({col for (col, _id, _ext) in assets})
            if cols:
                _msg("Collections ({}):".format(len(cols)))
                for cid in cols:
                    _msg("  " + cid)
            else:
                _warn("No collections found. Check the login and that the AOI has coverage.")
            session.close()
            return

        if not collections:
            collections = ["MDT-2m", "MDS-2m"]
            _msg("No collection selected; defaulting to MDT-2m and MDS-2m.")

        wgs84 = arcpy.SpatialReference(WGS84_EPSG)
        desc = arcpy.Describe(in_aoi)
        aoi_sr = desc.spatialReference
        source = in_aoi          # the layer cursor honors the active selection (all when none)
        total_ok = total_skip = total_fail = 0
        features = []                             # (area, wgs84 bbox)
        raw_names = []
        with arcpy.da.SearchCursor(source, ["SHAPE@", name_field]) as cursor:
            for shape, raw_name in cursor:
                if shape is None or raw_name in (None, ""):
                    _warn("Feature with no geometry or empty name; skipped.")
                    continue
                raw_names.append(raw_name)
                features.append((sanitize_name(str(raw_name)),
                                 self._feature_bbox(shape, shape.type, point_size, aoi_sr, wgs84)))
        if not features:
            session.close()
            msg = "No features to download."
            _err(msg)
            raise ValueError(msg)

        # Fail loud when two different feature names sanitize to the same folder name: their tiles
        # would merge into one folder and the second manifest would overwrite the first, silently.
        if per_feature:
            collisions = find_sanitize_collisions(raw_names)
            if collisions:
                session.close()
                detail = "; ".join("'{}' <- {}".format(n, ", ".join(r))
                                   for n, r in sorted(collisions.items()))
                msg = ("Different feature names sanitize to the same folder name, so their tiles "
                       "would merge into one folder: {}. Rename the features.".format(detail))
                _err(msg)
                raise ValueError(msg)

        if per_feature:
            # One folder per feature (the recurrence).
            for area, bbox in features:
                assets = self._collect_assets(session, bbox, collections, delay)
                if dry_run:
                    _msg("{}: {} tiles (dry run, not downloaded).".format(area, len(assets)))
                    continue
                if not assets:
                    _warn("{}: no tiles found for the footprint.".format(area))
                    continue
                folder = os.path.join(out_folder, area)
                _msg("{}: processing {} tiles...".format(area, len(assets)))
                ok, skip, fail, failed = self._download_assets(
                    session, assets, folder, overwrite, delay)
                self._write_manifest(folder, area, bbox, collections, len(assets))
                if failed:
                    self._write_failed(folder, area, failed)
                if build_vrt:
                    self._build_vrt(folder)
                total_ok += ok
                total_skip += skip
                total_fail += fail
                _msg("{}: downloaded {}, skipped {}, failed {} -> {}".format(
                    area, ok, skip, fail, folder))
        else:
            # All features into one flat folder, deduplicated by URL.
            merged = {}
            for area, bbox in features:
                for key, urls in self._collect_assets(session, bbox, collections, delay).items():
                    dst = merged.setdefault(key, [])
                    for u in urls:
                        if u not in dst:
                            dst.append(u)
            if dry_run:
                _msg("Flat: {} tiles from {} feature(s) (dry run, not downloaded).".format(
                    len(merged), len(features)))
            elif not merged:
                _warn("No tiles found for any feature.")
            else:
                _msg("Flat: processing {} tiles...".format(len(merged)))
                total_ok, total_skip, total_fail, failed = self._download_assets(
                    session, merged, out_folder, overwrite, delay, flat=True)
                self._write_manifest(out_folder, "ALL", None, collections, len(merged))
                if failed:
                    self._write_failed(out_folder, "ALL", failed)
                if build_vrt:
                    _warn("VRT is not built for the flat layout (products share one folder).")
                _msg("Block: downloaded {}, skipped {}, failed {} -> {}".format(
                    total_ok, total_skip, total_fail, out_folder))

        _msg("Download done. Tiles downloaded {}, skipped {}, failed {}.".format(
            total_ok, total_skip, total_fail))
        if total_fail:
            _warn("{} tile(s) failed. Re-run to retry; existing tiles are skipped.".format(total_fail))
        session.close()
        return


class BuildMosaicsByPolygon(object):
    def __init__(self):
        self.label = "02 - Build Mosaics by Polygon"
        self.description = ("Build one DEM and one DSM mosaic per area from the DGT LiDAR "
                            "download folders. Folders are matched to areas by name (the Tool 1 "
                            "layout, the default), by geometry, or by the FID number (the old "
                            "plugin layout), and merged per area. The spatial selection was "
                            "already done at download time, so no buffering or tile intersection "
                            "is needed. Optional AOI clip, per resolution trees, overlap clusters "
                            "and pyramids.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_aoi = arcpy.Parameter(
            displayName="AOI layer (areas of interest)", name="in_aoi",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_aoi.filter.list = ["Polygon"]

        p_field = arcpy.Parameter(
            displayName="Area name field", name="area_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_field.parameterDependencies = [p_aoi.name]
        p_field.filter.list = ["Text", "Short", "Long"]

        p_root = arcpy.Parameter(
            displayName="LiDAR root folder", name="lidar_root",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["per_area_subfolder", "flat"]
        p_struct.value = "per_area_subfolder"

        p_products = arcpy.Parameter(
            displayName="Products", name="products",
            datatype="GPString", parameterType="Required", direction="Input")
        p_products.filter.type = "ValueList"
        p_products.filter.list = ["DEM", "DSM", "BOTH"]   # same order in every tool
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
            displayName="Skip areas with missing folders", name="skip_incomplete",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_skip.value = True

        p_verify = arcpy.Parameter(
            displayName="Verify folder extent against AOI polygon", name="verify_extent",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_verify.value = True

        p_prefix = arcpy.Parameter(
            displayName="Folder name prefix (leave blank to find the data automatically)", name="folder_prefix",
            datatype="GPString", parameterType="Optional", direction="Input")

        p_res = arcpy.Parameter(
            displayName="Tile resolution (blank auto-detects; both = one subfolder each)",
            name="tile_resolution", datatype="GPString", parameterType="Optional",
            direction="Input", multiValue=True)
        p_res.filter.type = "ValueList"
        p_res.filter.list = ["2m", "50cm"]

        p_clusters = arcpy.Parameter(
            displayName="Also build overlap clusters", name="build_clusters",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_clusters.value = False

        p_dry_run = arcpy.Parameter(
            displayName="Report clusters only (no build)", name="cluster_dry_run",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_dry_run.value = False

        p_mapping = arcpy.Parameter(
            displayName="Folder to area mapping", name="folder_mapping",
            datatype="GPString", parameterType="Required", direction="Input")
        p_mapping.filter.type = "ValueList"
        p_mapping.filter.list = ["by name", "by geometry", "by FID number"]
        p_mapping.value = "by name"

        p_clip = arcpy.Parameter(
            displayName="Clip mosaic to AOI", name="clip_to_aoi",
            datatype="GPString", parameterType="Required", direction="Input")
        p_clip.filter.type = "ValueList"
        # none = full tile coverage; extent = the AOI bounding box (exact for a rectangular
        # cartogram sheet, fast, no clip pass); polygon = the true vector mask (irregular AOIs
        # such as mine polygons), a small extra clip pass per area.
        p_clip.filter.list = ["none", "extent", "polygon"]
        p_clip.value = "none"

        p_pyramids = arcpy.Parameter(
            displayName="Build pyramids and statistics", name="build_pyramids",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_pyramids.value = False

        # Group the less common parameters into collapsible categories, so the everyday run shows
        # only the essentials (Pro shows uncategorized parameters first, categories collapsed).
        for p in (p_clusters, p_dry_run):
            p.category = "Clustering"
        for p in (p_pixel, p_method, p_res, p_verify, p_prefix, p_pyramids):
            p.category = "Advanced"

        return [p_aoi, p_field, p_root, p_out, p_struct, p_products,
                p_pixel, p_method, p_overwrite, p_skip, p_verify, p_prefix, p_res,
                p_clusters, p_dry_run, p_mapping, p_clip, p_pyramids]

    def isLicensed(self):
        # Uses core Mosaic To New Raster, no Spatial Analyst needed.
        return True

    def updateParameters(self, parameters):
        # The cluster dry-run only applies when clustering is on.
        parameters[14].enabled = bool(parameters[13].value)
        # The extent verification only runs under by FID mapping (by name and by geometry are
        # authoritative), so grey it out otherwise instead of showing an inert checkbox.
        parameters[10].enabled = (parameters[15].valueAsText == "by FID number")
        return

    def updateMessages(self, parameters):
        return

    def _build_clusters(self, groups, fid_to_geom, fid_to_folder, products,
                        out_folder, pixel_type, mosaic_method, tile_resolution,
                        skip_incomplete, overwrite_existing, dry_run, build_pyramids=False):
        """Aggregate areas whose AOI polygons are contiguous (touch or overlap) into one
        mosaic per cluster, in parallel to the per area output.

        Every area is placed in exactly one cluster (a non overlapping area is its own one
        member cluster), since the per area and cluster outputs serve different uses. Clusters
        go to a 'clusters' subfolder of the output folder, named Cluster_NNN by the smallest
        member FID, each with a Cluster_NNN_members.txt manifest (the authority on the
        cluster's content; the ids depend on the AOI and renumber if it is edited).
        """
        # Reserve the Cluster_ prefix so a real area cannot collide with a generated id.
        for area, _fids in groups:
            if area.startswith("Cluster_") and area[len("Cluster_"):].isdigit():
                msg = ("Area '{}' collides with the reserved cluster name prefix 'Cluster_'. "
                       "Rename it before building clusters.").format(area)
                _err(msg)
                raise ValueError(msg)

        # One node per area group, keyed by its smallest FID; union its polygons once.
        nodes = []                        # (min_fid, area, fids, union_geom, extent)
        for area, fids in groups:
            geoms = [fid_to_geom[f] for f in fids if fid_to_geom.get(f) is not None]
            if not geoms:
                continue
            union = geoms[0]
            for g in geoms[1:]:
                union = union.union(g)
            nodes.append((min(fids), area, fids, union, union.extent))
        if not nodes:
            _warn("Clusters requested but no AOI geometry is available; skipping clustering.")
            return

        # Contiguity edges: polygons that are not disjoint (touch or overlap). The bounding
        # box check is a cheap pre filter before the full, slower geometry test.
        by_key = {n[0]: n for n in nodes}
        pairs = []
        for i in range(len(nodes)):
            ki, _ai, _fi, gi, ei = nodes[i]
            for j in range(i + 1, len(nodes)):
                kj, _aj, _fj, gj, ej = nodes[j]
                if (ei.XMax < ej.XMin or ej.XMax < ei.XMin
                        or ei.YMax < ej.YMin or ej.YMax < ei.YMin):
                    continue
                if not gi.disjoint(gj):
                    pairs.append((ki, kj))

        clusters = build_clusters(list(by_key.keys()), pairs)
        _msg("Clustering: {} area(s) form {} cluster(s) by contiguity.".format(
            len(nodes), len(clusters)))

        cluster_root = os.path.join(out_folder, "clusters")
        built = 0
        skipped = 0
        arcpy.SetProgressor("step", "Building clusters...", 0, len(clusters), 1)
        for idx, member_keys in enumerate(clusters, start=1):
            arcpy.SetProgressorPosition()
            cluster_id = "Cluster_{:03d}".format(idx)
            members = [by_key[k] for k in member_keys]
            member_areas = sorted(m[1] for m in members)
            all_fids = sorted(f for m in members for f in m[2])
            present = [f for f in all_fids if f in fid_to_folder]
            missing = [f for f in all_fids if f not in fid_to_folder]

            if dry_run:
                _msg("{}: {} area(s) [{}]; FIDs present {}/{}.".format(
                    cluster_id, len(member_areas), ", ".join(member_areas),
                    len(present), len(all_fids)))
                continue

            if not present:
                _warn("{}: no download folders present for any member; skipping.".format(cluster_id))
                skipped += 1
                continue
            if missing and skip_incomplete:
                _warn("{}: members not fully downloaded (FIDs {}); skipping (incomplete). "
                      "Re-run when the download finishes.".format(cluster_id, missing))
                skipped += 1
                continue

            present_folders = [fid_to_folder[f] for f in present]
            location = os.path.join(cluster_root, cluster_id)
            if not os.path.isdir(location):
                os.makedirs(location)

            made = 0
            for source, product_prefix in products:
                out_name = build_output_name(cluster_id, source) + ".tif"
                out_path = os.path.join(location, out_name)
                if os.path.exists(out_path) and not overwrite_existing:
                    _msg("{} ({}): exists, skipping ({}).".format(cluster_id, source, out_name))
                    if build_pyramids:
                        _build_pyramids_stats(out_path)   # idempotent via SKIP_EXISTING
                    continue
                tiles, sr, cell, res_used = _gather_product_tiles(
                    present_folders, product_prefix, tile_resolution)
                if not tiles:
                    continue
                with _CleanupOnError(out_path):
                    arcpy.management.MosaicToNewRaster(
                        input_rasters=tiles,
                        output_location=location,
                        raster_dataset_name_with_extension=out_name,
                        coordinate_system_for_the_raster=sr,
                        pixel_type=pixel_type,
                        number_of_bands=1,
                        mosaic_method=mosaic_method,
                    )
                if build_pyramids:
                    _build_pyramids_stats(out_path)
                size_label = "{}, {:g} m".format(res_used, cell) if res_used else "{:g} m".format(cell)
                _msg("{} ({}): mosaicked {} tiles ({}) from {} member area(s) -> {}".format(
                    cluster_id, source, len(tiles), size_label, len(member_areas), out_name))
                made += 1

            with open(os.path.join(location, cluster_id + "_members.txt"), "w", encoding="utf-8") as fh:
                fh.write("{}\n".format(cluster_id))
                fh.write("Member areas ({}):\n".format(len(member_areas)))
                for a in member_areas:
                    fh.write("  {}\n".format(a))
                fh.write("FIDs: {}\n".format(", ".join(str(f) for f in all_fids)))
            if made:
                built += 1

        arcpy.ResetProgressor()
        if not dry_run:
            _msg("Clusters done. Built: {}. Skipped: {}. Output in '{}'.".format(
                built, skipped, cluster_root))

    def _map_folders_by_name(self, data_folders, fid_to_area):
        """Map each AOI feature to the download folder whose name equals the sanitized area name.
        This is the exact pairing for folders produced by Tool 1 (Download DGT Data), which names
        each folder by the area name field; no geometry or FID number is involved, and folders not
        downloaded yet are simply absent (their areas are skipped, to be built on a later re-run)."""
        # Fail loud when two DIFFERENT area values sanitize to the same folder name (for example
        # "Sao Joao" and "S. Joao"): both would silently read the same folder and one area's
        # mosaic would be built from the other's tiles.
        claimed = {}
        # Strip like build_area_groups does, so "Covas" and "Covas " (a stray space) count as the
        # SAME raw value (one area, no false collision) rather than two different names.
        for raw in set(str(r).strip() for r in fid_to_area.values()):
            claimed.setdefault(sanitize_name(raw), set()).add(raw)
        collisions = {s: raws for s, raws in claimed.items() if len(raws) > 1}
        if collisions:
            detail = "; ".join("'{}' <- {}".format(s, ", ".join(sorted(raws)))
                               for s, raws in sorted(collisions.items()))
            msg = ("Different area names sanitize to the same folder name, so the by name mapping "
                   "is ambiguous: {}. Rename the areas, or use the by geometry mapping.").format(detail)
            _err(msg)
            raise ValueError(msg)

        by_name = {name: full for name, full in data_folders}
        fid_to_folder = {}
        for fid, raw in fid_to_area.items():
            folder = by_name.get(sanitize_name(str(raw)))
            if folder is not None:
                fid_to_folder[fid] = folder
        _msg("Mapped {} of {} AOI feature(s) to a folder by name.".format(
            len(fid_to_folder), len(fid_to_area)))
        return fid_to_folder

    def _area_extent(self, geoms, aoi_sr, tile_sr):
        """Combined extent of the area's AOI polygon(s) in the tile CRS, as an arcpy.Extent, used to
        bound the mosaic to the AOI (for example a cartogram sheet) through the analysis extent, so
        the mosaic comes out already cut with no extra clip pass. Returns None when there is no
        usable geometry. This is the extent (a clean cut for a rectangular sheet); it does not mask
        an irregular polygon to its shape."""
        box = None
        for g in geoms:
            if g is None:
                continue
            gt = _project_geom(g, aoi_sr, tile_sr)
            e = gt.extent
            if box is None:
                box = [e.XMin, e.YMin, e.XMax, e.YMax]
            else:
                box = [min(box[0], e.XMin), min(box[1], e.YMin),
                       max(box[2], e.XMax), max(box[3], e.YMax)]
        return arcpy.Extent(*box) if box is not None else None

    def _clip_to_polygon(self, in_raster, out_raster, geoms, aoi_sr, tile_sr):
        """Cut a mosaic to the true shape of the area's AOI polygon(s) with core Clip (dissolved
        and projected to the tile CRS); cells outside become NoData. The input mosaic is already
        bounded to the AOI extent, so this clip runs on a small raster and stays fast."""
        union = None
        for g in geoms:
            if g is None:
                continue
            gt = _project_geom(g, aoi_sr, tile_sr)
            union = gt if union is None else union.union(gt)
        if union is None:
            arcpy.management.CopyRaster(in_raster, out_raster)
            return
        clip_fc = "in_memory/_clip_aoi"
        if arcpy.Exists(clip_fc):
            arcpy.management.Delete(clip_fc)
        arcpy.management.CopyFeatures([union], clip_fc)
        try:
            arcpy.management.Clip(in_raster, "#", out_raster, clip_fc, "#", "ClippingGeometry")
        finally:
            arcpy.management.Delete(clip_fc)

    def _map_folders_by_geometry(self, data_folders, fid_to_geom, fid_to_area, aoi_sr):
        """Map each AOI feature to its download folder by geometry, not by the FID number in
        the folder name. For each feature, pick the folder whose tile extent center is nearest
        the feature bounding box center, among folders whose extent contains that center. Each
        download is the feature buffered symmetrically, so the folder extent center matches the
        feature box center; this holds even for areas a few hundred meters apart, and is immune
        to a shapefile whose FID order no longer matches the folder numbering. Folder extent
        comes from the .vrt, else the union of the tile extents. A feature with no folder extent
        around it is left unmapped and its area is skipped, since a downloaded folder always has
        a resolvable extent, so no match means the data is not there. Tiles are EPSG:3763.
        """
        tile_sr = arcpy.SpatialReference(PROJECT_EPSG)

        folders = []                       # (full, extent_polygon, center_x, center_y)
        for name, full in data_folders:
            ext = _folder_extent_resolved(full, tile_sr)
            if ext is None:
                _warn("Folder '{}' has no .vrt and no readable tiles; cannot place it by "
                      "geometry.".format(name))
                continue
            ce = ext.extent
            folders.append((full, ext, (ce.XMin + ce.XMax) / 2.0, (ce.YMin + ce.YMax) / 2.0))
        if not folders:
            msg = ("No data folder yielded a tile extent (.vrt or tiles), so the geometry "
                   "mapping has nothing to match. Switch 'Folder to area mapping' to "
                   "'by FID number'.")
            _err(msg)
            raise ValueError(msg)

        tie_tolerance = 500.0              # meters; flag near ties for the user to verify
        fid_to_folder = {}
        ambiguous = []
        unmatched = []
        for fid, geom in fid_to_geom.items():
            if geom is None:
                continue
            projected = _project_geom(geom, aoi_sr, tile_sr)
            fe = projected.extent
            fx = (fe.XMin + fe.XMax) / 2.0
            fy = (fe.YMin + fe.YMax) / 2.0
            pt = arcpy.PointGeometry(arcpy.Point(fx, fy), tile_sr)
            best_full = None
            best_d = None
            second_d = None
            for full, ext, cx, cy in folders:
                if not ext.contains(pt):
                    continue
                d = (fx - cx) ** 2 + (fy - cy) ** 2
                if best_d is None or d < best_d:
                    second_d = best_d
                    best_full, best_d = full, d
                elif second_d is None or d < second_d:
                    second_d = d
            if best_full is not None:
                fid_to_folder[fid] = best_full
                if second_d is not None and (second_d ** 0.5 - best_d ** 0.5) < tie_tolerance:
                    ambiguous.append(fid)
            else:
                unmatched.append(fid)

        present = sum(1 for g in fid_to_geom.values() if g is not None)
        _msg("Mapped {} of {} AOI feature(s) to a folder by geometry.".format(
            len(fid_to_folder), present))
        for fid in ambiguous:
            _warn("Area '{}' (FID {}): two folders were nearly equidistant; geometry picked the "
                  "nearest, verify it.".format(fid_to_area.get(fid, "?"), fid))
        if unmatched:
            _warn("{} AOI feature(s) had no folder extent around them and were left unmapped "
                  "(not downloaded yet): FID {}.".format(
                      len(unmatched), ", ".join(str(f) for f in sorted(unmatched))))
        return fid_to_folder

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_aoi = parameters[0].valueAsText
        area_field = parameters[1].valueAsText
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
        resolutions = [v.strip() for v in (parameters[12].values or []) if v and v.strip()]
        build_cluster_mosaics = bool(parameters[13].value)
        cluster_dry_run = bool(parameters[14].value)
        mapping_mode = parameters[15].valueAsText
        clip_mode = parameters[16].valueAsText or "none"
        build_pyramids = bool(parameters[17].value)

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
                  "shapefile FID. If this layer is not that shapefile, the folder to area "
                  "mapping may be wrong.".format(oid_field))

        # Read fid -> (raw name, geometry).
        fid_to_area = {}
        fid_to_geom = {}
        with arcpy.da.SearchCursor(in_aoi, ["OID@", "SHAPE@", area_field]) as cursor:
            for oid, shape, raw in cursor:
                fid_to_area[int(oid)] = raw
                fid_to_geom[int(oid)] = shape

        if not fid_to_area:
            msg = "AOI layer '{}' has no features.".format(in_aoi)
            _err(msg)
            raise ValueError(msg)

        # One output per area. Features sharing an Area value are merged.
        groups = build_area_groups([(fid, fid_to_area[fid]) for fid in fid_to_area])

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

        if mapping_mode == "by name":
            fid_to_folder = self._map_folders_by_name(data_folders, fid_to_area)
        elif mapping_mode == "by geometry":
            fid_to_folder = self._map_folders_by_geometry(
                data_folders, fid_to_geom, fid_to_area, aoi_sr)
        else:
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

        if len(resolutions) > 1:
            # One output tree per resolution (out/<res>/...), same file names inside each, so
            # the naming convention and every downstream tool work unchanged per tree.
            for res in resolutions:
                res_root = os.path.join(out_folder, res)
                _msg("Resolution {}: building into {}.".format(res, res_root))
                self._build_for_resolution(
                    groups, fid_to_geom, fid_to_folder, products, res_root, output_structure,
                    pixel_type, mosaic_method, overwrite_existing, skip_incomplete,
                    verify_extent, mapping_mode, clip_mode, build_pyramids, res, aoi_sr,
                    build_cluster_mosaics, cluster_dry_run)
        else:
            tile_resolution = resolutions[0] if resolutions else None
            self._build_for_resolution(
                groups, fid_to_geom, fid_to_folder, products, out_folder, output_structure,
                pixel_type, mosaic_method, overwrite_existing, skip_incomplete,
                verify_extent, mapping_mode, clip_mode, build_pyramids, tile_resolution, aoi_sr,
                build_cluster_mosaics, cluster_dry_run)
        return

    def _build_for_resolution(self, groups, fid_to_geom, fid_to_folder, products, out_folder,
                              output_structure, pixel_type, mosaic_method, overwrite_existing,
                              skip_incomplete, verify_extent, mapping_mode, clip_mode,
                              build_pyramids, tile_resolution, aoi_sr,
                              build_cluster_mosaics, cluster_dry_run):
        """Build the per area mosaics (and optional clusters) for one tile resolution into
        out_folder. Called once per selected resolution, each with its own output tree."""
        total = len(groups)
        built_areas = 0
        present_areas = 0
        created_count = 0
        skipped_no_data = []
        skipped_incomplete = []
        skipped_no_tiles = []
        skipped_mismatch = []

        arcpy.SetProgressor("step", "Building one mosaic per area...", 0, total, 1)
        for final_area, fids in groups:
            arcpy.SetProgressorPosition()
            present = [f for f in fids if f in fid_to_folder]
            missing = [f for f in fids if f not in fid_to_folder]

            if not present:
                _warn("Area '{}': no download folders present yet (FIDs {}). Skipping.".format(
                    final_area, fids))
                skipped_no_data.append(final_area)
                continue
            if missing and skip_incomplete:
                _warn("Area '{}': missing folders for FIDs {}. Skipping (incomplete). Re-run "
                      "when the download finishes.".format(final_area, missing))
                skipped_incomplete.append(final_area)
                continue

            present_folders = [fid_to_folder[f] for f in present]

            # Gather tiles per product first. This validates the tile CRS (fail loud) and
            # yields the tile spatial reference used by the extent check and the mosaic.
            gathered = {}              # source -> (tiles, tile_sr, cell, resolution)
            ref_tile_sr = None
            for source, product_prefix in products:
                tiles, sr, cell, res_used = _gather_product_tiles(
                    present_folders, product_prefix, tile_resolution)
                if tiles:
                    gathered[source] = (tiles, sr, cell, res_used)
                    if ref_tile_sr is None:
                        ref_tile_sr = sr
            if not gathered:
                _warn("Area '{}': folders present but no DEM/DSM tiles found. Skipping.".format(final_area))
                skipped_no_tiles.append(final_area)
                continue

            # Spatial sanity check: each folder's VRT extent must contain its FID's AOI
            # polygon centroid. Catches a wrong FID to folder mapping (disjoint was too weak,
            # an adjacent folder still overlaps).
            if verify_extent and mapping_mode == "by FID number":
                mismatch = False
                for f in present:
                    folder_poly = _folder_extent_polygon(fid_to_folder[f], ref_tile_sr)
                    if folder_poly is None:
                        _warn("Area '{}': folder for FID {} has no .vrt, cannot verify extent.".format(
                            final_area, f))
                        continue
                    geom = fid_to_geom[f]
                    if geom is None:
                        continue
                    geom_t = _project_geom(geom, aoi_sr, ref_tile_sr)
                    centroid = arcpy.PointGeometry(geom_t.centroid, ref_tile_sr)
                    if not folder_poly.contains(centroid):
                        _warn("Area '{}': folder for FID {} does not contain its AOI polygon "
                              "centroid. Possible wrong FID mapping. Skipping this area.".format(
                                  final_area, f))
                        mismatch = True
                        break
                if mismatch:
                    skipped_mismatch.append(final_area)
                    continue

            if missing:
                _warn("Area '{}': building a PARTIAL mosaic, missing FIDs {}. Re-run with "
                      "overwrite enabled after the download finishes to refresh it.".format(
                          final_area, missing))

            created_here = 0
            existing_here = 0
            for source, (tiles, sr, cell, res_used) in gathered.items():
                out_name = build_output_name(final_area, source) + ".tif"
                location = out_folder
                if output_structure == "per_area_subfolder":
                    location = os.path.join(out_folder, final_area)
                if not os.path.isdir(location):
                    os.makedirs(location)
                out_path = os.path.join(location, out_name)

                if os.path.exists(out_path) and not overwrite_existing:
                    _msg("Area '{}' ({}): output exists, skipping ({}).".format(
                        final_area, source, out_name))
                    if build_pyramids:
                        # SKIP_EXISTING makes this idempotent, so the option also serves a re-run
                        # that only adds pyramids to mosaics built earlier.
                        _build_pyramids_stats(out_path)
                    existing_here += 1
                    continue
                if os.path.exists(out_path) and overwrite_existing:
                    _msg("Area '{}' ({}): overwriting existing {}.".format(final_area, source, out_name))

                geoms = [fid_to_geom[f] for f in present]
                clip_extent = self._area_extent(geoms, aoi_sr, sr) if clip_mode != "none" else None
                # polygon mode: mosaic into a temp bounded to the AOI extent (small window), then
                # cut it to the true polygon shape with core Clip. Cheap on an area-sized raster;
                # the extent mode remains the fast exact cut for rectangular sheets.
                to_polygon = clip_mode == "polygon" and clip_extent is not None
                mosaic_name = ("_full_" + out_name) if to_polygon else out_name
                mosaic_path = os.path.join(location, mosaic_name)
                prev_extent = arcpy.env.extent
                try:
                    if clip_extent is not None:
                        arcpy.env.extent = clip_extent   # bound the mosaic to the AOI extent
                    with _CleanupOnError(mosaic_path, out_path):
                        arcpy.management.MosaicToNewRaster(
                            input_rasters=tiles,
                            output_location=location,
                            raster_dataset_name_with_extension=mosaic_name,
                            coordinate_system_for_the_raster=sr,
                            pixel_type=pixel_type,
                            number_of_bands=1,
                            mosaic_method=mosaic_method,
                        )
                        if to_polygon:
                            self._clip_to_polygon(mosaic_path, out_path, geoms, aoi_sr, sr)
                finally:
                    arcpy.env.extent = prev_extent
                    if to_polygon:
                        _delete_partial_raster(mosaic_path)
                if build_pyramids:
                    _build_pyramids_stats(out_path)
                size_label = "{}, {:g} m".format(res_used, cell) if res_used else "{:g} m".format(cell)
                clip_note = {"extent": ", clipped to the AOI extent",
                             "polygon": ", clipped to the AOI polygon"}.get(
                                 clip_mode if clip_extent is not None else "none", "")
                _msg("Area '{}' ({}): mosaicked {} tiles ({}) from {} folder(s){} -> {}".format(
                    final_area, source, len(tiles), size_label, len(present_folders), clip_note, out_name))
                created_here += 1

            created_count += created_here
            if created_here:
                built_areas += 1
            elif existing_here:
                present_areas += 1

        arcpy.ResetProgressor()

        _msg("Done. Areas: {}. Built now: {}. Already present: {}. Mosaics created: {}. Skipped "
             "(no data: {}, incomplete: {}, no tiles: {}, extent mismatch: {}).".format(
                 total, built_areas, present_areas, created_count,
                 len(skipped_no_data), len(skipped_incomplete),
                 len(skipped_no_tiles), len(skipped_mismatch)))
        if skipped_incomplete:
            _warn("Incomplete, re-run after download finishes: " + ", ".join(skipped_incomplete))
        if skipped_no_tiles:
            _warn("Folders present but no tiles found: " + ", ".join(skipped_no_tiles))
        if skipped_mismatch:
            _warn("Extent mismatch, check FID mapping: " + ", ".join(skipped_mismatch))

        if build_cluster_mosaics:
            self._build_clusters(
                groups, fid_to_geom, fid_to_folder, products, out_folder, pixel_type,
                mosaic_method, tile_resolution, skip_incomplete, overwrite_existing,
                cluster_dry_run, build_pyramids)
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
        self.label = "03 - Generate Surfaces"
        self.description = ("Derive topographic surfaces (slope in degrees and percent, aspect, "
                            "hillshade, profile and plan curvature) in batch from the per area DEM and DSM mosaics "
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
            displayName="Output folder (per_area_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Input")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_area_subfolder", "flat"]
        p_struct.value = "same_as_input"

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM", "BOTH"]   # same order in every tool
        p_source.value = "BOTH"

        p_slope = arcpy.Parameter(
            displayName="Slope (degrees)", name="do_slope",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_slope.value = True

        p_slopep = arcpy.Parameter(
            displayName="Slope (percent)", name="do_slope_percent",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_slopep.value = True          # the Suitability Mask defaults to a percent threshold (SLOPEP)

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

        return [p_in, p_recurse, p_out, p_struct, p_source, p_slope, p_slopep, p_aspect, p_hill,
                p_profc, p_planc, p_z, p_htype, p_az, p_alt, p_overwrite]

    def isLicensed(self):
        # Needs Spatial Analyst (slope/aspect/curvature/traditional hillshade). Image Analyst
        # is checked out at run time only for multidirectional hillshade.
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def updateParameters(self, parameters):
        # Output folder is only needed for per_area_subfolder or flat output.
        parameters[2].enabled = parameters[3].valueAsText != "same_as_input"
        # Azimuth/altitude only matter for a Traditional hillshade.
        traditional = bool(parameters[8].value) and parameters[12].valueAsText == "Traditional"
        parameters[13].enabled = traditional
        parameters[14].enabled = traditional
        return

    def updateMessages(self, parameters):
        if parameters[3].valueAsText != "same_as_input" and not parameters[2].valueAsText:
            parameters[2].setErrorMessage("Output folder is required unless output structure is "
                                          "same_as_input.")
        return

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        out_folder = parameters[2].valueAsText
        output_structure = parameters[3].valueAsText
        source_filter = parameters[4].valueAsText
        do_slope = bool(parameters[5].value)
        do_slope_percent = bool(parameters[6].value)
        do_aspect = bool(parameters[7].value)
        do_hillshade = bool(parameters[8].value)
        do_profc = bool(parameters[9].value)
        do_planc = bool(parameters[10].value)
        z_factor = float(parameters[11].value)
        hillshade_type = parameters[12].valueAsText
        azimuth = float(parameters[13].value) if parameters[13].value is not None else 315.0
        altitude = float(parameters[14].value) if parameters[14].value is not None else 45.0
        overwrite_existing = bool(parameters[15].value)

        arcpy.env.overwriteOutput = overwrite_existing
        multidirectional = do_hillshade and hillshade_type == "Multidirectional"

        if output_structure != "same_as_input" and not out_folder:
            msg = "Output folder is required unless output structure is same_as_input."
            _err(msg)
            raise ValueError(msg)

        wanted = []
        if do_slope:
            wanted.append("SLOPE")
        if do_slope_percent:
            wanted.append("SLOPEP")
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

            # Do not descend into the Resample tree: it holds same-named copies of the base
            # mosaics at another cell size, which would be processed a second time.
            mosaics = _find_base_mosaics(in_folder, recurse, allowed_sources)

            total = len(mosaics)
            created = 0
            failed = []
            arcpy.SetProgressor("step", "Deriving surfaces...", 0, total, 1)
            for path, area, source in mosaics:
                arcpy.SetProgressorPosition()
                try:
                    created += self._surfaces_for_mosaic(
                        path, area, source, out_folder, output_structure, wanted,
                        overwrite_existing, z_factor, multidirectional, azimuth, altitude)
                    _msg("{} ({}): surfaces done.".format(area, source))
                except Exception as exc:
                    _warn("{} ({}): surfaces failed: {}. Skipping this mosaic; a re-run retries it "
                          "(check the output is not locked by an open map or another process)."
                          .format(area, source, exc))
                    failed.append("{} ({})".format(area, source))
                    # A long run can exhaust process memory or handles (seen as ERROR 010240 on
                    # every save from some point on, typically the ia.Hillshade). Reclaim what we
                    # can and keep going; the summary tells the user to restart Pro if it persists.
                    import gc
                    gc.collect()

            arcpy.ResetProgressor()
            _msg("Done. Mosaics processed: {}. Surfaces created: {}. Failed: {}.".format(
                total, created, len(failed)))
        finally:
            arcpy.CheckInExtension("Spatial")
            if ia_checked_out:
                arcpy.CheckInExtension("ImageAnalyst")
        # Fail loud: if any mosaic failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = ("Surfaces failed for {} mosaic(s): {}. Re-run with overwrite off to retry just "
                   "these. If many areas fail in a row late in a long run (ERROR 010240 on save), "
                   "restart ArcGIS Pro first to free process memory, and check the free space on "
                   "the scratch drive.").format(len(failed), ", ".join(failed))
            _err(msg)
            raise RuntimeError(msg)
        return

    def _surfaces_for_mosaic(self, path, area, source, out_folder, output_structure, wanted,
                             overwrite_existing, z_factor, multidirectional, azimuth, altitude):
        """Derive the wanted surfaces for one mosaic (idempotent). Returns the number created.
        Isolated so a failure on one mosaic (for example a locked output) can be caught and the
        batch can continue with the rest."""
        _assert_projected_raster(path)
        if output_structure == "same_as_input":
            location = os.path.dirname(path)
        elif output_structure == "per_area_subfolder":
            location = os.path.join(out_folder, area)
        else:
            location = out_folder
        if not os.path.isdir(location):
            os.makedirs(location)

        # Resolve which selected surfaces still need writing (idempotency).
        targets = {}
        todo = {}
        for product in wanted:
            target = os.path.join(location, build_output_name(area, source, product) + ".tif")
            targets[product] = target
            if os.path.exists(target) and not overwrite_existing:
                _msg("{} ({}): {} exists, skipping.".format(area, source, os.path.basename(target)))
                todo[product] = False
            else:
                todo[product] = True

        created = 0
        if todo.get("SLOPE"):
            _save_raster(arcpy.sa.Slope(path, "DEGREE", z_factor), targets["SLOPE"])
            created += 1
        if todo.get("SLOPEP"):
            _save_raster(arcpy.sa.Slope(path, "PERCENT_RISE", z_factor), targets["SLOPEP"])
            created += 1
        if todo.get("ASPECT"):
            # Aspect takes no z_factor (it is direction only); z_factor applies to slope,
            # hillshade and curvature.
            _save_raster(arcpy.sa.Aspect(path), targets["ASPECT"])
            created += 1
        if todo.get("HILLSHADE"):
            if multidirectional:
                # arcpy.ia.Hillshade hillshade_type: 1 = multidirectional, 0 = single direction.
                _save_raster(arcpy.ia.Hillshade(path, hillshade_type=1, z_factor=z_factor),
                             targets["HILLSHADE"])
            else:
                _save_raster(arcpy.sa.Hillshade(path, azimuth, altitude, "SHADOWS", z_factor),
                             targets["HILLSHADE"])
            created += 1

        # Profile and plan curvature share one Curvature call; pass only the wanted outputs.
        want_profc = todo.get("PROFC", False)
        want_planc = todo.get("PLANC", False)
        if want_profc or want_planc:
            with _CleanupOnError(targets["PROFC"] if want_profc else None,
                                 targets["PLANC"] if want_planc else None):
                arcpy.sa.Curvature(path, z_factor,
                                   targets["PROFC"] if want_profc else "#",
                                   targets["PLANC"] if want_planc else "#")
            created += (1 if want_profc else 0) + (1 if want_planc else 0)
        return created


class Contours(object):
    def __init__(self):
        self.label = "07 - Contours"
        self.description = ("Generate cartographic contour lines from the per area DEM or DSM "
                            "mosaics, with a chosen equidistance and an optional master (index) "
                            "interval. The surface is smoothed before contouring and the lines are "
                            "smoothed after, both given in meters so the same values work for a "
                            "0.5 m or a 2 m mosaic. Output is one polyline shapefile per area in a "
                            "Contours subfolder.")
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
            displayName="Output folder (per_area_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Input")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_area_subfolder", "flat"]
        p_struct.value = "same_as_input"

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM", "BOTH"]
        p_source.value = "DEM"

        p_interval = arcpy.Parameter(
            displayName="Contour interval / equidistance (meters)", name="contour_interval",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_interval.value = 5

        p_index = arcpy.Parameter(
            displayName="Master (index) interval (meters, 0 = none)", name="index_interval",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_index.value = 25

        p_base = arcpy.Parameter(
            displayName="Base contour (meters)", name="base_contour",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_base.value = 0

        p_smooth_dem = arcpy.Parameter(
            displayName="DEM smoothing radius (meters, 0 = none)", name="dem_smooth_radius",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_smooth_dem.value = 5

        p_smooth_line = arcpy.Parameter(
            displayName="Line smoothing tolerance (meters, PAEK, 0 = none)", name="line_smooth_tol",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_smooth_line.value = 12

        p_min_length = arcpy.Parameter(
            displayName="Minimum contour length (meters, 0 = keep all)", name="min_length",
            datatype="GPDouble", parameterType="Optional", direction="Input")
        p_min_length.value = 0

        p_z = arcpy.Parameter(
            displayName="Z factor", name="z_factor",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_z.value = 1

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_out, p_struct, p_source, p_interval, p_index, p_base,
                p_smooth_dem, p_smooth_line, p_min_length, p_z, p_overwrite]

    def isLicensed(self):
        # Contour and Focal Statistics need Spatial Analyst; Smooth Line is core.
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
        interval = parameters[5].value
        if interval is not None and interval <= 0:
            parameters[5].setErrorMessage("The contour interval must be greater than 0.")
        index = parameters[6].value
        if interval and index and index > 0 and abs((index / interval) - round(index / interval)) > 1e-6:
            parameters[6].setWarningMessage("The master interval is usually a multiple of the "
                                            "contour interval.")
        return

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        out_folder = parameters[2].valueAsText
        output_structure = parameters[3].valueAsText
        source_filter = parameters[4].valueAsText
        interval = float(parameters[5].value)
        index_interval = float(parameters[6].value or 0)
        base = float(parameters[7].value or 0)
        smooth_dem_m = float(parameters[8].value or 0)
        smooth_line_m = float(parameters[9].value or 0)
        min_length_m = float(parameters[10].value or 0)
        z_factor = float(parameters[11].value)
        overwrite_existing = bool(parameters[12].value)

        if interval <= 0:
            msg = "The contour interval must be greater than 0."
            _err(msg)
            raise ValueError(msg)
        if output_structure != "same_as_input" and not out_folder:
            msg = "Output folder is required unless output structure is same_as_input."
            _err(msg)
            raise ValueError(msg)

        if arcpy.CheckExtension("Spatial") != "Available":
            msg = "Spatial Analyst extension is not available. It is required for this tool."
            _err(msg)
            raise RuntimeError(msg)
        arcpy.CheckOutExtension("Spatial")
        arcpy.env.overwriteOutput = overwrite_existing
        try:
            allowed_sources = SOURCES if source_filter == "BOTH" else (source_filter,)

            # Skip our own Contours output and the Resample tree (same-named mosaic copies).
            mosaics = _find_base_mosaics(in_folder, recurse, allowed_sources,
                                         (RESAMPLE_DIRNAME, CONTOURS_DIRNAME))

            total = len(mosaics)
            created = 0
            skipped = 0
            failed = []
            arcpy.SetProgressor("step", "Generating contours...", 0, total, 1)
            for path, area, source in mosaics:
                arcpy.SetProgressorPosition()
                try:
                    sr = _assert_projected_raster(path)
                    if output_structure == "same_as_input":
                        base_loc = os.path.dirname(path)
                    elif output_structure == "per_area_subfolder":
                        base_loc = os.path.join(out_folder, area)
                    else:
                        base_loc = out_folder
                    location = os.path.join(base_loc, CONTOURS_DIRNAME)
                    if not os.path.isdir(location):
                        os.makedirs(location)
                    out_shp = os.path.join(location, build_output_name(area, source, "CONT") + ".shp")

                    if arcpy.Exists(out_shp) and not overwrite_existing:
                        _msg("{} ({}): contours exist, skipping.".format(area, source))
                        skipped += 1
                        continue

                    cell = arcpy.Raster(path).meanCellWidth
                    if interval < 2 * cell:
                        _warn("{} ({}): a {:g} m interval is small for the {:g} m cell size; the "
                              "contours may be dense and noisy.".format(area, source, interval, cell))

                    n = self._contours_for(path, out_shp, interval, index_interval, base, z_factor,
                                           smooth_dem_m, smooth_line_m, min_length_m)
                    master_note = ", master {:g} m".format(index_interval) if index_interval > 0 else ""
                    _msg("{} ({}, {}): {} contours ({:g} m interval{}) -> {}".format(
                        area, source, _crs_label(sr), n, interval, master_note,
                        os.path.basename(out_shp)))
                    created += 1
                except Exception as exc:
                    _warn("{} ({}): contours failed: {}. Skipping this mosaic; a re-run retries "
                          "it.".format(area, source, exc))
                    failed.append("{} ({})".format(area, source))

            arcpy.ResetProgressor()
            _msg("Done. Mosaics: {}. Contour layers created: {}. Skipped existing: {}. "
                 "Failed: {}.".format(total, created, skipped, len(failed)))
        finally:
            arcpy.CheckInExtension("Spatial")
        # Fail loud: if any mosaic failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = "Contours failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return

    def _contours_for(self, dem_path, out_shp, interval, index_interval, base, z_factor,
                      smooth_dem_m, smooth_line_m, min_length_m):
        """Smooth the surface (Focal MEAN in map units, so it is resolution aware), contour, smooth
        the lines (PAEK, meters), flag the master contours and drop the short ones, then write
        out_shp. Returns the contour feature count. On failure the partial out_shp is deleted, so a
        re-run does not skip a truncated layer, and the scratch temps are removed either way."""
        scratch = arcpy.env.scratchGDB
        tmp_lines = os.path.join(scratch, "cont_lines")
        tmp_smooth = os.path.join(scratch, "cont_smooth")
        for tmp in (tmp_lines, tmp_smooth):
            if arcpy.Exists(tmp):
                arcpy.management.Delete(tmp)

        try:
            surface = dem_path
            if smooth_dem_m and smooth_dem_m > 0:
                # NbrCircle in MAP units keeps the smoothing scale in meters at any cell size.
                surface = arcpy.sa.FocalStatistics(
                    dem_path, arcpy.sa.NbrCircle(smooth_dem_m, "MAP"), "MEAN")

            arcpy.sa.Contour(surface, tmp_lines, interval, base, z_factor)

            result = tmp_lines
            if smooth_line_m and smooth_line_m > 0:
                arcpy.cartography.SmoothLine(tmp_lines, tmp_smooth, "PAEK",
                                             "{} Meters".format(smooth_line_m))
                result = tmp_smooth

            if min_length_m and min_length_m > 0:
                with arcpy.da.UpdateCursor(result, ["SHAPE@LENGTH"]) as cur:
                    for (length,) in cur:
                        if length is not None and length < min_length_m:
                            cur.deleteRow()

            if index_interval and index_interval > 0:
                arcpy.management.AddField(result, "master", "SHORT")
                with arcpy.da.UpdateCursor(result, ["Contour", "master"]) as cur:
                    for value, _m in cur:
                        if value is None:
                            cur.updateRow((value, 0))
                            continue
                        ratio = value / index_interval
                        cur.updateRow((value, 1 if abs(ratio - round(ratio)) < 1e-6 else 0))

            count = int(arcpy.management.GetCount(result)[0])
            arcpy.management.CopyFeatures(result, out_shp)
            return count
        except Exception:
            # Never leave a truncated shapefile for a later run to skip as complete.
            try:
                if arcpy.Exists(out_shp):
                    arcpy.management.Delete(out_shp)
            except Exception:
                pass
            raise
        finally:
            for tmp in (tmp_lines, tmp_smooth):
                if arcpy.Exists(tmp):
                    arcpy.management.Delete(tmp)


class ReclassifyFactor(object):
    def __init__(self):
        self.label = "05 - Reclassify Factors"
        self.description = ("Reclassify the Aspect, Slope and annual Solar rasters into the "
                            "project suitability classes (a fixed scheme), in batch. Aspect "
                            "yields two rasters: quadrants (ASPECT_DIR) and solar suitability "
                            "(ASPECT_RCL); Slope and Solar yield SLOPE_RCL and SOLARUNI_RCL. "
                            "Solar reclassifies the uniform-sky annual raster (SOLARUNI) only, "
                            "not the overcast variant. Each output, plus a legend, goes to a "
                            "Reclass subfolder. Uses numpy for deterministic boundaries.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder (the Tool 3 and Tool 4 outputs)", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_factors = arcpy.Parameter(
            displayName="Factors to reclassify", name="factors",
            datatype="GPString", parameterType="Required", direction="Input", multiValue=True)
        p_factors.filter.type = "ValueList"
        p_factors.filter.list = ["ASPECT", "SLOPE", "SOLAR"]
        p_factors.value = ["ASPECT", "SLOPE", "SOLAR"]

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_factors, p_overwrite]

    def isLicensed(self):
        # Pure numpy reclassification (RasterToNumPyArray / NumPyArrayToRaster). No extension.
        return True

    def _schemes_for(self, area, source, product):
        """Output (filename, classes, flat_class) tuples for one source product. Aspect yields
        two rasters (quadrants and suitability); slope and solar yield one each."""
        if product == "ASPECT":
            return [
                (build_output_name(area, source, "ASPECT_DIR") + ".tif",
                 ASPECT_DIR_CLASSES, ASPECT_DIR_FLAT),
                (build_output_name(area, source, "ASPECT", reclass=True) + ".tif",
                 ASPECT_RCL_CLASSES, ASPECT_RCL_FLAT),
            ]
        if product == "SLOPE":
            return [(build_output_name(area, source, "SLOPE", reclass=True) + ".tif",
                     SLOPE_RCL_CLASSES, None)]
        return [(build_output_name(area, source, "SOLARUNI", reclass=True) + ".tif",
                 SOLAR_RCL_CLASSES, None)]

    def _write_legend(self, location, names):
        """Write the class matrix (legend) into a Reclass subfolder, listing the files there."""
        lines = ["Dados reclassificados (Tool 05)",
                 "===============================",
                 "FONTE: DEM = Modelo Digital do Terreno; DSM = Modelo Digital de Superficie.",
                 "",
                 "Ficheiros nesta pasta:"]
        for n in sorted(names):
            lines.append("  " + n)
        lines.append("")
        lines.append(RECLASS_LEGEND_TEXT)
        with open(os.path.join(location, "RECLASS_legenda.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        factors = [v.strip().upper() for v in (parameters[2].valueAsText or "").split(";") if v.strip()]
        overwrite_existing = bool(parameters[3].value)

        arcpy.env.overwriteOutput = overwrite_existing

        # Validate the fixed schemes before writing anything: a bad edit to the class constants
        # (a gap or an overlap between different ids) would otherwise make silent 9999 holes.
        for scheme in (ASPECT_DIR_CLASSES, ASPECT_RCL_CLASSES, SLOPE_RCL_CLASSES, SOLAR_RCL_CLASSES):
            validate_value_table(scheme)

        wanted = set()
        if "ASPECT" in factors:
            wanted.add("ASPECT")
        if "SLOPE" in factors:
            wanted.add("SLOPE")
        if "SOLAR" in factors:
            wanted.add("SOLARUNI")
        if not wanted:
            msg = "Select at least one factor to reclassify (Aspect, Slope or Solar)."
            _err(msg)
            raise ValueError(msg)

        # Discover the source rasters (not already reclassified). Skip our own Reclass subfolder;
        # the Resample tree is deliberately NOT skipped, so resampled factors can be reclassified.
        rasters = []
        for dirpath, fn in _walk_tifs(in_folder, recurse, (RECLASS_DIRNAME,)):
            info = parse_source_and_product(fn)
            if (info["product"] not in wanted or info["area"] is None
                    or info["source"] is None or info["reclass"]):
                continue
            rasters.append((os.path.join(dirpath, fn), info["area"], info["source"], info["product"]))

        if not rasters:
            msg = "No factor rasters ({}) found in '{}'.".format(", ".join(sorted(wanted)), in_folder)
            _err(msg)
            raise ValueError(msg)

        total = len(rasters)
        created = 0
        skipped_existing = 0
        failed = []
        folders_written = {}                                  # Reclass subfolder -> [output names]
        arcpy.SetProgressor("step", "Reclassifying factors...", 0, total, 1)
        for path, area, source, product in rasters:
            arcpy.SetProgressorPosition()
            location = os.path.join(os.path.dirname(path), RECLASS_DIRNAME)
            folders_written.setdefault(location, [])
            try:
                made, skipped = self._reclassify_raster(
                    path, area, source, product, location, folders_written[location],
                    overwrite_existing)
                created += made
                skipped_existing += skipped
            except Exception as exc:
                _warn("{} ({}): reclassification failed: {}. Skipping this raster; a re-run "
                      "retries it.".format(area, source, exc))
                failed.append("{} ({} {})".format(area, source, product))
                import gc
                gc.collect()                  # free the numpy array of a failed raster

        for location, names in folders_written.items():
            if os.path.isdir(location):
                self._write_legend(location, names)

        arcpy.ResetProgressor()
        _msg("Done. Source rasters: {}. Reclassified outputs: {}. Skipped existing: {}. "
             "Failed: {}. NoData: {}.".format(total, created, skipped_existing, len(failed),
                                              RECLASS_NODATA))
        # Fail loud: if any raster failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = "Reclassification failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return

    def _reclassify_raster(self, path, area, source, product, location, names,
                           overwrite_existing):
        """Reclassify one factor raster into its scheme outputs (idempotent). Returns
        (created, skipped). Isolated so a failure on one raster (a locked output, memory) can be
        caught and the batch can continue with the rest."""
        created = 0
        skipped = 0
        src = arcpy.Raster(path)
        # A single band annual total is required. A multiband SOLARUNI is Tool 4 run with the
        # time-interval option on (one band per interval, not the annual total); reclassifying it
        # against the annual breaks would silently produce nonsense, so fail loud.
        if int(getattr(src, "bandCount", 1) or 1) != 1:
            msg = ("{} ({}): {} has {} bands. This tool needs a single band raster; a multiband "
                   "SOLARUNI comes from Tool 4 with time intervals on, which is not the annual "
                   "total. Re-run Tool 4 without the interval option."
                   .format(area, source, product, src.bandCount))
            _err(msg)
            raise ValueError(msg)
        sr = src.spatialReference
        sr_defined = not (sr is None or sr.name in (None, "", "Unknown"))
        if not sr_defined:
            _warn("{} ({}): input {} has an undefined CRS; the outputs CRS will be undefined "
                  "too.".format(area, source, product))
        lower_left = arcpy.Point(src.extent.XMin, src.extent.YMin)
        cell_w = src.meanCellWidth
        cell_h = src.meanCellHeight
        arr = None                                        # read once per source, lazily

        for out_name, classes, flat_class in self._schemes_for(area, source, product):
            if out_name not in names:
                names.append(out_name)
            out_path = os.path.join(location, out_name)
            if os.path.exists(out_path) and not overwrite_existing:
                _msg("{} ({}): {} exists, skipping.".format(area, source, out_name))
                skipped += 1
                continue

            if arr is None:
                # Read as float (Slope/Aspect/Solar are float) and map the raster's own
                # NoData to NaN, so reclassify_array sends those cells to the output NoData.
                try:
                    arr = arcpy.RasterToNumPyArray(path).astype("float32")
                except MemoryError:
                    raise MemoryError(
                        "{} ({}) {} is too large to load into memory for reclassification. "
                        "Reduce the extent or process fewer rasters at a time.".format(
                            area, source, product))
                if src.noDataValue is not None:
                    arr[arr == src.noDataValue] = float("nan")

            if not os.path.isdir(location):
                os.makedirs(location)
            out_arr = reclassify_array(arr, classes, RECLASS_NODATA,
                                       flat_value=-1 if flat_class is not None else None,
                                       flat_class=flat_class)
            with _CleanupOnError(out_path):
                out_raster = arcpy.NumPyArrayToRaster(out_arr, lower_left, cell_w, cell_h,
                                                      value_to_nodata=RECLASS_NODATA)
                out_raster.save(out_path)
                if sr_defined:
                    # NumPyArrayToRaster leaves the CRS undefined; set it when it is known.
                    arcpy.management.DefineProjection(out_path, sr)
            _msg("{} ({}): {} ({}) -> {}".format(area, source, product, _crs_label(sr), out_name))
            created += 1
        return created, skipped


class SolarRadiation(object):
    def __init__(self):
        self.label = "04 - Solar Radiation"
        self.description = ("Compute annual incoming solar radiation (global, kWh/m2) per area from the "
                            "DEM mosaics, in batch, with arcpy.sa.RasterSolarRadiation (GPU accelerated). "
                            "Choose the diffuse model (uniform, overcast, or both), and optionally output "
                            "the direct, diffuse and direct duration rasters. Baseline runs at the native "
                            "2 m cell size; a coarser solar cell size resamples first. This is the heavy "
                            "tool; expect long run times.")
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
            displayName="Output folder (per_area_subfolder or flat only)", name="out_folder",
            datatype="DEFolder", parameterType="Optional", direction="Input")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["same_as_input", "per_area_subfolder", "flat"]
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
        p_cell.value = 0

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

        p_diffuse_model = arcpy.Parameter(
            displayName="Diffuse model type", name="diffuse_model_type",
            datatype="GPString", parameterType="Required", direction="Input")
        p_diffuse_model.filter.type = "ValueList"
        p_diffuse_model.filter.list = ["UNIFORM_SKY", "STANDARD_OVERCAST_SKY", "BOTH"]
        p_diffuse_model.value = "UNIFORM_SKY"

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        p_out_direct = arcpy.Parameter(
            displayName="Output Direct Radiation raster", name="out_direct",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_out_direct.value = True

        p_out_diffuse = arcpy.Parameter(
            displayName="Output Diffuse Radiation raster", name="out_diffuse",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_out_diffuse.value = True

        p_out_duration = arcpy.Parameter(
            displayName="Output Direct Duration raster (hours)", name="out_duration",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_out_duration.value = True

        p_reuse = arcpy.Parameter(
            displayName="Reuse Tool 3 slope and aspect (native runs only)", name="reuse_surfaces",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_reuse.value = False

        p_grid = arcpy.Parameter(
            displayName="Sun map grid level (5 to 7; higher is more accurate and slower)",
            name="sunmap_grid_level", datatype="GPLong", parameterType="Required", direction="Input")
        p_grid.filter.type = "ValueList"
        p_grid.filter.list = [5, 6, 7]
        p_grid.value = 7

        p_use_interval = arcpy.Parameter(
            displayName="Calculate insolation from time intervals", name="use_time_interval",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_use_interval.value = False

        p_interval_unit = arcpy.Parameter(
            displayName="Time interval unit", name="interval_unit",
            datatype="GPString", parameterType="Optional", direction="Input")
        p_interval_unit.filter.type = "ValueList"
        p_interval_unit.filter.list = ["MINUTE", "HOUR", "DAY", "WEEK"]
        p_interval_unit.value = "DAY"

        p_interval = arcpy.Parameter(
            displayName="Time interval", name="interval",
            datatype="GPLong", parameterType="Optional", direction="Input")
        p_interval.value = 14

        return [p_in, p_recurse, p_out, p_struct, p_source, p_cell, p_method,
                p_year, p_neigh, p_trans, p_diff, p_diffuse_model,
                p_out_direct, p_out_diffuse, p_out_duration, p_reuse,
                p_grid, p_use_interval, p_interval_unit, p_interval, p_overwrite]

    def isLicensed(self):
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def updateParameters(self, parameters):
        parameters[2].enabled = parameters[3].valueAsText != "same_as_input"
        use_interval = bool(parameters[17].value)
        parameters[18].enabled = use_interval
        parameters[19].enabled = use_interval
        return

    def updateMessages(self, parameters):
        if parameters[3].valueAsText != "same_as_input" and not parameters[2].valueAsText:
            parameters[2].setErrorMessage("Output folder is required unless output structure is "
                                          "same_as_input.")
        return

    def _reuse_slope_aspect(self, mosaic_path, area, source, native, sr):
        """Find the Tool 03 SLOPE and ASPECT rasters next to the mosaic, to pass as
        in_slope_raster / in_aspect_raster. Returns (slope, aspect) or (None, None).

        Only returned when both exist and match the DEM cell size and CRS, since
        RasterSolarRadiation silently resamples a mismatched grid. The slope/aspect units
        RasterSolarRadiation expects are not documented by Esri, so reuse is off by default;
        validate with an A/B run (internal vs reused, both native) before trusting it.
        """
        folder = os.path.dirname(mosaic_path)
        slope = os.path.join(folder, build_output_name(area, source, "SLOPE") + ".tif")
        aspect = os.path.join(folder, build_output_name(area, source, "ASPECT") + ".tif")
        if not (os.path.exists(slope) and os.path.exists(aspect)):
            _warn("{} ({}): reuse requested but SLOPE/ASPECT not found next to the mosaic; "
                  "computing them internally.".format(area, source))
            return None, None
        for p in (slope, aspect):
            r = arcpy.Raster(p)
            if abs(r.meanCellWidth - native) > 1e-6:
                _warn("{} ({}): {} cell size differs from the DEM; computing internally to avoid "
                      "silent resampling.".format(area, source, os.path.basename(p)))
                return None, None
            if not _same_crs(r.spatialReference, sr):
                _warn("{} ({}): {} CRS differs from the DEM; computing internally.".format(
                    area, source, os.path.basename(p)))
                return None, None
        return slope, aspect

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
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
        diffuse_model = parameters[11].valueAsText
        out_direct = bool(parameters[12].value)
        out_diffuse = bool(parameters[13].value)
        out_duration = bool(parameters[14].value)
        reuse_surfaces = bool(parameters[15].value)
        grid_level = int(parameters[16].value)
        interval_mode = "INTERVAL" if bool(parameters[17].value) else "NO_INTERVAL"
        interval_unit = parameters[18].valueAsText or "DAY"
        interval = int(parameters[19].value or 14)   # optional param; guard a cleared value
        overwrite_existing = bool(parameters[20].value)

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

        # Diffuse model(s) to run; each is (API value, name suffix). BOTH is two runs.
        if diffuse_model == "BOTH":
            models = [("UNIFORM_SKY", "UNI"), ("STANDARD_OVERCAST_SKY", "OVC")]
        elif diffuse_model == "STANDARD_OVERCAST_SKY":
            models = [("STANDARD_OVERCAST_SKY", "OVC")]
        else:
            models = [("UNIFORM_SKY", "UNI")]

        allowed_sources = SOURCES if source_filter == "BOTH" else (source_filter,)
        start_date = "1/1/{}".format(year)
        end_date = "12/31/{}".format(year)

        # Do not descend into the Resample tree (same-named mosaic copies at another cell size).
        mosaics = _find_base_mosaics(in_folder, recurse, allowed_sources)

        total = len(mosaics)
        runs = total * len(models)
        size_label = "{} m".format(solar_cell_size) if solar_cell_size > 0 else "native 2 m"
        _warn("Solar radiation is a heavy whole year computation. {} run(s) ({} mosaic(s) x {} "
              "diffuse model(s)) at a {} cell size may take a long time; benchmark one area "
              "first.".format(runs, total, len(models), size_label))

        arcpy.CheckOutExtension("Spatial")
        created = 0
        skipped_existing = 0
        failed = []
        try:
            arcpy.SetProgressor("step", "Computing solar radiation...", 0, runs, 1)
            for path, area, source in mosaics:
                if source == "DSM":
                    _warn("{}: solar is computed on the DSM (canopy and building surface), not the "
                          "bare ground a PV plant would sit on.".format(area))

                if output_structure == "same_as_input":
                    location = os.path.dirname(path)
                elif output_structure == "per_area_subfolder":
                    location = os.path.join(out_folder, area)
                else:
                    location = out_folder
                if not os.path.isdir(location):
                    os.makedirs(location)

                for api_value, suffix in models:
                    arcpy.SetProgressorPosition()
                    token = "SOLAR" + suffix                  # SOLARUNI / SOLAROVC
                    out_name = build_output_name(area, source, token) + ".tif"
                    out_path = os.path.join(location, out_name)

                    if os.path.exists(out_path) and not overwrite_existing:
                        _msg("{} ({}, {}): {} exists, skipping.".format(area, source, suffix, out_name))
                        skipped_existing += 1
                        continue

                    resampled = None
                    kwargs = {}
                    try:
                        sr = _assert_projected_raster(path)
                        native = arcpy.Raster(path).meanCellWidth
                        surface = path
                        did_resample = False
                        if solar_cell_size and solar_cell_size < native:
                            _warn("{} ({}): requested solar cell size {:g} m is finer than the native "
                                  "{:g} m; running at native.".format(area, source, solar_cell_size, native))
                        if solar_cell_size and solar_cell_size > native:
                            resampled = os.path.join(arcpy.env.scratchFolder,
                                                     "solar_{}.tif".format(area))
                            arcpy.management.Resample(
                                path, resampled, "{0} {0}".format(solar_cell_size), resample_method)
                            surface = resampled
                            did_resample = True

                        kwargs = dict(
                            in_surface_raster=surface,
                            start_date_time=start_date,
                            end_date_time=end_date,
                            use_time_interval=interval_mode,
                            neighborhood_distance=neighborhood,
                            use_adaptive_neighborhood="ADAPTIVE_NEIGHBORHOOD",
                            diffuse_model_type=api_value,
                            diffuse_proportion=diffuse_proportion,
                            transmittivity=transmittivity,
                            analysis_target_device="GPU_THEN_CPU",
                            sunmap_grid_level=grid_level,
                        )
                        if interval_mode == "INTERVAL":
                            kwargs["interval_unit"] = interval_unit
                            kwargs["interval"] = interval

                        # Reuse the Tool 03 slope/aspect only on a native run, so the grids
                        # match; a resampled surface uses the tool's internal slope/aspect.
                        in_slope = None
                        if reuse_surfaces and not did_resample:
                            in_slope, in_aspect = self._reuse_slope_aspect(path, area, source, native, sr)
                            if in_slope:
                                kwargs["in_slope_raster"] = in_slope
                                kwargs["in_aspect_raster"] = in_aspect

                        extras = []
                        if out_direct:
                            kwargs["out_direct_radiation_raster"] = os.path.join(
                                location, build_output_name(area, source, token + "DIR") + ".tif")
                            extras.append("direct")
                        if out_diffuse:
                            kwargs["out_diffuse_radiation_raster"] = os.path.join(
                                location, build_output_name(area, source, token + "DIF") + ".tif")
                            extras.append("diffuse")
                        if out_duration:
                            kwargs["out_duration_raster"] = os.path.join(
                                location, build_output_name(area, source, token + "DUR") + ".tif")
                            extras.append("duration")

                        rad = arcpy.sa.RasterSolarRadiation(**kwargs)
                        rad.save(out_path)
                        note = " reusing slope/aspect" if in_slope else ""
                        note += (" + " + ", ".join(extras)) if extras else ""
                        _msg("{} ({}, {}): solar radiation (kWh/m2, {}){} -> {}".format(
                            area, source, suffix, _crs_label(sr), note, out_name))
                        created += 1
                    except Exception as exc:
                        _delete_partial_raster(out_path)
                        for aux in ("out_direct_radiation_raster", "out_diffuse_radiation_raster",
                                    "out_duration_raster"):
                            if kwargs.get(aux):
                                _delete_partial_raster(kwargs[aux])
                        _err("{} ({}, {}): solar radiation failed: {}".format(area, source, suffix, exc))
                        failed.append("{} ({}, {})".format(area, source, suffix))
                    finally:
                        if resampled and arcpy.Exists(resampled):
                            arcpy.management.Delete(resampled)

            arcpy.ResetProgressor()
            _msg("Done. Mosaics: {}. Diffuse model(s): {}. Solar runs created: {}. Skipped existing: "
                 "{}. Failed: {}.".format(total, len(models), created, skipped_existing, len(failed)))
        finally:
            arcpy.CheckInExtension("Spatial")
        # Fail loud: if any run failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = "Solar radiation failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return


class Resample(object):
    def __init__(self):
        self.label = "06 - Resample"
        self.description = ("Resample the named factor rasters (mosaics and surfaces) to a target "
                            "cell size, in batch, selecting which data types to process. The type "
                            "list is filled from what actually exists under the folder you pick, so "
                            "you can point it at the whole project root and it finds every product "
                            "wherever it was written. Outputs go to a 'Resample' folder inside that "
                            "root, grouped by area, file names unchanged, so everything lands in one "
                            "place. When a product exists in more than one resolution, one is chosen "
                            "per the resolution setting. Continuous rasters use bilinear; the class "
                            "rasters (_RCL, APT, APTCLS, ASPECT_DIR) and aspect use nearest, or "
                            "MAJORITY when you set it, to preserve the classes.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder (a 'Resample' subfolder is created here)", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_types = arcpy.Parameter(
            displayName="Data types to resample", name="types",
            datatype="GPString", parameterType="Required", direction="Input", multiValue=True)
        p_types.filter.type = "ValueList"
        # The full list, from the naming convention (updateParameters narrows it to what is really
        # under the chosen folder). CONT is a shapefile (Tool 7), so it has no raster to resample.
        p_types.filter.list = _resample_all_types()
        p_types.value = ["DEM", "DSM"]

        p_cell = arcpy.Parameter(
            displayName="Target cell size (meters)", name="target_cell_size",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_cell.value = 5

        p_method = arcpy.Parameter(
            displayName="Resampling method", name="method",
            datatype="GPString", parameterType="Optional", direction="Input")
        p_method.filter.type = "ValueList"
        p_method.filter.list = ["auto", "NEAREST", "MAJORITY", "BILINEAR", "CUBIC"]
        p_method.value = "auto"          # bilinear for continuous, nearest for categorical

        p_res_policy = arcpy.Parameter(
            displayName="Source resolution when a product exists in several", name="res_policy",
            datatype="GPString", parameterType="Optional", direction="Input")
        p_res_policy.filter.type = "ValueList"
        p_res_policy.filter.list = ["finest", "coarsest"]
        p_res_policy.value = "finest"    # matches the Suitability Mask, which also picks the finest

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_types, p_cell, p_method, p_res_policy, p_overwrite]

    def isLicensed(self):
        # Core Data Management Resample, no extension needed.
        return True

    def updateParameters(self, parameters):
        # Fill the type list from what is actually under the chosen folder, so the dialog offers
        # only products that exist there (point at the project root and it shows everything that
        # was generated). Rescan only when the folder or recurse just changed, to keep the dialog
        # snappy; the run rediscovers from disk anyway. Falls back to the full list on an empty or
        # unreadable folder, so nothing is ever hidden by mistake.
        p_in, p_recurse, p_types = parameters[0], parameters[1], parameters[2]
        if (p_in.altered or p_recurse.altered) and p_in.valueAsText:
            present = _present_type_tokens(p_in.valueAsText, bool(p_recurse.value),
                                           (RESAMPLE_DIRNAME,), _resample_all_types())
            p_types.filter.list = [t for t in _resample_all_types() if t in present] or \
                _resample_all_types()
            if p_types.values:
                keep = [v for v in p_types.values if v in set(p_types.filter.list)]
                p_types.value = keep
        return

    def updateMessages(self, parameters):
        if parameters[3].value is not None and float(parameters[3].value) <= 0:
            parameters[3].setErrorMessage("Target cell size must be a positive number of meters.")
        return

    def _pick_by_resolution(self, found, res_policy):
        """Keep one raster per (area, token) when it exists in several resolutions, chosen by cell
        size (finest = smallest, coarsest = largest). Returns (kept_list, deduped_count)."""
        by_key = {}
        for item in found:
            _path, info, token = item
            by_key.setdefault((info["area"], token), []).append(item)
        kept = []
        deduped = 0
        for group in by_key.values():
            if len(group) == 1:
                kept.append(group[0])
                continue
            ordered = sorted(group, key=lambda it: arcpy.Raster(it[0]).meanCellWidth)
            kept.append(ordered[0] if res_policy == "finest" else ordered[-1])
            deduped += len(group) - 1
        return kept, deduped

    def _restrict_to_source_classes(self, src_path, out_path):
        """Set to NoData any cell of the resampled raster whose value is not a class present in the
        source. ArcGIS Resample with MAJORITY can read an 8-bit mask's NoData region and stamp
        out-of-range values along the boundary; this scrubs them so only real classes survive.
        Returns the count of cells scrubbed (0 leaves the raster untouched)."""
        import numpy as np
        src = arcpy.Raster(src_path)
        src_nd = src.noDataValue
        valid = set(int(v) for v in np.unique(arcpy.RasterToNumPyArray(src)))
        if src_nd is not None:
            valid.discard(int(src_nd))

        out = arcpy.Raster(out_path)
        out_nd = int(out.noDataValue) if out.noDataValue is not None else 255
        lower_left = arcpy.Point(out.extent.XMin, out.extent.YMin)
        cell_w, cell_h = out.meanCellWidth, out.meanCellHeight
        sr = out.spatialReference
        arr = arcpy.RasterToNumPyArray(out, nodata_to_value=out_nd)
        # Flag ONLY cells holding a real value that is not a source class. Exclude the NoData
        # background (already out_nd); counting it would balloon the number to the whole frame
        # even though nothing real was touched.
        bad = ~np.isin(arr, list(valid)) & (arr != out_nd)
        n = int(bad.sum())
        if n == 0:
            return 0

        arr[bad] = out_nd
        del out                                   # release the handle before replacing the file
        tmp = os.path.join(os.path.dirname(out_path), "_scrub_" + os.path.basename(out_path))
        arcpy.NumPyArrayToRaster(arr, lower_left, cell_w, cell_h,
                                 value_to_nodata=out_nd).save(tmp)
        arcpy.management.Delete(out_path)         # removes the .tif and its sidecars
        arcpy.management.Rename(tmp, out_path)
        if sr is not None and sr.name not in (None, "", "Unknown"):
            arcpy.management.DefineProjection(out_path, sr)
        return n

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        selected = set(str(s).upper() for s in (parameters[2].values or []))
        target_cell = float(parameters[3].value)
        method_choice = (parameters[4].valueAsText or "auto")
        res_policy = (parameters[5].valueAsText or "finest")
        overwrite_existing = bool(parameters[6].value)

        arcpy.env.overwriteOutput = overwrite_existing

        if target_cell <= 0:
            msg = "Target cell size must be a positive number of meters."
            _err(msg)
            raise ValueError(msg)
        if not selected:
            msg = "Select at least one data type to resample."
            _err(msg)
            raise ValueError(msg)

        out_root = os.path.join(in_folder, RESAMPLE_DIRNAME)

        # Discover the named rasters of the selected types, never descending into the Resample
        # output folder so a re-run does not resample its own outputs.
        found = []
        for dirpath, fn in _walk_tifs(in_folder, recurse, (RESAMPLE_DIRNAME,)):
            info = parse_source_and_product(fn)
            token = _resample_type_token(info)
            if token is None or token not in selected:
                continue
            found.append((os.path.join(dirpath, fn), info, token))

        if not found:
            msg = "No rasters of the selected types ({}) found in '{}'.".format(
                ", ".join(sorted(selected)), in_folder)
            _err(msg)
            raise ValueError(msg)

        # No silent skips: name the selected types that had no raster under the folder.
        missing = sorted(selected - {t for _p, _i, t in found})
        if missing:
            _warn("Selected but not found under '{}': {}. Nothing was resampled for them."
                  .format(in_folder, ", ".join(missing)))

        # The same product can exist in more than one resolution (a 2m and a 50cm tree) and would
        # otherwise write to the same output name, colliding. Group by (area, token) and keep one
        # per the resolution policy, so a run over the whole project root consolidates cleanly.
        rasters, deduped = self._pick_by_resolution(found, res_policy)
        if deduped:
            _msg("{} product(s) existed in more than one resolution; kept the {} of each."
                 .format(deduped, res_policy))

        _msg("Resampling {} raster(s) to {:g} m into '{}'.".format(len(rasters), target_cell, out_root))
        total = len(rasters)
        created = 0
        skipped_existing = 0
        copied_native = 0
        failed = []
        interpolated = []              # categorical rasters an explicit method would interpolate
        incompatible = []              # continuous rasters skipped because MAJORITY needs integer
        cleaned_total = 0              # boundary garbage cells scrubbed from class rasters
        arcpy.SetProgressor("step", "Resampling rasters...", 0, total, 1)
        for path, info, token in rasters:
            arcpy.SetProgressorPosition()
            area = info["area"]
            fn = os.path.basename(path)
            location = os.path.join(out_root, area)
            if not os.path.isdir(location):
                os.makedirs(location)
            out_path = os.path.join(location, fn)

            if os.path.exists(out_path) and not overwrite_existing:
                _msg("{} ({}): {} exists, skipping.".format(area, token, fn))
                skipped_existing += 1
                continue

            try:
                src = arcpy.Raster(path)
                native = src.meanCellWidth
                sr = src.spatialReference
                if abs(native - target_cell) <= 1e-6:
                    # Already at the target: copy it through so the Resample folder is a
                    # complete set of the selected types at the target resolution.
                    arcpy.management.CopyRaster(path, out_path)
                    _msg("{} ({}): already at {:g} m, copied -> {}".format(area, token, native, fn))
                    copied_native += 1
                    continue
                if target_cell < native:
                    _warn("{} ({}): target {:g} m is finer than native {:g} m; upsampling adds no "
                          "real detail.".format(area, token, target_cell, native))
                # Class rasters and the circular aspect must not be interpolated (see the
                # constants); everything else is a continuous measure and takes bilinear.
                no_interp = (info["reclass"] or info["product"] in CATEGORICAL_PRODUCTS
                             or info["product"] in CIRCULAR_PRODUCTS)
                if method_choice == "auto":
                    method = "NEAREST" if no_interp else "BILINEAR"
                else:
                    method = method_choice
                    # MAJORITY needs an integer raster (ArcGIS ERROR 001847 on floating point), so
                    # a continuous type in the selection would abort. Skip it with a clear note
                    # instead: MAJORITY is for the class rasters, deselect DEM/DSM/slope/solar.
                    if method == "MAJORITY" and not is_integer_pixel_type(src.pixelType):
                        _warn("{} ({}): MAJORITY only applies to integer class rasters; this one is "
                              "floating point, skipped. Deselect the continuous types (DEM, DSM, "
                              "SLOPE, ASPECT, SOLAR) when resampling with MAJORITY."
                              .format(area, token))
                        incompatible.append("{} ({})".format(area, token))
                        continue
                    if no_interp and method in ("BILINEAR", "CUBIC"):
                        interpolated.append("{} ({})".format(area, token))
                arcpy.management.Resample(path, out_path, "{0} {0}".format(target_cell), method)
                # MAJORITY resample of an 8-bit mask can read the NoData region and stamp
                # out-of-range garbage (4, 27, 207) along the boundary. For an integer class raster
                # scrub any value the source does not have back to NoData. A clean raster is a
                # no-op. Continuous rasters (float) are left alone.
                cleaned = 0
                if is_integer_pixel_type(src.pixelType) and no_interp:
                    cleaned = self._restrict_to_source_classes(path, out_path)
                    if cleaned:
                        cleaned_total += cleaned
                _msg("{} ({}): resampled {:g} m -> {:g} m ({}, {}) -> {}{}".format(
                    area, token, native, target_cell, method, _crs_label(sr), fn,
                    ", scrubbed {} boundary cell(s)".format(cleaned) if cleaned else ""))
                created += 1
            except Exception as exc:
                _delete_partial_raster(out_path)
                _err("{} ({}): resample failed: {}".format(area, token, exc))
                failed.append("{} ({})".format(area, token))

        arcpy.ResetProgressor()
        _msg("Done. Selected rasters: {}. Resampled: {}. Copied (already at target): {}. "
             "Skipped existing: {}. Skipped (MAJORITY needs integer): {}. Failed: {}.".format(
                 total, created, copied_native, skipped_existing, len(incompatible), len(failed)))
        if cleaned_total:
            _msg("Scrubbed {} boundary cell(s) of resample garbage (out-of-class values set to "
                 "NoData) across the class rasters.".format(cleaned_total))
        if interpolated:
            # The user asked for it explicitly, so this is a warning, not a failure.
            _warn("{} class or aspect raster(s) were resampled with {}, which interpolates: it "
                  "invents class values that are not in the legend, and for aspect it averages "
                  "across the 0/360 wrap and the -1 flat sentinel. Affected: {}. Use MAJORITY or "
                  "NEAREST for these.".format(len(interpolated), method_choice,
                                              ", ".join(interpolated)))
        if failed:
            msg = "Resample failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return


class VerifyOutputs(object):
    def __init__(self):
        self.label = "08 - Verify Outputs"
        self.description = ("Read-only integrity check of a results tree. Verifies every .tif "
                            "(GeoTIFF signature, opens in arcpy, CRS matches the expected EPSG) "
                            "and, per area with a base mosaic, that the expected products exist. "
                            "The Resample tree is checked for integrity but not for completeness "
                            "(it is a copy set). Nothing is written except an optional report.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder", name="results_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_expected = arcpy.Parameter(
            displayName="Expected products per area", name="expected_products",
            datatype="GPString", parameterType="Optional", direction="Input", multiValue=True)
        p_expected.filter.type = "ValueList"
        # CONT is a shapefile and APT/APTCLS are per mine in their own folder, so none can be found by
        # the per area .tif completeness check; offering them would always report missing.
        p_expected.filter.list = [p for p in PRODUCTS if p not in ("CONT", "APT", "APTCLS")]
        p_expected.value = ["SLOPE", "ASPECT", "HILLSHADE", "PROFC", "PLANC", "SOLARUNI"]

        p_epsg = arcpy.Parameter(
            displayName="Expected EPSG", name="expected_epsg",
            datatype="GPLong", parameterType="Required", direction="Input")
        p_epsg.value = PROJECT_EPSG

        p_report = arcpy.Parameter(
            displayName="Write VERIFY_report.txt in the results folder", name="write_report",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_report.value = True

        return [p_in, p_recurse, p_expected, p_epsg, p_report]

    def isLicensed(self):
        return True                        # read-only, no extension needed

    def execute(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        expected = list(parameters[2].values) if parameters[2].values else []
        expected_epsg = int(parameters[3].value)
        write_report = bool(parameters[4].value)

        # Verify scans everything, including the Resample tree (integrity, not completeness),
        # so no directory is pruned; under_resample is derived per file from the path.
        rasters = []                       # (path, info dict, under_resample)
        for dirpath, fn in _walk_tifs(in_folder, recurse):
            parts = os.path.normpath(os.path.relpath(dirpath, in_folder)).split(os.sep)
            under_resample = RESAMPLE_DIRNAME in parts
            rasters.append((os.path.join(dirpath, fn), parse_source_and_product(fn),
                            under_resample))
        if not rasters:
            msg = "No .tif rasters found in '{}'.".format(in_folder)
            _err(msg)
            raise ValueError(msg)

        corrupt = []
        wrong_crs = []
        base_seen = set()                  # (area, source) with a base mosaic, main tree only
        products_seen = set()              # (area, source, product) in the main tree
        arcpy.SetProgressor("step", "Verifying rasters...", 0, len(rasters), 1)
        for path, info, under_resample in rasters:
            arcpy.SetProgressorPosition()
            rel = os.path.relpath(path, in_folder)
            if not _file_looks_valid(path):
                corrupt.append(rel + " (not a valid GeoTIFF signature)")
                continue
            try:
                sr = arcpy.Describe(path).spatialReference
            except Exception as exc:
                corrupt.append("{} (arcpy cannot open it: {})".format(rel, exc))
                continue
            code = getattr(sr, "factoryCode", 0) if sr is not None else 0
            if code != expected_epsg:
                wrong_crs.append("{} ({})".format(rel, _crs_label(sr)))
            if not under_resample and info["area"] and info["source"] and not info["reclass"]:
                if info["product"] is None:
                    base_seen.add((info["area"], info["source"]))
                else:
                    products_seen.add((info["area"], info["source"], info["product"]))

        arcpy.ResetProgressor()
        missing = []
        for area, source in sorted(base_seen):
            for product in expected:
                if (area, source, product) not in products_seen:
                    missing.append("{} ({}): {}".format(area, source, product))

        _msg("Verified {} raster(s). Areas with a base mosaic: {}.".format(
            len(rasters), len(base_seen)))
        for label, items in (("Corrupt or unreadable", corrupt),
                             ("CRS differs from EPSG:{}".format(expected_epsg), wrong_crs),
                             ("Missing expected products", missing)):
            if items:
                _warn("{}: {}".format(label, len(items)))
                for item in items[:20]:
                    _warn("  " + item)
                if len(items) > 20:
                    _warn("  ... and {} more (see the report).".format(len(items) - 20))
        if not (corrupt or wrong_crs or missing):
            _msg("No problems found.")

        if write_report:
            report = os.path.join(in_folder, "VERIFY_report.txt")
            with open(report, "w", encoding="utf-8") as fh:
                fh.write("Verify Outputs report\n")
                fh.write("Folder: {}\n".format(in_folder))
                fh.write("Rasters checked: {}\n".format(len(rasters)))
                fh.write("Areas with a base mosaic: {}\n".format(len(base_seen)))
                fh.write("Expected products: {}\n\n".format(", ".join(expected) or "(none)"))
                for label, items in (("Corrupt or unreadable", corrupt),
                                     ("CRS differs from EPSG:{}".format(expected_epsg), wrong_crs),
                                     ("Missing expected products", missing)):
                    fh.write("{}: {}\n".format(label, len(items)))
                    for item in items:
                        fh.write("  {}\n".format(item))
                    fh.write("\n")
            _msg("Report written to {}".format(report))
        return


class SuitabilityMask(object):
    def __init__(self):
        self.label = "09 - Suitability Mask"
        self.description = ("Binary suitability mask per mine: 1 where the slope is at or below "
                            "the threshold AND the annual solar radiation (SOLARUNI) is at or "
                            "above the minimum, else 0 (cells that are NoData in either input "
                            "stay NoData), clipped to each mask polygon. Polygons sharing a name "
                            "are treated as one mine. The output is one <Mine>_<SOURCE>_APT.tif "
                            "per mine. Optionally a second raster <Mine>_<SOURCE>_APTCLS.tif is "
                            "written: the binary mask multiplied by the annual solar radiation in "
                            "suitability classes 1 to 6, so the suitable cells keep their solar "
                            "class (3 to 6) and the rest is 0. Weighted overlay and exclusion "
                            "masks remain outside this toolbox.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder (with SLOPE or SLOPEP, and SOLARUNI rasters)",
            name="results_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM"]
        p_source.value = "DEM"

        p_mask = arcpy.Parameter(
            displayName="Mask polygons (mines)", name="mask_layer",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_mask.filter.list = ["Polygon"]

        p_field = arcpy.Parameter(
            displayName="Mine name field", name="name_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_field.parameterDependencies = ["mask_layer"]
        p_field.filter.list = ["Text", "Short", "Long"]

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_units = arcpy.Parameter(
            displayName="Slope threshold units", name="slope_units",
            datatype="GPString", parameterType="Required", direction="Input")
        p_units.filter.type = "ValueList"
        p_units.filter.list = ["degrees", "percent"]
        p_units.value = "percent"          # the suitability rule is defined in percent; needs SLOPEP

        p_slope = arcpy.Parameter(
            displayName="Maximum suitable slope", name="slope_max",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_slope.value = 20

        p_solar = arcpy.Parameter(
            displayName="Minimum suitable solar radiation (kWh/m2)", name="solar_min",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_solar.value = 1400                   # the lower bound of SOLARUNI class 3

        p_classified = arcpy.Parameter(
            displayName="Also output solar-class suitability (mask x solar class 1 to 6)",
            name="output_classified", datatype="GPBoolean", parameterType="Optional",
            direction="Input")
        p_classified.value = True

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_source, p_mask, p_field, p_out,
                p_units, p_slope, p_solar, p_classified, p_overwrite]

    def isLicensed(self):
        # Con and ExtractByMask need Spatial Analyst.
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def updateMessages(self, parameters):
        if parameters[7].value is not None and float(parameters[7].value) <= 0:
            parameters[7].setErrorMessage("The slope threshold must be greater than 0.")
        return

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        prev_extent = arcpy.env.extent
        prev_snap = arcpy.env.snapRaster
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite
            arcpy.env.extent = prev_extent
            arcpy.env.snapRaster = prev_snap

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        source = parameters[2].valueAsText
        mask_layer = parameters[3].valueAsText
        name_field = parameters[4].valueAsText
        out_folder = parameters[5].valueAsText
        slope_units = parameters[6].valueAsText
        slope_max = float(parameters[7].value)
        solar_min = float(parameters[8].value)
        classified = bool(parameters[9].value)
        overwrite_existing = bool(parameters[10].value)

        arcpy.env.overwriteOutput = overwrite_existing
        # The unit picks the slope raster and the comparison is done in its native units, so no
        # conversion: percent compares the SLOPEP raster (Tool 3 slope in percent) against a
        # percent threshold; degrees compares the SLOPE raster against a degree threshold.
        if slope_units == "percent":
            slope_product = "SLOPEP"
        else:
            slope_product = "SLOPE"
        thr = slope_max
        _msg("Slope threshold: {:g} {} (compared against the {} {} raster).".format(
            slope_max, slope_units, source, slope_product))
        _msg("Solar minimum: {:g} kWh/m2 (SOLARUNI).".format(solar_min))

        if arcpy.CheckExtension("Spatial") != "Available":
            msg = "Spatial Analyst extension is not available. It is required for this tool."
            _err(msg)
            raise RuntimeError(msg)
        arcpy.CheckOutExtension("Spatial")
        try:
            # Discover the slope and SOLARUNI rasters per area (main tree, not Resample copies).
            # The same product can appear in more than one resolution tree (Tool 2 builds one per
            # resolution, e.g. 2m and 50cm), so collect every candidate now and pick the finest
            # resolution per area below, the same one for slope and solar.
            slope_cand = {}
            solar_cand = {}
            for dirpath, fn in _walk_tifs(in_folder, recurse, (RESAMPLE_DIRNAME,)):
                info = parse_source_and_product(fn)
                if info["area"] is None or info["source"] != source or info["reclass"]:
                    continue
                if info["product"] == slope_product:
                    slope_cand.setdefault(info["area"], []).append(os.path.join(dirpath, fn))
                elif info["product"] == "SOLARUNI":
                    solar_cand.setdefault(info["area"], []).append(os.path.join(dirpath, fn))

            if not (set(slope_cand) & set(solar_cand)):
                msg = ("No area has both a {0} {1} and a {0} SOLARUNI raster under '{2}'. Run "
                       "Tools 3 and 4 first (for a percent threshold, Tool 3 must output slope "
                       "in percent).").format(source, slope_product, in_folder)
                _err(msg)
                raise ValueError(msg)

            # Pick the finest resolution present for both inputs, per area.
            slope_by_area = {}
            solar_by_area = {}
            cell_cache = {}
            multi = []                 # areas that had the inputs in more than one resolution
            mixed = []                 # areas with no resolution shared by slope and solar
            for area in set(slope_cand) & set(solar_cand):
                sp, so, cell = self._pick_finest_pair(slope_cand[area], solar_cand[area], cell_cache)
                slope_by_area[area] = sp
                solar_by_area[area] = so
                if len(slope_cand[area]) > 1 or len(solar_cand[area]) > 1:
                    multi.append(area)
                if cell is None:
                    mixed.append(area)
            areas = sorted(slope_by_area)
            if multi:
                _msg("{} area(s) had the inputs in more than one resolution; used the finest "
                     "available (smallest cell size) for each.".format(len(multi)))
            if mixed:
                _warn("Slope and SOLARUNI shared no resolution for: {}. Used the finest of each; "
                      "verify these.".format(", ".join(sorted(mixed))))
            only_one = sorted(set(slope_cand) ^ set(solar_cand))
            if only_one:
                _warn("Areas missing one of the two inputs (skipped for spatial matching): {}"
                      .format(", ".join(only_one)))

            # A single band SOLARUNI (the annual total) is required. A multiband one is Tool 4 with
            # the time-interval option on, whose per-interval values would make almost nothing pass
            # the threshold, silently. Check once, fail loud with the offending area.
            for area in areas:
                bands = int(getattr(arcpy.Raster(solar_by_area[area]), "bandCount", 1) or 1)
                if bands != 1:
                    msg = ("SOLARUNI for '{}' has {} bands, so it is not the annual total (Tool 4 "
                           "was run with time intervals on). Re-run Tool 4 without the interval "
                           "option.".format(area, bands))
                    _err(msg)
                    raise ValueError(msg)

            # Group the mask polygons by mine name (same-name polygons are one mine). Fail loud
            # when two DIFFERENT names sanitize to the same output name: their polygons would
            # silently merge into one mask and one mine would get no output.
            aoi_sr = arcpy.Describe(mask_layer).spatialReference
            mines = {}
            raws_by_name = {}
            with arcpy.da.SearchCursor(mask_layer, [name_field, "SHAPE@"]) as cursor:
                for raw, shape in cursor:
                    if shape is None or raw in (None, ""):
                        _warn("Feature with no geometry or empty name; skipped.")
                        continue
                    name = sanitize_name(str(raw))
                    raws_by_name.setdefault(name, set()).add(str(raw).strip())
                    mines.setdefault(name, []).append(shape)
            collisions = {n: r for n, r in raws_by_name.items() if len(r) > 1}
            if collisions:
                detail = "; ".join("'{}' <- {}".format(n, ", ".join(sorted(r)))
                                   for n, r in sorted(collisions.items()))
                msg = ("Different mine names sanitize to the same output name, so their polygons "
                       "would merge into one mask: {}. Rename the mines.").format(detail)
                _err(msg)
                raise ValueError(msg)
            if not mines:
                msg = "No usable mask polygons in '{}'.".format(mask_layer)
                _err(msg)
                raise ValueError(msg)
            if not os.path.isdir(out_folder):
                os.makedirs(out_folder)

            created = 0
            skipped = 0
            failed = []
            ext_cache = {}                 # area -> (extent, sr), Describe-d once per area
            arcpy.SetProgressor("step", "Building suitability masks...", 0, len(mines), 1)
            for mine, shapes in sorted(mines.items()):
                arcpy.SetProgressorPosition()
                out_path = os.path.join(out_folder, build_output_name(mine, source, "APT") + ".tif")
                class_path = (os.path.join(out_folder, build_output_name(mine, source, "APTCLS")
                              + ".tif") if classified else None)
                outputs_done = os.path.exists(out_path) and (not classified
                                                             or os.path.exists(class_path))
                if outputs_done and not overwrite_existing:
                    _msg("{}: exists, skipping.".format(mine))
                    skipped += 1
                    continue
                try:
                    area = self._area_for_mine(mine, shapes, areas, slope_by_area, aoi_sr, ext_cache)
                    if area is None:
                        raise ValueError("no SLOPE/SOLARUNI raster covers this mine")
                    self._mask_for_mine(mine, shapes, slope_by_area[area], solar_by_area[area],
                                        thr, solar_min, aoi_sr, out_path, class_path)
                    outs = os.path.basename(out_path)
                    if class_path:
                        outs += ", " + os.path.basename(class_path)
                    _msg("{}: mask from '{}' (slope <= {:g} {} AND solar >= {:g}) -> {}".format(
                        mine, area, thr, slope_units, solar_min, outs))
                    created += 1
                except Exception as exc:
                    _warn("{}: failed: {}".format(mine, exc))
                    failed.append(mine)

            arcpy.ResetProgressor()
            _msg("Done. Mines: {}. Masks created: {}. Skipped existing: {}. Failed: {}.".format(
                len(mines), created, skipped, len(failed)))
        finally:
            arcpy.CheckInExtension("Spatial")
        # Fail loud: if any mine failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = "Suitability mask failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return

    def _cell(self, path, cache):
        """Mean cell width of a raster, cached by path (a light header read)."""
        if path not in cache:
            cache[path] = arcpy.Raster(path).meanCellWidth
        return cache[path]

    def _pick_finest_pair(self, slope_paths, solar_paths, cache):
        """Choose the slope and SOLARUNI rasters from the finest resolution present for BOTH
        (the smallest cell size shared by the two), so a mine never mixes grids. Returns
        (slope_path, solar_path, cell). When there is no shared resolution, returns the finest
        of each with cell None, so the caller can flag it."""
        sl = sorted((self._cell(p, cache), p) for p in slope_paths)
        so = sorted((self._cell(p, cache), p) for p in solar_paths)
        so_by_cell = {}
        for c, p in so:
            so_by_cell.setdefault(round(c, 3), p)      # finest first wins on a genuine duplicate
        for c, p in sl:
            if round(c, 3) in so_by_cell:
                return p, so_by_cell[round(c, 3)], c
        return sl[0][1], so[0][1], None

    def _area_for_mine(self, mine, shapes, areas, slope_by_area, aoi_sr, ext_cache):
        """Resolve which area raster covers a mine: by name first (the area named like the mine),
        else spatially (the raster whose extent contains the most polygon centroids). Extents are
        Describe-d once per area, cached across mines. Warns when part of a multi polygon mine
        falls outside the chosen raster extent, since those cells come out NoData."""
        def _info(area):
            if area not in ext_cache:
                desc = arcpy.Describe(slope_by_area[area])
                ext_cache[area] = (desc.extent, desc.spatialReference)
            return ext_cache[area]

        def _centroids_inside(area):
            ext, r_sr = _info(area)
            inside = 0
            for shape in shapes:
                geom = _project_geom(shape, aoi_sr, r_sr)
                c = geom.centroid
                if ext.XMin <= c.X <= ext.XMax and ext.YMin <= c.Y <= ext.YMax:
                    inside += 1
            return inside

        chosen = None
        if mine in areas:
            chosen = mine
        else:
            best = 0
            for area in areas:
                n = _centroids_inside(area)
                if n > best:
                    best, chosen = n, area
                    if n == len(shapes):
                        break
        if chosen is not None:
            inside = _centroids_inside(chosen)
            if inside < len(shapes):
                _warn("{}: {} of {} polygon(s) fall outside raster '{}'; those cells will be "
                      "NoData in the mask.".format(mine, len(shapes) - inside, len(shapes), chosen))
        return chosen

    def _mask_for_mine(self, mine, shapes, slope_path, solar_path, thr, solar_min,
                       aoi_sr, out_path, class_path=None):
        """Compute the binary mask over the mine polygons and write out_path. The analysis extent
        is bounded to the polygons so only that window is computed, snapped to the slope grid.
        thr is compared against the slope raster in its own units (degrees or percent).

        When class_path is given, also write the binary mask multiplied by the annual solar
        radiation reclassified into the suitability classes 1 to 6 (SOLAR_RCL_CLASSES, the same
        breaks as Tool 5), so suitable cells keep their solar class (3 to 6) and the rest is 0.
        NoData stays NoData in both outputs."""
        r_sr = arcpy.Describe(slope_path).spatialReference
        if not _same_crs(r_sr, arcpy.Describe(solar_path).spatialReference):
            raise ValueError("slope and SOLARUNI rasters have different CRS")
        geoms = [_project_geom(s, aoi_sr, r_sr) for s in shapes]

        union = geoms[0]
        for g in geoms[1:]:
            union = union.union(g)
        mask_fc = "in_memory/_apt_mask"
        if arcpy.Exists(mask_fc):
            arcpy.management.Delete(mask_fc)
        arcpy.management.CopyFeatures([union], mask_fc)

        prev_extent = arcpy.env.extent
        prev_snap = arcpy.env.snapRaster
        try:
            arcpy.env.snapRaster = slope_path
            arcpy.env.extent = union.extent
            slope_r = arcpy.sa.Raster(slope_path)
            solar_r = arcpy.sa.Raster(solar_path)
            apt = arcpy.sa.Con((slope_r <= thr) & (solar_r >= solar_min), 1, 0)
            _save_class_mask(arcpy.sa.ExtractByMask(apt, mask_fc), out_path)
            if class_path:
                # Result 2: the binary mask times the solar radiation in classes 1 to 6. The
                # breaks mirror Tool 5 (SOLAR_RCL_CLASSES); on continuous radiation a cell landing
                # exactly on a class boundary is negligible.
                remap = arcpy.sa.RemapRange([[lo, hi, cls] for (cls, lo, hi) in SOLAR_RCL_CLASSES])
                solar_cls = arcpy.sa.Reclassify(solar_r, "Value", remap, "NODATA")
                apt_cls = apt * solar_cls
                _save_class_mask(arcpy.sa.ExtractByMask(apt_cls, mask_fc), class_path)
        finally:
            arcpy.env.extent = prev_extent
            arcpy.env.snapRaster = prev_snap
            if arcpy.Exists(mask_fc):
                arcpy.management.Delete(mask_fc)


# arcpy Raster.pixelType strings mapped to the MosaicToNewRaster pixel_type wording. Only the
# integer types the class rasters use are listed; anything else falls back to a safe signed 16 bit.
_MOSAIC_PIXEL_TYPES = {
    "U1": "1_BIT", "U2": "2_BIT", "U4": "4_BIT",
    "U8": "8_BIT_UNSIGNED", "S8": "8_BIT_SIGNED",
    "U16": "16_BIT_UNSIGNED", "S16": "16_BIT_SIGNED",
    "U32": "32_BIT_UNSIGNED", "S32": "32_BIT_SIGNED",
    "F32": "32_BIT_FLOAT", "F64": "64_BIT",
}


def is_integer_pixel_type(pixel_type):
    """True when an arcpy Raster.pixelType string is an integer type. RasterToPolygon needs an
    integer raster, so a float surface must be rejected with a clear message rather than let
    arcpy fail with ERROR 010423. Pure (string in, bool out) so it is unit tested."""
    return bool(pixel_type) and str(pixel_type).upper().startswith(("U", "S"))


def class_products():
    """The class (categorical) products that can be merged or vectorized: the suitability and
    aspect quadrant rasters, plus the Tool 05 _RCL variants. Derived from the tuples, so a new
    product needs no edit here."""
    return list(CATEGORICAL_PRODUCTS) + [p + "_" + RECLASS_SUFFIX for p in RECLASSIFIED_PRODUCTS]


def _fill_class_product_list(p_in, p_recurse, p_products):
    """Fill a products multi-value list from the class rasters actually under the chosen folder, so
    the dialog offers only products that exist there instead of the full static list. Shared by
    Tools 10 and 11, which offer the same list and both discover with _group_class_rasters (no
    skip dir, so the Resample folder is included). Rescans only when the folder or recurse just
    changed, to keep the dialog snappy; the run rediscovers from disk anyway. Falls back to the
    full list on an empty or unreadable folder, so nothing is ever hidden by mistake, and prunes a
    selection that the new folder no longer offers."""
    if (p_in.altered or p_recurse.altered) and p_in.valueAsText:
        present = _present_type_tokens(p_in.valueAsText, bool(p_recurse.value), (),
                                       class_products())
        p_products.filter.list = [t for t in class_products() if t in present] or class_products()
        if p_products.values:
            keep = [v for v in p_products.values if v in set(p_products.filter.list)]
            p_products.value = keep


def _group_class_rasters(in_folder, recurse, source, selected):
    """Discover the class rasters under in_folder and group them by (source, product).

    Returns {(source, product_token): [(path, area), ...]}. product_token carries the _RCL suffix
    when the raster is reclassified, matching what class_products() offers. The Resample folder is
    NOT skipped here: it is usually exactly where the 5 m generalized rasters live, so the caller
    points at the folder it wants.
    """
    groups = {}
    for dirpath, fn in _walk_tifs(in_folder, recurse):
        info = parse_source_and_product(fn)
        if info["area"] is None or info["source"] is None:
            continue
        if source != "BOTH" and info["source"] != source:
            continue
        token = _resample_type_token(info)
        if token is None or token not in selected:
            continue
        groups.setdefault((info["source"], token), []).append(
            (os.path.join(dirpath, fn), info["area"]))
    return groups


def _assert_uniform_grid(paths, label):
    """Fail loud unless every raster shares one CRS, one cell size and one NoData value. Mosaicking
    a mix of CRS or cell size would silently resample or reproject; a mix of NoData values would let
    one raster's NoData collar be read as another's data (and vice versa), which silently corrupts
    the merge. A missing NoData is also rejected, since the empty collar would then be written as
    data. Returns (spatial_reference, cell_size, extent) of the union."""
    sr = None
    cell = None
    nodata = None
    xmin = ymin = xmax = ymax = None
    for p in paths:
        d = arcpy.Describe(p)
        r_sr = d.spatialReference
        r = arcpy.Raster(p)
        r_cell = r.meanCellWidth
        r_nd = r.noDataValue
        if r_nd is None:
            msg = ("{}: '{}' has no NoData value; its empty collar would merge as data. Rebuild it "
                   "with a NoData value (Tools 05/09).".format(label, os.path.basename(p)))
            _err(msg)
            raise ValueError(msg)
        if sr is None:
            sr, cell, nodata = r_sr, r_cell, r_nd
        else:
            if not _same_crs(sr, r_sr):
                msg = ("{}: '{}' is {} but the others are {}. Merge one CRS at a time."
                       .format(label, os.path.basename(p), _crs_label(r_sr), _crs_label(sr)))
                _err(msg)
                raise ValueError(msg)
            if abs(r_cell - cell) > 1e-6:
                msg = ("{}: '{}' has a {:g} m cell but the others have {:g} m. Resample them to a "
                       "common cell size first (Tool 06)."
                       .format(label, os.path.basename(p), r_cell, cell))
                _err(msg)
                raise ValueError(msg)
            if abs(r_nd - nodata) > 1e-6:
                msg = ("{}: '{}' has NoData {:g} but the others have {:g}. A mixed NoData corrupts "
                       "the merge, since one raster's NoData collar reads as another's data. "
                       "Rebuild the class rasters with a consistent NoData (Tool 09)."
                       .format(label, os.path.basename(p), r_nd, nodata))
                _err(msg)
                raise ValueError(msg)
        e = d.extent
        xmin = e.XMin if xmin is None else min(xmin, e.XMin)
        ymin = e.YMin if ymin is None else min(ymin, e.YMin)
        xmax = e.XMax if xmax is None else max(xmax, e.XMax)
        ymax = e.YMax if ymax is None else max(ymax, e.YMax)
    return sr, cell, (xmin, ymin, xmax, ymax)


class MergeClasses(object):
    def __init__(self):
        self.label = "10 - Merge Class Rasters"
        self.description = ("Merge the per area class rasters of each selected product into one "
                            "raster covering every area: the suitability masks (APT, APTCLS), the "
                            "aspect quadrants (ASPECT_DIR) and the Tool 05 _RCL factors. Only "
                            "class rasters are merged; a continuous surface is out of scope. "
                            "Areas of interest are scattered, so the merged grid spans their whole "
                            "bounding box and is almost all NoData: it is written with GDAL as a "
                            "sparse, LZW compressed BigTIFF, so the empty blocks cost nothing. "
                            "Without gdal (osgeo) it falls back to arcpy, which writes the grid "
                            "densely and can be very large and slow. Output "
                            "<Prefix>_<SOURCE>_<PRODUCT>.tif.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder (with the class rasters)", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM", "BOTH"]
        p_source.value = "DEM"

        p_products = arcpy.Parameter(
            displayName="Class products to merge", name="products",
            datatype="GPString", parameterType="Required", direction="Input", multiValue=True)
        p_products.filter.type = "ValueList"
        p_products.filter.list = class_products()
        p_products.value = ["APT", "APTCLS"]

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_prefix = arcpy.Parameter(
            displayName="Output name prefix", name="prefix",
            datatype="GPString", parameterType="Required", direction="Input")
        p_prefix.value = "Todas"

        p_zero_nodata = arcpy.Parameter(
            displayName="Treat the 0 class as NoData", name="zero_nodata",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_zero_nodata.value = False        # 0 (surveyed, not suitable) is a real class, not NoData

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_source, p_products, p_out, p_prefix, p_zero_nodata,
                p_overwrite]

    def isLicensed(self):
        # GDAL Warp, or core MosaicToNewRaster as the fallback. No extension.
        return True

    def updateParameters(self, parameters):
        # Offer only the class products that actually exist under the chosen folder.
        _fill_class_product_list(parameters[0], parameters[1], parameters[3])
        return

    def execute(self, parameters, messages):
        # The .pyt runs in-process, so env settings leak into the Pro session; restore on exit.
        prev_overwrite = arcpy.env.overwriteOutput
        prev_compression = arcpy.env.compression
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite
            arcpy.env.compression = prev_compression

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        source = parameters[2].valueAsText
        selected = set(str(s).upper() for s in (parameters[3].values or []))
        out_folder = parameters[4].valueAsText
        prefix = sanitize_name(parameters[5].valueAsText)
        zero_nodata = bool(parameters[6].value)
        overwrite_existing = bool(parameters[7].value)

        arcpy.env.overwriteOutput = overwrite_existing
        arcpy.env.compression = "LZW"
        if zero_nodata:
            _msg("The 0 class is declared NoData in the output. Note that 0 (surveyed, not "
                 "suitable) and NoData (outside the area) then look the same, so the suitable "
                 "share of an area can no longer be computed from this raster alone.")

        groups = _group_class_rasters(in_folder, recurse, source, selected)
        if not groups:
            msg = ("No class rasters of the selected products ({}) found under '{}'. Run Tools 5 "
                   "and 9 first, or point at the Resample folder with the 5 m rasters."
                   .format(", ".join(sorted(selected)), in_folder))
            _err(msg)
            raise ValueError(msg)
        if not os.path.isdir(out_folder):
            os.makedirs(out_folder)

        created = 0
        skipped = 0
        failed = []
        arcpy.SetProgressor("step", "Merging class rasters...", 0, len(groups), 1)
        for (src, token), items in sorted(groups.items()):
            arcpy.SetProgressorPosition()
            out_path = os.path.join(out_folder, "{}_{}_{}.tif".format(prefix, src, token))
            if os.path.exists(out_path) and not overwrite_existing:
                _msg("{} ({}): exists, skipping.".format(token, src))
                skipped += 1
                continue
            try:
                paths = sorted(p for p, _a in items)
                sr, cell, (xmin, ymin, xmax, ymax) = _assert_uniform_grid(
                    paths, "{} ({})".format(token, src))
                ncols = int(math.ceil((xmax - xmin) / cell))
                nrows = int(math.ceil((ymax - ymin) / cell))
                cells = float(ncols) * float(nrows)
                _msg("{} ({}, {}): {} raster(s), {:g} m cell, merged grid {} x {} = {:.3g} cells."
                     .format(token, src, _crs_label(sr), len(paths), cell, ncols, nrows, cells))
                if cells > 2e9:
                    _warn("{} ({}): the merged grid is {:.3g} cells because the areas are far "
                          "apart, and is almost all NoData. Sparse BigTIFF handles it, but a "
                          "denser writer or a finer cell size will not. Consider merging the 5 m "
                          "rasters, not the native ones.".format(token, src, cells))
                self._merge(paths, out_path, token, src, zero_nodata)
                # Fail loud on a silent drop: BuildVRT used to skip inputs whose CRS WKT was not
                # byte identical, shrinking the grid without an error. Verify the output covers the
                # expected union grid (targetAlignedPixels can shift it by up to one cell).
                chk = arcpy.Raster(out_path)
                if chk.width < ncols - 1 or chk.height < nrows - 1:
                    raise RuntimeError(
                        "{} ({}): merged grid is {} x {} but the inputs span {} x {}. Inputs were "
                        "dropped during the merge; the output is incomplete.".format(
                            token, src, chk.width, chk.height, ncols, nrows))
                del chk
                # gdal leaves no attribute table, which breaks the class symbology in Pro; add one.
                _build_class_rat(out_path)
                _msg("{} ({}): merged -> {} ({:.1f} MB)".format(
                    token, src, os.path.basename(out_path),
                    os.path.getsize(out_path) / (1024.0 * 1024.0)))
                created += 1
            except Exception as exc:
                _warn("{} ({}): merge failed: {}. Skipping this product.".format(token, src, exc))
                failed.append("{} ({})".format(token, src))

        arcpy.ResetProgressor()
        _msg("Done. Products: {}. Merged: {}. Skipped existing: {}. Failed: {}.".format(
            len(groups), created, skipped, len(failed)))
        # Fail loud: if any product failed, the tool result must be a failure, not SUCCESS.
        if failed:
            msg = "Merge failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return

    def _merge(self, paths, out_path, token, src, zero_nodata):
        """Write the merged raster with gdal.Warp onto one aligned grid, a sparse LZW BigTIFF, so
        the mostly empty country wide grid costs almost nothing. Without osgeo, fall back to arcpy,
        which writes every block.

        Warp, not BuildVRT: BuildVRT skips any input whose CRS WKT is not byte identical to the
        first input's, even when it is the same CRS, and only warns. On the per area rasters, whose
        WKT varies slightly, that silently dropped most areas and destroyed the merge. Warp
        tolerates the WKT variants, snaps every area onto one grid (targetAlignedPixels) and
        preserves every class exactly. Nearest keeps the values categorical; the sources' own
        NoData is passed as src and dst NoData, so the empty collar stays NoData and the grid is
        sparse.

        zero_nodata declares the 0 class as NoData, flagged on the OUTPUT after the merge (both
        engines), so the sources' own NoData is preserved. On scattered, same source data the areas
        do not overlap, so an overlap winner is moot in practice.
        """
        try:
            from osgeo import gdal
        except ImportError:
            gdal = None

        if gdal is None:
            _warn("{} ({}): gdal (osgeo) is not available, falling back to arcpy. The whole grid "
                  "is written densely, which for scattered areas is slow and can be very large, "
                  "and a BigTIFF above 4 GB is not guaranteed. Install gdal for the sparse path."
                  .format(token, src))
            self._merge_arcpy(paths, out_path, zero_nodata, token, src)
            return

        ref = gdal.Open(paths[0])
        nd = ref.GetRasterBand(1).GetNoDataValue()
        gt = ref.GetGeoTransform()
        xres, yres = abs(gt[1]), abs(gt[5])
        ref = None
        if nd is None:
            # Fail loud: without a NoData the empty collar would be written as data.
            raise RuntimeError("{} ({}): source '{}' has no NoData value; cannot merge without "
                               "painting the collar as data.".format(
                                   token, src, os.path.basename(paths[0])))

        opts = ["BIGTIFF=YES", "COMPRESS=LZW", "TILED=YES", "SPARSE_OK=TRUE"]
        # gdal returns None on failure instead of raising; surface it.
        out_ds = gdal.Warp(out_path, paths, xRes=xres, yRes=yres, targetAlignedPixels=True,
                           resampleAlg="near", srcNodata=nd, dstNodata=nd, multithread=True,
                           creationOptions=opts)
        if out_ds is None:
            raise RuntimeError("gdal.Warp returned None")
        if zero_nodata:
            out_ds.GetRasterBand(1).SetNoDataValue(0)
        out_ds = None                              # flush and close

    def _merge_arcpy(self, paths, out_path, zero_nodata, token, src):
        """Fallback merge with core MosaicToNewRaster. Pixel type and band count come from the
        first input. LAST matches the GDAL path (later raster in the sorted list wins an overlap),
        so the two engines agree; on same source scattered data the areas do not overlap anyway."""
        first = arcpy.Raster(paths[0])
        arcpy.management.MosaicToNewRaster(
            input_rasters=paths,
            output_location=os.path.dirname(out_path),
            raster_dataset_name_with_extension=os.path.basename(out_path),
            coordinate_system_for_the_raster=arcpy.Describe(paths[0]).spatialReference,
            pixel_type=_MOSAIC_PIXEL_TYPES.get(first.pixelType, "16_BIT_SIGNED"),
            number_of_bands=1,
            mosaic_method="LAST")
        if zero_nodata:
            arcpy.management.SetRasterProperties(out_path, nodata=[[1, 0]])


class VectorizeClasses(object):
    def __init__(self):
        self.label = "11 - Vectorize Class Rasters"
        self.description = ("Convert the class rasters to polygons, with every area merged into "
                            "one shapefile per product. Scattered areas are cheap as polygons, and "
                            "the area of each polygon comes out computed, which is what a merged "
                            "raster cannot give directly. Each polygon carries the area name, the "
                            "class value and its area in square meters. Every polygon is kept, "
                            "including the not suitable 0, so the suitable share of each area can "
                            "be computed. Output <Prefix>_<SOURCE>_<PRODUCT>.shp. Point it at the "
                            "5 m rasters: vectorizing a 50 cm raster makes millions of stair step "
                            "polygons.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = arcpy.Parameter(
            displayName="Results folder (with the class rasters)", name="in_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_recurse = arcpy.Parameter(
            displayName="Recurse subfolders", name="recurse_subfolders",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_recurse.value = True

        p_source = arcpy.Parameter(
            displayName="Source", name="source_filter",
            datatype="GPString", parameterType="Required", direction="Input")
        p_source.filter.type = "ValueList"
        p_source.filter.list = ["DEM", "DSM", "BOTH"]
        p_source.value = "DEM"

        p_products = arcpy.Parameter(
            displayName="Class products to vectorize", name="products",
            datatype="GPString", parameterType="Required", direction="Input", multiValue=True)
        p_products.filter.type = "ValueList"
        p_products.filter.list = class_products()
        p_products.value = ["APT", "APTCLS", "ASPECT_DIR"]

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p_prefix = arcpy.Parameter(
            displayName="Output name prefix", name="prefix",
            datatype="GPString", parameterType="Required", direction="Input")
        p_prefix.value = "Todas"

        p_simplify = arcpy.Parameter(
            displayName="Simplify polygons", name="simplify",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_simplify.value = False           # off: the outline follows the cell edges exactly

        p_drop_zero = arcpy.Parameter(
            displayName="Drop the 0 class polygons", name="drop_zero",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_drop_zero.value = False          # 0 (surveyed, not suitable) is a real class

        p_overwrite = arcpy.Parameter(
            displayName="Overwrite existing outputs", name="overwrite_existing",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_overwrite.value = False

        return [p_in, p_recurse, p_source, p_products, p_out, p_prefix, p_simplify, p_drop_zero,
                p_overwrite]

    def isLicensed(self):
        # RasterToPolygon and CalculateGeometryAttributes are core. No extension.
        return True

    def updateParameters(self, parameters):
        # Offer only the class products that actually exist under the chosen folder.
        _fill_class_product_list(parameters[0], parameters[1], parameters[3])
        return

    def execute(self, parameters, messages):
        prev_overwrite = arcpy.env.overwriteOutput
        try:
            return self._run(parameters, messages)
        finally:
            arcpy.env.overwriteOutput = prev_overwrite

    def _run(self, parameters, messages):
        in_folder = parameters[0].valueAsText
        recurse = bool(parameters[1].value)
        source = parameters[2].valueAsText
        selected = set(str(s).upper() for s in (parameters[3].values or []))
        out_folder = parameters[4].valueAsText
        prefix = sanitize_name(parameters[5].valueAsText)
        simplify = "SIMPLIFY" if bool(parameters[6].value) else "NO_SIMPLIFY"
        drop_zero = bool(parameters[7].value)
        overwrite_existing = bool(parameters[8].value)

        arcpy.env.overwriteOutput = True     # the in_memory scratch is rewritten per raster
        if drop_zero:
            _msg("Dropping the 0 class polygons. Only the suitable area is kept, so the total "
                 "area of each mine is no longer in the output and the suitable share cannot be "
                 "computed from it alone.")

        groups = _group_class_rasters(in_folder, recurse, source, selected)
        if not groups:
            msg = ("No class rasters of the selected products ({}) found under '{}'. Run Tools 5 "
                   "and 9 first, or point at the Resample folder with the 5 m rasters."
                   .format(", ".join(sorted(selected)), in_folder))
            _err(msg)
            raise ValueError(msg)
        if not os.path.isdir(out_folder):
            os.makedirs(out_folder)

        created = 0
        skipped = 0
        failed = []
        arcpy.SetProgressor("step", "Vectorizing class rasters...", 0, len(groups), 1)
        for (src, token), items in sorted(groups.items()):
            arcpy.SetProgressorPosition()
            out_shp = os.path.join(out_folder, "{}_{}_{}.shp".format(prefix, src, token))
            if arcpy.Exists(out_shp) and not overwrite_existing:
                _msg("{} ({}): exists, skipping.".format(token, src))
                skipped += 1
                continue
            try:
                n = self._vectorize_group(sorted(items), out_shp, simplify, drop_zero, token, src)
                _msg("{} ({}): {} polygon(s) from {} area(s) -> {}".format(
                    token, src, n, len(items), os.path.basename(out_shp)))
                created += 1
            except Exception as exc:
                _warn("{} ({}): vectorize failed: {}. Skipping this product."
                      .format(token, src, exc))
                failed.append("{} ({})".format(token, src))

        arcpy.ResetProgressor()
        _msg("Done. Products: {}. Shapefiles created: {}. Skipped existing: {}. Failed: {}.".format(
            len(groups), created, skipped, len(failed)))
        if failed:
            msg = "Vectorize failed for: " + ", ".join(failed)
            _err(msg)
            raise RuntimeError(msg)
        return

    def _vectorize_group(self, items, out_shp, simplify, drop_zero, token, src):
        """Polygonize every area's raster and append them into one shapefile, then compute the
        polygon areas. drop_zero skips the not suitable class. Returns the polygon count."""
        # Fail loud when the same area is present more than once (for example the native Reclass
        # raster and its Resample copy under one root). Vectorizing both would put that ground in
        # the shapefile twice and double count AREA_M2, which is exactly what this tool is for.
        seen = {}
        for path, area in items:
            seen.setdefault(area, []).append(path)
        dups = {a: p for a, p in seen.items() if len(p) > 1}
        if dups:
            detail = "; ".join("'{}' x{}".format(a, len(p)) for a, p in sorted(dups.items()))
            msg = ("{} ({}): the same area appears more than once, so AREA_M2 would double count: "
                   "{}. Point at a single tree (for example only the Resample folder), not a root "
                   "that holds both the native and the resampled copy.".format(token, src, detail))
            _err(msg)
            raise ValueError(msg)
        # One projected CRS across the group: a mixed CRS would append geometries from different
        # systems into one shapefile. Unlike the merge tool, a mixed cell size is fine here, since
        # each polygon area is computed from its own geometry, not from a shared grid.
        sr = arcpy.Describe(items[0][0]).spatialReference
        if sr is None or sr.type != "Projected":
            msg = ("{} ({}): the rasters must be in a projected CRS to compute areas in meters; "
                   "found {}.".format(token, src, _crs_label(sr)))
            _err(msg)
            raise ValueError(msg)
        for path, area in items[1:]:
            if not _same_crs(sr, arcpy.Describe(path).spatialReference):
                msg = ("{} ({}): '{}' is in a different CRS than the others. Vectorize one CRS at "
                       "a time.".format(token, src, os.path.basename(path)))
                _err(msg)
                raise ValueError(msg)

        if arcpy.Exists(out_shp):
            arcpy.management.Delete(out_shp)
        arcpy.management.CreateFeatureclass(
            os.path.dirname(out_shp), os.path.basename(out_shp), "POLYGON", spatial_reference=sr)
        # Shapefile field names are limited to 10 characters.
        arcpy.management.AddField(out_shp, "AREA_NAME", "TEXT", field_length=MAX_NAME_LEN)
        arcpy.management.AddField(out_shp, "GRIDCODE", "LONG")

        tmp = "in_memory/_vec_poly"
        for path, area in items:
            if not is_integer_pixel_type(arcpy.Raster(path).pixelType):
                msg = ("{} ({}): '{}' is not an integer raster, so it cannot be polygonized. "
                       "Vectorize the class rasters, not a continuous surface."
                       .format(token, src, os.path.basename(path)))
                _err(msg)
                raise ValueError(msg)
            if arcpy.Exists(tmp):
                arcpy.management.Delete(tmp)
            arcpy.conversion.RasterToPolygon(path, tmp, simplify, "Value")
            # Map the RasterToPolygon schema onto ours: gridcode carries the class value, and the
            # area name is constant per input raster.
            with arcpy.da.SearchCursor(tmp, ["SHAPE@", "gridcode"]) as reader:
                with arcpy.da.InsertCursor(out_shp, ["SHAPE@", "AREA_NAME", "GRIDCODE"]) as writer:
                    for shape, gridcode in reader:
                        if drop_zero and gridcode == 0:
                            continue
                        writer.insertRow((shape, area, gridcode))
        if arcpy.Exists(tmp):
            arcpy.management.Delete(tmp)

        arcpy.management.AddField(out_shp, "AREA_M2", "DOUBLE")
        arcpy.management.CalculateGeometryAttributes(
            out_shp, [["AREA_M2", "AREA"]], area_unit="SQUARE_METERS")
        return int(arcpy.management.GetCount(out_shp)[0])


# ===========================================================================
# Self tests (pure functions; numpy tests run only if numpy is importable).
# Run: python LidarTerrainToolbox.pyt
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
    check("spaces to underscore", sanitize_name("Area Norte") == "Area_Norte")
    check("hyphen separator to underscore", sanitize_name("Sao-Chico") == "Sao_Chico")
    check("leading digit kept for file output", sanitize_name("2 Areas") == "2_Areas")
    check("leading digit prefixed for geodatabase",
          sanitize_name("2 Areas", to_geodatabase=True).startswith("M_"))
    check("sheet code merged", sanitize_name("1-A Valença") == "1A_Valenca")
    check("sheet code merged (two digit sheet)",
          sanitize_name("10-A Celorico de Basto") == "10A_Celorico_de_Basto")
    check("multi sheet code merged",
          sanitize_name("25-CD 29-A Rosmaninhal Segura Retorta") == "25CD_29A_Rosmaninhal_Segura_Retorta")
    check("number running into a word is not merged",
          sanitize_name("Area 16 Artigo 1452 EDM") == "Area_16_Artigo_1452_EDM")
    check("sheet code merged then prefixed for geodatabase",
          sanitize_name("1-A Valença", to_geodatabase=True) == "M_1A_Valenca")
    check("collapses repeated underscores", "__" not in sanitize_name("Area   Norte"))
    check("truncated to limit", len(sanitize_name("A" * 100)) <= MAX_NAME_LEN)
    check_raises("empty result raises", lambda: sanitize_name("***"))
    check_raises("None raises", lambda: sanitize_name(None))

    print("dedupe_name")
    used = {"AreaA", "AreaA_2"}
    check("collision appends next free suffix", dedupe_name("AreaA", used) == "AreaA_3")
    check("no collision unchanged", dedupe_name("AreaB", used) == "AreaB")

    print("find_sanitize_collisions")
    _col = find_sanitize_collisions(["Covas-1", "Covas 1", "Lousal"])
    check("distinct raws colliding are reported",
          _col == {"Covas_1": ["Covas 1", "Covas-1"]})
    check("no collision returns empty", find_sanitize_collisions(["Lousal", "Jales"]) == {})
    check("same raw repeated is not a collision",
          find_sanitize_collisions(["Jales", "Jales"]) == {})

    print("_datums_coincident")
    check("etrs89 and wgs84 are coincident (no datum-shift warning)",
          _datums_coincident("D_ETRS_1989", "D_WGS_1984"))
    check("wgs84 and etrs89 are coincident either way",
          _datums_coincident("D_WGS_1984", "D_ETRS_1989"))
    check("datum 73 and etrs89 are not coincident",
          not _datums_coincident("D_Datum_73", "D_ETRS_1989"))
    check("lisboa and wgs84 are not coincident",
          not _datums_coincident("D_Lisbon", "D_WGS_1984"))
    check("unknown datum name is not coincident",
          not _datums_coincident(None, "D_WGS_1984"))

    print("build_output_name / parse_source_and_product round trip")
    cases = [
        ("Sao_Domingos", "DEM", "SLOPE", False),
        ("Sao_Domingos", "DEM", "SLOPE", True),
        ("Area_Norte", "DSM", "ASPECT", False),
        ("AreaA", "DEM", None, False),               # base mosaic
        ("AreaA", "DEM", "SOLAR", False),
        ("AreaA", "DEM", "SLOPEP", False),           # slope percent
        ("AreaA", "DEM", "SOLARUNI", False),         # solar, uniform sky model
        ("AreaA", "DSM", "SOLAROVCDIR", False),      # solar, overcast model, direct aux
        ("AreaA", "DEM", "CONT", False),             # contour lines (Tool 7)
        ("Covas", "DEM", "APT", False),              # suitability mask (Tool 9)
        ("Covas", "DEM", "APTCLS", False),           # suitability graded by solar class (Tool 9)
        ("Covas", "DEM", "ASPECT_DIR", False),       # product token that contains an underscore
        ("Sao_Domingos", "DSM", "ASPECT_DIR", False),
    ]
    for area, source, product, reclass in cases:
        name = build_output_name(area, source, product, reclass)
        p = parse_source_and_product(name + ".tif")
        check("round trip {}".format(name),
              p["area"] == area and p["source"] == source
              and p["product"] == product and p["reclass"] == reclass)

    check("area with underscore not misparsed",
          parse_source_and_product("Sao_Domingos_DEM_SLOPE.tif")["area"] == "Sao_Domingos")
    # ASPECT_DIR is a product token that itself contains an underscore, so the parser has to try
    # the last two tokens joined before the last one alone. It must not eat ASPECT_RCL or ASPECT.
    _dir = parse_source_and_product("Covas_DEM_ASPECT_DIR.tif")
    check("two token product parsed",
          _dir["area"] == "Covas" and _dir["source"] == "DEM"
          and _dir["product"] == "ASPECT_DIR" and not _dir["reclass"])
    check("two token product resample token",
          _resample_type_token(parse_source_and_product("Covas_DEM_ASPECT_DIR.tif")) == "ASPECT_DIR")
    check("plain aspect still parses",
          parse_source_and_product("Covas_DEM_ASPECT.tif")["product"] == "ASPECT")
    _rcl = parse_source_and_product("Covas_DEM_ASPECT_RCL.tif")
    check("aspect reclass still parses",
          _rcl["product"] == "ASPECT" and _rcl["reclass"] and _rcl["area"] == "Covas")
    check("area ending in a product word not misparsed",
          parse_source_and_product("Vale_ASPECT_DEM_SLOPE.tif")["area"] == "Vale_ASPECT")

    print("_walk_tifs / _find_base_mosaics")
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="lidar_walk_")
    try:
        def _touch(*parts):
            p = os.path.join(tmpdir, *parts)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        _touch("AreaA_DEM.tif")
        _touch("AreaA_DEM_SLOPE.tif")
        _touch("notes.txt")                                  # ignored, not a .tif
        _touch(RESAMPLE_DIRNAME, "AreaA", "AreaA_DEM.tif")   # resampled copy
        _touch(RECLASS_DIRNAME, "AreaA_DEM_SLOPE_RCL.tif")   # reclassified
        _touch("cont", "AreaA_DEM_CONT_placeholder.tif")     # in a would-be Contours dir
        all_found = {fn for _d, fn in _walk_tifs(tmpdir, True)}
        check("_walk_tifs finds every tif, skips non tif",
              all_found == {"AreaA_DEM.tif", "AreaA_DEM_SLOPE.tif",
                            "AreaA_DEM_SLOPE_RCL.tif", "AreaA_DEM_CONT_placeholder.tif"})
        skipped = {fn for _d, fn in _walk_tifs(tmpdir, True, (RESAMPLE_DIRNAME,))}
        check("_walk_tifs prunes the skip dir (case insensitive)",
              "AreaA_DEM_SLOPE_RCL.tif" in skipped and len(skipped) == 4
              and all("resample" not in _d.lower() for _d, _fn in
                      _walk_tifs(tmpdir, True, (RESAMPLE_DIRNAME.upper(),))))
        check("_walk_tifs non recurse stays at the root",
              {fn for _d, fn in _walk_tifs(tmpdir, False)}
              == {"AreaA_DEM.tif", "AreaA_DEM_SLOPE.tif"})
        base = _find_base_mosaics(tmpdir, True, SOURCES, (RESAMPLE_DIRNAME,))
        check("_find_base_mosaics returns the base mosaic only (no surface, no resample copy)",
              [(a, s) for _p, a, s in base] == [("AreaA", "DEM")])
        check_raises("_find_base_mosaics fails loud when none match",
                     lambda: _find_base_mosaics(tmpdir, True, ("DSM",)))
        # _present_type_tokens: only the types with a real raster under the folder, so the dialog
        # offers what exists. The Resample copy is skipped, the SLOPE_RCL in Reclass is included.
        present = _present_type_tokens(tmpdir, True, (RESAMPLE_DIRNAME,), _resample_all_types())
        check("_present_type_tokens sees the base, surface and reclass, not the resample copy",
              present == {"DEM", "SLOPE", "SLOPE_RCL"})
        check("_present_type_tokens is empty for a missing folder",
              _present_type_tokens(os.path.join(tmpdir, "nope"), True, (), _resample_all_types())
              == set())
        check("_resample_all_types has no CONT and carries the _RCL variants",
              "CONT" not in _resample_all_types() and "SOLARUNI_RCL" in _resample_all_types())
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("class_products / is_integer_pixel_type")
    _cp = class_products()
    check("class products cover the suitability and aspect quadrants",
          "APT" in _cp and "APTCLS" in _cp and "ASPECT_DIR" in _cp)
    check("class products cover every reclassified factor",
          all(p + "_RCL" in _cp for p in RECLASSIFIED_PRODUCTS))
    check("class products exclude the continuous surfaces",
          "SLOPE" not in _cp and "SLOPEP" not in _cp and "SOLARUNI" not in _cp)
    check("integer pixel types accepted",
          all(is_integer_pixel_type(t) for t in ("U8", "S16", "U16", "S32")))
    check("float pixel types rejected",
          not any(is_integer_pixel_type(t) for t in ("F32", "F64")))
    check("empty pixel type rejected", not is_integer_pixel_type(None))
    check("every mosaic pixel type maps to a known wording",
          all(v.endswith(("BIT", "_SIGNED", "_UNSIGNED", "_FLOAT")) or v == "64_BIT"
              for v in _MOSAIC_PIXEL_TYPES.values()))
    check("unknown name yields source None",
          parse_source_and_product("random_file.tif")["source"] is None)
    check("area ending in mixed case token kept (case sensitive parse)",
          parse_source_and_product("Vale_Dem.tif")["area"] == "Vale_Dem"
          and parse_source_and_product("Vale_Dem.tif")["source"] is None)
    check("lowercase tokens not recognized",
          parse_source_and_product("AreaA_dem_slope.tif")["source"] is None)
    check("empty name yields area None", parse_source_and_product("")["area"] is None)
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
    # The fixed suitability schemes are the one place to edit the classes; validate them so a
    # typo (a gap, an overlap between different ids) fails the tests instead of silently making
    # 9999 holes at run time.
    check("ASPECT_DIR scheme is a valid tiling",
          validate_value_table(ASPECT_DIR_CLASSES) is not None)
    check("ASPECT_RCL scheme is a valid tiling",
          validate_value_table(ASPECT_RCL_CLASSES) is not None)
    check("SLOPE_RCL scheme is a valid tiling",
          validate_value_table(SLOPE_RCL_CLASSES) is not None)
    check("SOLAR_RCL scheme is a valid tiling",
          validate_value_table(SOLAR_RCL_CLASSES) is not None)

    print("build_area_groups")
    groups = build_area_groups([(3, "Cortes Pereira"), (4, "Cortes Pereira"), (0, "Alcaria Queimada")])
    check("merges same area across fids",
          ("Cortes_Pereira", [3, 4]) in groups and ("Alcaria_Queimada", [0]) in groups)
    check("ordered by smallest fid", groups[0][0] == "Alcaria_Queimada")
    g2 = build_area_groups([(0, "São João"), (1, "Sao Joao")])
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

    print("build_clusters")
    check("two overlapping merge, third separate",
          build_clusters([1, 2, 3], [(1, 2)]) == [[1, 2], [3]])
    check("transitive chain merges",
          build_clusters([1, 2, 3, 4], [(1, 2), (2, 3)]) == [[1, 2, 3], [4]])
    check("no pairs all singletons",
          build_clusters([3, 1, 2], []) == [[1], [2], [3]])
    check("ordered by smallest key",
          build_clusters([5, 9, 2], [(5, 9)]) == [[2], [5, 9]])
    check("pair with unknown key ignored",
          build_clusters([1, 2], [(1, 99)]) == [[1], [2]])

    print("_parse_tile_resolution")
    check("2m token from a DGT name",
          _parse_tile_resolution("MDT-2m-152470-07-2025_v01.tif") == "2m")
    check("50cm token from a DGT name",
          _parse_tile_resolution("MDS-50cm-152470-07-2025_v01.tif") == "50cm")
    check("product code is case insensitive, token kept as written",
          _parse_tile_resolution("mdt-2M-1-2-3.tif") == "2M")
    check("dotted token kept",
          _parse_tile_resolution("MDT-0.5m-1-2-3.tif") == "0.5m")
    check("non DGT name yields None",
          _parse_tile_resolution("hillshade.tif") is None)
    check("too few segments yields None",
          _parse_tile_resolution("MDT-2m.tif") is None)
    check("empty name yields None",
          _parse_tile_resolution("") is None)

    print("_choose_resolution")
    check("single token auto-detected",
          _choose_resolution({"2m"}, None) == "2m")
    check("no tokens means no filter",
          _choose_resolution(set(), None) is None)
    check("explicit request wins and is lowercased",
          _choose_resolution({"2m", "50cm"}, "2M") == "2m")
    check_raises("auto mode raises on mixed resolutions",
                 lambda: _choose_resolution({"2m", "50cm"}, None))

    print("_resample_type_token")
    check("base mosaic source token",
          _resample_type_token(parse_source_and_product("AreaA_DEM.tif")) == "DEM")
    check("surface product token",
          _resample_type_token(parse_source_and_product("AreaA_DEM_SLOPE.tif")) == "SLOPE")
    check("reclassified token gets _RCL",
          _resample_type_token(parse_source_and_product("AreaA_DEM_ASPECT_RCL.tif")) == "ASPECT_RCL")
    check("solar product token",
          _resample_type_token(parse_source_and_product("AreaA_DEM_SOLAR.tif")) == "SOLAR")
    check("non conforming name yields None",
          _resample_type_token(parse_source_and_product("random_file.tif")) is None)

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
        check("aspect_dir scheme",
              reclassify_array(_np.array([[0.0, 45.0, 180.0, 350.0, -1.0]]),
                               ASPECT_DIR_CLASSES, 9999, flat_value=-1,
                               flat_class=ASPECT_DIR_FLAT).tolist() == [[1, 2, 5, 1, 9]])
        check("aspect_rcl scheme",
              reclassify_array(_np.array([[0.0, 60.0, 120.0, 180.0, 340.0, -1.0]]),
                               ASPECT_RCL_CLASSES, 9999, flat_value=-1,
                               flat_class=ASPECT_RCL_FLAT).tolist() == [[1, 2, 3, 4, 1, 4]])
        check("slope_rcl scheme (20 percent equals 11.31 deg)",
              reclassify_array(_np.array([[10.0, 12.0]]), SLOPE_RCL_CLASSES, 9999).tolist()
              == [[1, 0]])
        check("solar_rcl scheme",
              reclassify_array(_np.array([[1000.0, 1300.0, 1500.0, 2100.0]]),
                               SOLAR_RCL_CLASSES, 9999).tolist() == [[1, 2, 3, 6]])

    print("DGT download helpers")
    check("asset extension geotiff",
          _asset_extension("image/tiff; application=geotiff") == ".tif")
    check("asset extension laz", _asset_extension("application/vnd.laszip") == ".laz")
    check("asset extension unknown", _asset_extension("application/json") == ".bin")
    check("geotiff head is a valid asset", _looks_like_asset(b"II*\x00\x08\x00"))
    check("laz head is a valid asset", _looks_like_asset(b"LASF\x00\x00"))
    check("html login page is not an asset", not _looks_like_asset(b"<!DOCTYPE html>"))
    check("small bbox is not split", len(divide_bbox((-8.0, 39.0, -7.99, 39.01))) == 1)
    check("large bbox is split into a grid", len(divide_bbox((-8.0, 39.0, -7.0, 39.5))) > 1)
    _action, _fields = parse_login_form(
        '<form id="kc-form-login" action="https://x/auth">'
        '<input name="username" value=""><input type="hidden" name="tab_id" value="abc"></form>')
    check("keycloak form action", _action == "https://x/auth")
    check("keycloak form hidden field", _fields.get("tab_id") == "abc")
    import tempfile
    _cred = os.path.join(tempfile.gettempdir(), "lt_test_creds.json")
    write_dgt_credentials(_cred, "user1", "pw1")
    check("credentials round trip", read_dgt_credentials(_cred) == ("user1", "pw1"))
    check("missing credentials file", read_dgt_credentials(_cred + ".nope") == (None, None))
    try:
        os.remove(_cred)
    except OSError:
        pass

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
