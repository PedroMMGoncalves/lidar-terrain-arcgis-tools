# End to end tests

Scripts that exercise the tools on real DGT data and on the live CDD portal, the way the
toolbox was validated. They are not unit tests: they need ArcGIS Pro, the data paths set at
the top of each script (local folders on this machine, edit them for yours) and, for the two
live ones, the CDD credentials saved by Tool 1. Run one with the Pro Python, from this folder:

```text
"C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat" e2e_engines.py
```

Each script writes its outputs into a folder next to it (`e2e_*`, ignored by git), prints what it
measured and ends with an `ok` or `FAILED` line and a matching exit code. Timings and sizes in the
README and the commit messages come from these runs.

| Script | What it checks |
| --- | --- |
| `e2e_cover.py` | Tool 2 clip modes with the coverage option on and off (real 0.5 m tile). |
| `e2e_versions.py` | Tool 2 keeps the latest _vNN tile version and recompresses (real tile plus a v01 copy). |
| `e2e_engines.py` | Tool 2 gdal and arcpy engines agree cell for cell (extent, polygon, none; cover on and off; FIRST and LAST). |
| `e2e_sheet8c.py` | Sheet scale timing of both engines on the real 8C sheet at 2 m (2542 tiles, through a junction). |
| `e2e_tools3to6.py` | Tools 2 to 6 on a real tile: every output recompressed, pyramid option honored. |
| `e2e_recompress_tool.py` | Tool 13 dry run, rewrite and no-op re-run on real tiles. |
| `e2e_ortho.py` | Tool 12 on synthetic DGT style orthophoto blocks (no network). |
| `e2e_download.py` | Tool 1 against the live CDD: version filter, orthophoto MIME, one tile recompressed on arrival (needs credentials). |
| `e2e_real.py` | Tool 1 plus Tool 12 on real orthophoto blocks from the live CDD (needs credentials, ~460 MB). |
| `validate_toolbox.py` | Loads the toolbox like Pro and validates every tool's parameters (no data needed). |

The sheet scale script creates a junction named after the area that points at the tile folder
and removes it at the end; the two live scripts download a few hundred MB. Run the pure unit
tests first (`python LidarTerrainToolbox.pyt` at the repository root), they take seconds.
