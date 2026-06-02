# -*- coding: utf-8 -*-
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
PRODUCTS = ("SLOPE", "ASPECT", "HILLSHADE", "PROFC", "PLANC")
RECLASS_SUFFIX = "RCL"
MAX_NAME_LEN = 40                                    # margin for suffixes like _DEM_ASPECT_RCL


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


_LINEAR_UNITS_TO_M = {
    "millimeters": 0.001, "millimeter": 0.001,
    "centimeters": 0.01, "centimeter": 0.01,
    "decimeters": 0.1, "decimeter": 0.1,
    "meters": 1.0, "meter": 1.0,
    "kilometers": 1000.0, "kilometer": 1000.0,
    "feet": 0.3048, "foot": 0.3048,
    "yards": 0.9144, "yard": 0.9144,
    "miles": 1609.344, "mile": 1609.344,
    "nauticalmiles": 1852.0, "nauticalmile": 1852.0,
}


def linear_unit_to_meters(linear_unit):
    """Convert an ArcGIS Linear Unit string like "5 Kilometers" to meters.

    The project is metric, so buffers are computed in meters. Fails loud on an
    unknown or non linear unit (for example DecimalDegrees), since a degree based
    buffer would be wrong here.
    """
    if not linear_unit:
        raise ValueError("Empty buffer distance.")
    parts = str(linear_unit).split()
    if len(parts) != 2:
        raise ValueError("Buffer distance must be 'value unit', got: {!r}".format(linear_unit))
    value_str, unit = parts
    try:
        value = float(value_str.replace(",", "."))
    except ValueError:
        raise ValueError("Buffer distance value is not numeric: {!r}".format(value_str))
    factor = _LINEAR_UNITS_TO_M.get(unit.lower())
    if factor is None:
        raise ValueError("Unsupported buffer unit '{}'. Use a linear unit (meters, kilometers), not degrees.".format(unit))
    return value * factor


# ===========================================================================
# Toolbox
# ===========================================================================

class Toolbox(object):
    def __init__(self):
        self.label = "Mining Terrain Factor Toolbox"
        self.alias = "MiningTerrain"
        # Tools are registered incrementally. Passo 0 ships the shared helpers and
        # the self tests only. Each Tool class is added here as it is built and
        # validated:
        #   BuildMosaicsByPolygon  (category "Mosaicking")       -> implemented
        #   DeriveSurfaces         (category "Surfaces")
        #   ReclassifyFactor       (category "Reclassification")
        # A future fifth tool for solar irradiation (Area Solar Radiation) would be
        # registered here too. Out of scope now, no functional stub.
        self.tools = [BuildMosaicsByPolygon]


# ===========================================================================
# Tools
# ===========================================================================
#   BuildMosaicsByPolygon  (Mosaicking)        -> implemented below
#   DeriveSurfaces         (Surfaces)          -> after Tool 1 is validated
#   ReclassifyFactor       (Reclassification)  -> after Tool 2 (imports numpy lazily)
# A future fifth tool for solar irradiation (Area Solar Radiation) would go here.


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


def _build_tile_index(folder):
    """List .tif tiles in `folder` and return (footprints, tile_sr).

    footprints is a list of (tile_path, extent_polygon). For a regular tile grid
    the rectangular extent is the exact footprint (spec 4.2). Fails loud if the
    folder has no rasters, if the tiles are not in a projected CRS (geographic or
    undefined are invalid for slope and aspect downstream), or if the tiles are not
    all in the same CRS.
    """
    prev_ws = arcpy.env.workspace
    try:
        arcpy.env.workspace = folder
        names = arcpy.ListRasters("*", "TIF") or []
    finally:
        arcpy.env.workspace = prev_ws

    if not names:
        msg = "No .tif tiles found in '{}'.".format(folder)
        _err(msg)
        raise ValueError(msg)

    footprints = []
    ref_sr = None
    for name in names:
        path = os.path.join(folder, name)
        desc = arcpy.Describe(path)
        sr = desc.spatialReference
        if ref_sr is None:
            ref_sr = sr
            if sr is None or sr.type != "Projected":
                msg = ("Tiles in '{}' have a geographic or undefined CRS ('{}'). A projected "
                       "CRS in meters is required (project CRS EPSG:{}). Reproject the tiles "
                       "first.").format(folder, sr.name if sr else "Unknown", PROJECT_EPSG)
                _err(msg)
                raise ValueError(msg)
            if not sr.factoryCode:
                _warn("Tiles in '{}' use a projected CRS without an EPSG/WKID ('{}'). "
                      "Project CRS is EPSG:{}.".format(folder, sr.name, PROJECT_EPSG))
            elif int(sr.factoryCode) != PROJECT_EPSG:
                _warn("Tiles in '{}' are EPSG:{} ('{}'), not the project EPSG:{}. Proceeding.".format(
                    folder, sr.factoryCode, sr.name, PROJECT_EPSG))
        elif not _same_crs(ref_sr, sr):
            msg = ("Tiles in '{}' are not all in the same CRS ({} vs {}). All tiles must "
                   "share one projected CRS.").format(folder, _crs_label(ref_sr), _crs_label(sr))
            _err(msg)
            raise ValueError(msg)
        footprints.append((path, desc.extent.polygon))
    return footprints, ref_sr


class BuildMosaicsByPolygon(object):
    def __init__(self):
        self.label = "Build Mosaics By Polygon"
        self.description = ("For each mining polygon, buffer it, select the LiDAR tiles "
                            "that intersect, and write one mosaic per mina, in batch, for "
                            "the DEM and/or DSM tile folders.")
        self.category = "Mosaicking"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_polys = arcpy.Parameter(
            displayName="Mining polygons", name="in_polygons",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_polys.filter.list = ["Polygon"]

        p_field = arcpy.Parameter(
            displayName="Mina name field", name="mina_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_field.parameterDependencies = [p_polys.name]
        p_field.filter.list = ["Text", "Short", "Long"]

        p_dem = arcpy.Parameter(
            displayName="DEM tiles folder", name="dem_tiles_folder",
            datatype="DEFolder", parameterType="Optional", direction="Input")

        p_dsm = arcpy.Parameter(
            displayName="DSM tiles folder", name="dsm_tiles_folder",
            datatype="DEFolder", parameterType="Optional", direction="Input")

        p_buffer = arcpy.Parameter(
            displayName="Buffer distance", name="buffer_distance",
            datatype="GPLinearUnit", parameterType="Required", direction="Input")
        p_buffer.value = "5 Kilometers"

        p_out = arcpy.Parameter(
            displayName="Output folder", name="out_folder",
            datatype="DEFolder", parameterType="Required", direction="Output")

        p_struct = arcpy.Parameter(
            displayName="Output structure", name="output_structure",
            datatype="GPString", parameterType="Required", direction="Input")
        p_struct.filter.type = "ValueList"
        p_struct.filter.list = ["per_mina_subfolder", "flat"]
        p_struct.value = "per_mina_subfolder"

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

        return [p_polys, p_field, p_dem, p_dsm, p_buffer, p_out,
                p_struct, p_pixel, p_method, p_overwrite]

    def isLicensed(self):
        # Tool 1 uses core Mosaic To New Raster, no Spatial Analyst needed (spec 4.1).
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        if not parameters[2].valueAsText and not parameters[3].valueAsText:
            parameters[2].setErrorMessage("Provide at least one tiles folder (DEM or DSM).")
            parameters[3].setErrorMessage("Provide at least one tiles folder (DEM or DSM).")
        return

    def execute(self, parameters, messages):
        in_polygons = parameters[0].valueAsText
        mina_field = parameters[1].valueAsText
        dem_folder = parameters[2].valueAsText
        dsm_folder = parameters[3].valueAsText
        buffer_distance = parameters[4].valueAsText
        out_folder = parameters[5].valueAsText
        output_structure = parameters[6].valueAsText
        pixel_type = parameters[7].valueAsText
        mosaic_method = parameters[8].valueAsText
        overwrite_existing = bool(parameters[9].value)

        arcpy.env.overwriteOutput = overwrite_existing

        if pixel_type != "32_BIT_FLOAT":
            _warn("pixel_type is {}, but DGT LiDAR is floating point. Non float types "
                  "lose elevation precision.".format(pixel_type))

        buffer_m = linear_unit_to_meters(buffer_distance)

        sources = []
        if dem_folder:
            sources.append(("DEM", dem_folder))
        if dsm_folder:
            sources.append(("DSM", dsm_folder))
        if not sources:
            msg = "Provide at least one tiles folder (DEM or DSM)."
            _err(msg)
            raise ValueError(msg)

        polys_sr = arcpy.Describe(in_polygons).spatialReference
        if polys_sr is None or polys_sr.name in (None, "", "Unknown"):
            msg = "Mining polygons have no spatial reference. Define their CRS first."
            _err(msg)
            raise ValueError(msg)

        # Read polygons once and resolve sanitized, collision free mina names once,
        # so the same mina gets the same name across DEM and DSM (spec 3.7).
        features = []          # list of (final_mina, geometry)
        used = set()
        n_collisions = 0
        with arcpy.da.SearchCursor(in_polygons, ["SHAPE@", mina_field]) as cursor:
            for shape, raw in cursor:
                if shape is None:
                    _warn("A feature has null geometry. Skipping it.")
                    continue
                base = sanitize_name(raw)
                final = dedupe_name(base, used)
                if final != base:
                    n_collisions += 1
                    _warn("Name collision: '{}' already used, using '{}' instead.".format(base, final))
                used.add(final)
                features.append((final, shape))

        if not features:
            msg = "No usable polygons found in '{}'.".format(in_polygons)
            _err(msg)
            raise ValueError(msg)

        total_minas = len(features)
        no_coverage = []       # list of (mina, source)

        for source, folder in sources:
            footprints, tile_sr = _build_tile_index(folder)
            _msg("Source {}: {} tiles, CRS {}.".format(source, len(footprints), _crs_label(tile_sr)))

            # CRS policy: tiles are the reference. If the polygons differ, reproject
            # the polygon geometries (vector, lossless), never the tiles.
            need_proj = not _same_crs(polys_sr, tile_sr)
            transform = None
            if need_proj:
                candidates = arcpy.ListTransformations(polys_sr, tile_sr)
                transform = candidates[0] if candidates else None
                _warn("Polygons CRS ({}) differs from tiles CRS ({}). Reprojecting "
                      "polygons{}.".format(
                          _crs_label(polys_sr), _crs_label(tile_sr),
                          " using transformation '{}'".format(transform) if transform
                          else " (no datum transformation applied)"))

            arcpy.SetProgressor("step", "Mosaicking {} tiles per mina...".format(source),
                                0, total_minas, 1)
            for final_mina, shape in features:
                geom = shape.projectAs(tile_sr, transform) if need_proj else shape
                buffered = geom.buffer(buffer_m)
                # not disjoint == intersects. Tiles that only touch the buffer edge are
                # included on purpose (extra edge coverage for downstream surfaces).
                selected = [path for path, foot in footprints if not buffered.disjoint(foot)]
                arcpy.SetProgressorPosition()

                if not selected:
                    _warn("Mina '{}' ({}): no tiles intersect the buffered polygon. "
                          "Skipping.".format(final_mina, source))
                    no_coverage.append((final_mina, source))
                    continue

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
                    continue
                if os.path.exists(out_path) and overwrite_existing:
                    _msg("Mina '{}' ({}): overwriting existing {}.".format(
                        final_mina, source, out_name))

                arcpy.management.MosaicToNewRaster(
                    input_rasters=selected,
                    output_location=location,
                    raster_dataset_name_with_extension=out_name,
                    coordinate_system_for_the_raster=tile_sr,
                    pixel_type=pixel_type,
                    number_of_bands=1,
                    mosaic_method=mosaic_method,
                )
                _msg("Mina '{}' ({}): mosaicked {} tiles -> {}".format(
                    final_mina, source, len(selected), out_name))

            arcpy.ResetProgressor()

        _msg("Done. Minas: {}. Sources: {}. No coverage cases: {}. Collisions resolved: {}.".format(
            total_minas, ", ".join(s for s, _ in sources), len(no_coverage), n_collisions))
        if no_coverage:
            _warn("No coverage (mina, source): " + "; ".join(
                "{} {}".format(m, s) for m, s in no_coverage))
        return


# ===========================================================================
# Self tests (pure functions, no arcpy, no numpy). Run: python MiningTerrainToolbox.pyt
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

    print("linear_unit_to_meters")
    check("kilometers", linear_unit_to_meters("5 Kilometers") == 5000.0)
    check("meters", linear_unit_to_meters("250 Meters") == 250.0)
    check("miles", abs(linear_unit_to_meters("1 Miles") - 1609.344) < 1e-6)
    check("decimal comma tolerated", linear_unit_to_meters("2,5 Kilometers") == 2500.0)
    check_raises("degrees rejected", lambda: linear_unit_to_meters("5 DecimalDegrees"))
    check_raises("non numeric value rejected", lambda: linear_unit_to_meters("abc Meters"))
    check_raises("missing unit rejected", lambda: linear_unit_to_meters("5"))

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
