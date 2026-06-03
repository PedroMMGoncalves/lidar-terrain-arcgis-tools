# lidar-terrain-arcgis-tools

> ArcGIS Pro Python toolbox that derives topographic factors (slope, aspect, hillshade, curvature, solar radiation) from LiDAR DEM and DSM, in batch, per area of interest.

[![ArcGIS Pro](https://img.shields.io/badge/ArcGIS_Pro-3.7-green.svg)](https://www.esri.com/en-us/arcgis/products/arcgis-pro/overview)
[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org)
[![Spatial Analyst](https://img.shields.io/badge/Extension-Spatial_Analyst-orange.svg)](https://www.esri.com/en-us/arcgis/products/arcgis-spatial-analyst/overview)
[![Image Analyst](https://img.shields.io/badge/Extension-Image_Analyst-orange.svg)](https://www.esri.com/en-us/arcgis/products/arcgis-image-analyst/overview)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](https://www.esri.com/en-us/arcgis/products/arcgis-pro/overview)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

A single ArcGIS Pro Python Toolbox (`.pyt`) that processes, in batch, LiDAR elevation data (DEM and DSM), per area of interest, to generate topographic factors: slope, aspect, hillshade, profile and plan curvature, and annual solar radiation. Every output follows one consistent naming convention so it can feed any downstream analysis.

This toolbox is the **factor engine only**. Any downstream analysis (suitability modeling, factor weighting, exclusion masks) is done elsewhere and is NOT part of this toolbox. Project CRS: ETRS89 / PT-TM06 (EPSG:3763), meters, Z factor 1.

## Contents

[Summary](#summary) - [Method](#method) - [Requirements](#requirements) - [Data source](#data-source) - [Installation](#installation) - [Usage](#usage) - [Naming convention](#naming-convention) - [Example](#example) - [Tests](#tests) - [Troubleshooting](#troubleshooting) - [Limitations and notes](#limitations-and-notes) - [Contributing](#contributing) - [Citation](#citation) - [License](#license)

---

## Summary

Four tools form the pipeline, run in order, with the input and output folders always chosen explicitly by the user. A fifth tool, Resample, is an optional utility you can run on any of the outputs:

1. **Build Mosaics by Polygon**: one DEM and one DSM mosaic per area of interest, from the DGT LiDAR download folders.
2. **Generate Surfaces**: slope, aspect, hillshade and curvature (profile and plan) from each mosaic.
3. **Solar Radiation**: annual incoming solar radiation (global, kWh/m2) per area, computed on the DEM with RasterSolarRadiation (GPU).
4. **Reclassify Slope and Aspect**: ordinal integer classes for slope and aspect, defined in a value table.
5. **Resample** (optional): resample the named rasters to a coarser cell size, for example 2 m to 10 m, into a `Resample` folder grouped by area.

Every output is named by a single, consistent convention (see [Naming convention](#naming-convention)) so each tool can find and parse what the previous one produced.

---

## Method

```mermaid
flowchart TD
    AOI["AOI polygons<br/>Area name + FID"]
    LID["DGT LiDAR folders<br/>..._&lt;FID&gt; with MDT (DEM) and MDS (DSM)"]
    AOI --> T1
    LID --> T1
    T1["Tool 1<br/>Build Mosaics by Polygon"]
    T1 --> M["Per area mosaics<br/>Area_DEM.tif / Area_DSM.tif"]
    M --> T2["Tool 2<br/>Generate Surfaces"]
    M --> T3["Tool 3<br/>Solar Radiation"]
    M -.-> T5["Tool 5 (optional)<br/>Resample"]
    T2 --> S["Surfaces<br/>SLOPE, ASPECT, HILLSHADE, PROFC, PLANC"]
    T3 --> SOL["Area_DEM_SOLAR.tif<br/>(kWh/m2)"]
    T5 -.-> RS["Resample folder<br/>coarser cell size (e.g. 2 m to 10 m)<br/>grouped by area"]
    S --> T4["Tool 4<br/>Reclassify Slope and Aspect"]
    T4 --> RCL["Ordinal factors<br/>Area_SOURCE_SLOPE_RCL.tif / _ASPECT_RCL.tif"]
    RCL --> EXT["Analysis<br/>(external to this toolbox)"]
    SOL --> EXT

    classDef tool fill:#1f6feb,stroke:#0d3b8a,color:#ffffff;
    classDef opttool fill:#1f6feb,stroke:#0d3b8a,color:#ffffff,stroke-dasharray:5 3;
    classDef data fill:#eaf2ff,stroke:#1f6feb,color:#0b2a5b;
    classDef ext fill:#f5f5f5,stroke:#999999,color:#333333,stroke-dasharray:4 3;
    class T1,T2,T3,T4 tool;
    class T5 opttool;
    class AOI,LID,M,S,SOL,RCL,RS data;
    class EXT ext;
```

- The DGT LiDAR arrives pre-split into one download folder per area of interest, where the folder name ends in the AOI feature FID and holds the tiles in `MDT*` (terrain, DEM) and `MDS*` (surface, DSM) subfolders. The 5 km buffer selection was already applied at download time.
- **Tool 1** groups AOI features by area name (the folder number equals the feature FID), merges each area's MDT and MDS tiles across all of its folders into one DEM and one DSM mosaic, and deduplicates tiles by name. It optionally verifies, per folder, that the tile extent contains the AOI polygon centroid (a guard against a wrong FID mapping).
- **Tool 2** derives the selected surfaces with Spatial Analyst (and Image Analyst for the multidirectional hillshade). Slope is in degrees; aspect uses the Esri convention with -1 for flat; curvature produces profile and plan.
- **Tool 3** computes annual global solar radiation (kWh/m2) on the DEM with RasterSolarRadiation (GPU accelerated). The DEM is resampled to a coarser solar cell size first (default 10 m) because annual insolation is a smooth field, which keeps the heavy whole year run tractable.
- **Tool 4** reclassifies slope and aspect into ordinal integer classes using `[min, max)` semantics (the last class inclusive at the top), implemented in numpy for deterministic boundaries. The value table validation fails loud on gaps and on overlaps between different class ids before anything is written.
- **Tool 5** (optional) resamples the selected data types to a target cell size with core Resample, writing to a `Resample` folder grouped by area; continuous rasters use bilinear, reclassified (`_RCL`) rasters use nearest. Typical use is 2 m to 10 m to lighten the later steps.
- EPSG is explicit in code and in the logs. Mosaics inherit the tiles CRS (EPSG:3763); the AOI layer, which may be in a different CRS, is projected only for the extent check.

---

## Requirements

- ArcGIS Pro **3.7**, Windows (arcpy is Windows only on ArcGIS Pro 3.x). Uses the Python bundled with ArcGIS Pro and numpy from that environment; no extra packages.
- **Spatial Analyst** extension for Tool 2 (slope, aspect, curvature, traditional hillshade) and Tool 3 (solar radiation). RasterSolarRadiation uses the GPU when available and falls back to the CPU.
- **Image Analyst** extension for Tool 2 only when the hillshade type is Multidirectional.
- Tools 1, 4 and 5 need no extension (mosaicking and resampling are core, and the reclassification is pure numpy).
- Tiles in the project CRS, ETRS89 / PT-TM06 (EPSG:3763). The tools log the CRS and warn if it differs.

---

## Data source

The tools were built for the DGT LiDAR survey of mainland Portugal, *Levantamento LiDAR de Portugal Continental*, produced by the [Direção-Geral do Território (DGT)](https://www.dgterritorio.gov.pt/levantamento-lidar-de-portugal-continental-0). The survey provides a 10 points/m2 LAZ point cloud and the derived terrain model (MDT, the DEM) and surface model (MDS, the DSM) at 0.5 m and 2 m resolution as GeoTIFF, under an open data policy with no usage restrictions. This project used the 2 m models (`MDT-2m`, `MDS-2m`), but the tools are not tied to a resolution: Tool 1 mosaics any `MDT*` and `MDS*` tiles, so the 0.5 m models (`MDS-50cm-...`) work as well. When a download folder holds more than one resolution, set Tool 1's *Tile resolution* parameter (for example `2m` or `50cm`) to pick one; left blank it auto-detects a single resolution and fails loud on a mix, so a mosaic never blends cell sizes.

The data is distributed through the DGT Data Center (CDD), which requires a free registration: <https://cdd.dgterritorio.gov.pt/dgt-fe>.

The tiles used here were downloaded with the [DGT CDD Downloader](https://plugins.qgis.org/plugins/dgt_cdd_downloader/) plugin for QGIS (Duarte Carreira, Hugo Santos and Pedro Venâncio). It authenticates against the CDD portal, splits a large area into chunks, organizes the files per area, and can build a per folder VRT. That is the exact on disk layout Tool 1 expects: one download folder per area of interest, each with `MDT*` and `MDS*` subfolders and a per product `.vrt`.

> The factor tools (2 to 4) work on any DEM and DSM raster. Only Tool 1 (mosaicking) is tailored to the DGT download folder layout described above.

---

## Installation

1. In *Catalog*, right-click a folder, choose **Add Toolbox**, and select `LidarTerrainToolbox.pyt`.
2. The tools appear at the root of the **LiDAR Terrain Toolbox**, with numbered labels (01, 02, ...) so they run in pipeline order.

No installation beyond *Add Toolbox*: the script runs on the Python bundled with ArcGIS Pro. To remove it, right-click the toolbox in *Catalog* and choose *Remove*.

---

## Usage

Run the tools in order. Each tool takes its input from the previous tool's output folder; nothing is inferred by hidden convention.

### Tool 1, Build Mosaics By Polygon

One DEM and one DSM mosaic per area, merging all of that area's download folders.

| Parameter | Description |
| --- | --- |
| AOI layer | Polygon layer whose FID numbers the download folders. Carries the `Area` name field. |
| Area name field | Field that names the output (sanitized: accents and special characters removed). |
| LiDAR root folder | Folder that contains the `..._<FID>` download folders. |
| Output folder | Where the mosaics are written. |
| Output structure | `per_area_subfolder` (default) or `flat`. |
| Products | `BOTH` (default), `DEM`, or `DSM`. |
| Pixel type | Default `32_BIT_FLOAT` (DGT LiDAR is float). |
| Mosaic method | For overlaps; `FIRST` recommended for contiguous tiles. |
| Overwrite existing outputs | Off skips existing mosaics. |
| Skip areas with missing folders | On (default) skips areas whose folders are not all present yet. |
| Verify folder extent against AOI polygon | On (default) checks each folder maps to the right area. |
| Folder name prefix | Optional. Leave blank to auto-detect the prefix from the data folders. |
| Tile resolution | Optional. Leave blank to auto-detect; set (for example `2m` or `50cm`) to pick one resolution when a folder holds more than one. |

### Tool 2, Generate Surfaces

Topographic surfaces from the Tool 1 mosaics. Each surface has its own checkbox (all on by default).

| Parameter | Description |
| --- | --- |
| Input mosaics folder | The Tool 1 output. |
| Recurse subfolders | On (default) finds the `per_area_subfolder` layout. |
| Output structure | `same_as_input` (default; each surface is written next to its input mosaic), `per_area_subfolder`, or `flat`. |
| Output folder | Only for `per_area_subfolder` or `flat`; greyed out and not needed for `same_as_input`. |
| Source | `BOTH` (default), `DEM`, or `DSM`. |
| Slope, Aspect, Hillshade, Profile curvature, Plan curvature | One checkbox each. |
| Z factor | Default 1 (project is metric). |
| Hillshade type | `Multidirectional` (default, Image Analyst) or `Traditional`. |
| Hillshade azimuth, altitude | Traditional hillshade only. |
| Overwrite existing outputs | Off skips existing surfaces. |

### Tool 3, Solar Radiation

Annual incoming solar radiation (global, kWh/m2) per area, with `RasterSolarRadiation` (GPU when available). This is the heavy tool; expect long run times. The DEM is resampled to a coarser solar cell size first, since annual insolation is a smooth field.

| Parameter | Description |
| --- | --- |
| Input mosaics folder | The Tool 1 output. |
| Recurse subfolders | On (default). |
| Output structure, Output folder | `same_as_input` default, as in the other tools. |
| Source | `DEM` (default, the terrain resource for ground PV), `DSM`, or `BOTH`. |
| Solar cell size | Meters to resample the DEM to before the run; default 10 (0 = native 2 m). |
| Resample method | `BILINEAR` (default). |
| Year | Whole year; the year only sets the leap year. |
| Shadow neighborhood distance | How far to look for terrain shadows; default `1000 Meters`, adaptive. |
| Transmittivity, Diffuse proportion | Atmosphere; defaults 0.6 and 0.3 (clear-sky conditions). |
| Overwrite | Off skips existing outputs. |

### Tool 4, Reclassify Slope and Aspect

Ordinal classes for slope and aspect, from a value table.

| Parameter | Description |
| --- | --- |
| Input factor rasters folder | The Tool 2 output. |
| Recurse subfolders | On (default). |
| Factor to process | `BOTH` (default), `SLOPE`, or `ASPECT`. |
| Slope classes, Aspect classes | Value tables of `class_id`, `min`, `max`. `[min, max)`, last class inclusive at the top. |
| Flat class value | Optional. Class for the Aspect Flat (-1). |
| Output structure | `same_as_input` (default; each reclassified raster is written next to its input), `per_area_subfolder`, or `flat`. |
| Output folder | Only for `per_area_subfolder` or `flat`; greyed out and not needed for `same_as_input`. |
| Unmapped values to NoData | On (default). Off makes any cell outside all classes a fail loud error. |
| Overwrite existing outputs | Off skips existing reclassified rasters. |

The value table allows the **same `class_id` on multiple rows**, which supports the circular aspect (for example North split into `315 to 360` and `0 to 45`, both class id 1). The tool does not detect wraparound; you partition the circle into linear rows. The validation fails loud on a gap, on an overlap between different class ids, on a non integer class id, and on `min` not less than `max`, before any raster is written.

### Tool 5, Resample

Resample the named rasters to a coarser cell size (for example 2 m to 10 m), in batch. An optional utility; point it at any folder of mosaics or surfaces. Outputs go to a `Resample` folder inside the results root, grouped by area, with the file names unchanged, so the other tools read them the same way.

| Parameter | Description |
| --- | --- |
| Results folder | The results root; a `Resample` subfolder is created inside it. |
| Recurse subfolders | On (default) finds the `per_area_subfolder` layout. |
| Data types to resample | Multi-select: `DEM`, `DSM`, `SLOPE`, `ASPECT`, `HILLSHADE`, `PROFC`, `PLANC`, `SOLAR`, `SLOPE_RCL`, `ASPECT_RCL`. Default `DEM`, `DSM`. |
| Target cell size (meters) | Default 10. |
| Overwrite existing outputs | Off skips existing resampled rasters. |

The method is automatic per type: bilinear for continuous rasters, nearest for reclassified (`_RCL`) so the ordinal classes are preserved. A raster already at the target cell size is skipped; a target finer than the native cell size warns, since upsampling adds no real detail.

---

## Naming convention

A single convention links the tools:

- Mosaic: `{Area}_{SOURCE}` where SOURCE is `DEM` or `DSM`.
- Surface: `{Area}_{SOURCE}_{PRODUCT}` where PRODUCT is `SLOPE`, `ASPECT`, `HILLSHADE`, `PROFC`, `PLANC` or `SOLAR`.
- Reclassified: the surface name plus `_RCL`.

The `.tif` extension is added on write. Area names are sanitized for ArcGIS and the file system (accents and c cedilla folded to ASCII, separators to underscore). If two different areas sanitize to the same name they get a numeric suffix; several AOI polygons of the **same** area are merged into one output.

| Stage | Example output |
| --- | --- |
| Mosaic (DEM) | `Sao_Domingos_DEM.tif` |
| Surface (slope) | `Sao_Domingos_DEM_SLOPE.tif` |
| Reclassified (aspect) | `Sao_Domingos_DEM_ASPECT_RCL.tif` |

---

## Example

A real Tool 1 run over 22 selected AOI features (the AOI layer was in EPSG:102164 and was projected only for the extent check; the mosaics inherit the tiles CRS, EPSG:3763). The 22 features resolved to 21 areas, because one area (`Cortes Pereira`) has two AOI polygons that were merged into a single output.

Abbreviated geoprocessing log:

```text
AOI layer CRS: EPSG:102164.
AOI layer is EPSG:102164, not the project EPSG:3763. Polygons are projected for the extent check only.
Sanitized area name 'Defesa das Mercês' to 'Defesa_das_Merces'.
Sanitized area name 'Preguiça' to 'Preguica'.
Area 'Alcaria_Queimada' (DEM): mosaicked 112 tiles from 1 folder(s) -> Alcaria_Queimada_DEM.tif
Area 'Alcaria_Queimada' (DSM): mosaicked 112 tiles from 1 folder(s) -> Alcaria_Queimada_DSM.tif
Area 'Cortes_Pereira' (DEM): mosaicked 95 tiles from 2 folder(s) -> Cortes_Pereira_DEM.tif
Area 'Cortes_Pereira' (DSM): mosaicked 95 tiles from 2 folder(s) -> Cortes_Pereira_DSM.tif
...
Done. Areas: 21. Built now: 21. Already present: 0. Mosaics created: 42. Skipped (no data: 0, incomplete: 0, no tiles: 0, extent mismatch: 0).
```

You then point Tool 2 at this output folder to derive the surfaces, and Tool 4 at the Tool 2 output to reclassify slope and aspect.

---

## Tests

The shared helpers have pure unit tests inside the toolbox file, runnable outside ArcGIS:

```text
python LidarTerrainToolbox.pyt
```

This exercises name sanitization and collision handling, the output name build and parse round trip, the value table validation (gaps, overlaps, repeated class id), the area grouping, the folder prefix auto-detection, the VRT extent parsing, the tile resolution parsing and selection, the resample type mapping, and the numpy reclassification logic (the numpy tests are skipped if numpy is not installed in the Python being used). Under ArcGIS Pro the test block does not run; ArcGIS imports the module, it does not execute it as a script.

---

## Troubleshooting

| Message in the geoprocessing log | Cause | Fix |
| --- | --- | --- |
| `No LiDAR data folders ... found` | LiDAR root does not contain folders with `MDT*`/`MDS*` | Point at the folder that contains the `..._<FID>` download folders. |
| `Cannot auto-detect a folder prefix ...` | Folder names are inconsistent or have no trailing FID number | Set the folder prefix explicitly. |
| `... missing folders for FIDs ...` (skipped) | An area's download folders are not all present yet | Re-run after the download finishes (keep Skip incomplete on). |
| `... does not contain its AOI polygon centroid` | A folder likely maps to the wrong area | Check the FID to folder mapping, or turn the extent check off to isolate. |
| `Spatial Analyst extension is not available` | Tool 2 has no Spatial Analyst license | Enable Spatial Analyst in *Project > Licensing*. |
| `Multidirectional hillshade needs the Image Analyst extension` | Tool 2 multidirectional without Image Analyst | Enable Image Analyst, or choose Traditional, or turn hillshade off. |
| `Gap in coverage between ...` / `Overlap between different classes ...` | Tool 4 value table is not a clean tiling | Fix the class intervals so they tile the range with no holes or cross class overlaps. |
| `Class id 9999 collides with the NoData value ...` | A class id or flat class equals the reserved NoData | Use a different class id. |

---

## Limitations and notes

- **The ordinal scale is not harmonized between factors.** You define arbitrary intervals per factor; only slope and aspect are reclassified. Harmonizing the classes is a downstream analysis step, outside this toolbox. *Document this for whoever consumes the outputs, to avoid misuse.*
- **The downstream analysis is external** and not part of this toolbox.
- **Folder layout assumption (Tool 1):** the download folder number must equal the AOI feature FID (0 based shapefile FID). Loading the AOI as a geodatabase feature class (1 based OBJECTID) can shift the mapping; the tool warns when the OID field is not `FID`.
- **Memory (Tool 4):** each factor raster is read into memory for the numpy reclassification. The per area extents are tolerable; a very large raster can exceed memory, and the tool then aborts with a clear message.
- **Datum reprojection:** `projectAs` uses the default transformation. Where no default geographic transformation exists between the input and project datums, it may need to be specified explicitly.
- **Aspect Flat:** aspect uses -1 for flat. In Tool 4 the optional flat class wins over any class interval that happens to contain -1.

---

## Contributing

Issues and pull requests are welcome. When reporting a problem, please include:

- ArcGIS Pro version (Help > About).
- The tool, its parameters, and the input CRS and EPSG code (the tools print the CRS in the log).
- The tail of the geoprocessing log, including the final `Done. ...` summary line.
- A minimal dataset or description that reproduces the issue, if possible.

---

## Citation

If you use this toolbox, please cite it via the metadata in [`CITATION.cff`](CITATION.cff) (GitHub renders this in the sidebar as "Cite this repository"). A versioned DOI is minted via Zenodo for each GitHub release.

---

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
