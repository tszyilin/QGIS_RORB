# RORB QGIS Toolbox — Claude context

## Repo layout

```
QGIS_RORB/
  01_RORB_catg/           ← development source for the catchment prep plugin
    rorb_catg/            ← the actual QGIS plugin package
    rorb_specific/        ← standalone helper scripts (not part of the plugin)
    build_zip.ps1         ← builds rorb_catg.zip at repo root (legacy, kept for reference)
  02_RORB_reseults_viewer/ ← development source for the results viewer plugin
    rorb_qgis/            ← the actual QGIS plugin package
    build_zip.ps1         ← builds rorb_qgis.zip at repo root (legacy, kept for reference)
  rorb_suite/             ← COMBINED distribution plugin (what users install)
    __init__.py           ← classFactory → RorbSuitePlugin
    metadata.txt          ← single metadata for the combined plugin
    plugin.py             ← RorbSuitePlugin: 4-icon "RORB Tools" toolbar
    rorb_catg/            ← copy of 01_RORB_catg/rorb_catg/
    rorb_qgis/            ← copy of 02_RORB_reseults_viewer/rorb_qgis/
    build_zip.ps1         ← THE build script users/CI should run
  rorb_suite.zip          ← built artifact; what plugins.xml download_url points to
  plugins.xml             ← QGIS custom repository XML (single entry: RORB Tools)
```

## How to release an update

### If only `rorb_qgis` (results viewer) changed:
1. Edit files under `02_RORB_reseults_viewer/rorb_qgis/`
2. Bump `version=` in `02_RORB_reseults_viewer/rorb_qgis/metadata.txt`
3. Sync the copy: overwrite `rorb_suite/rorb_qgis/` with the updated source
4. Go to step "Rebuild & publish" below

### If only `rorb_catg` (catchment tools) changed:
1. Edit files under `01_RORB_catg/rorb_catg/`
2. Bump `version=` in `01_RORB_catg/rorb_catg/metadata.txt`
3. Sync the copy: overwrite `rorb_suite/rorb_catg/` with the updated source
4. Go to step "Rebuild & publish" below

### Rebuild & publish (always):
1. Bump `version=` in `rorb_suite/metadata.txt` to match the updated sub-plugin
2. Update `<version>` and `<update_date>` in `plugins.xml`
3. Run the build script (PowerShell):
   ```powershell
   & ".\rorb_suite\build_zip.ps1"
   ```
   This overwrites `rorb_suite.zip` at the repo root.
4. Commit everything and push:
   ```
   git add rorb_suite/ rorb_suite.zip plugins.xml
   git commit -m "Release vX.Y: <what changed>"
   git push
   ```
   QGIS plugin manager will detect the version bump and offer the update to users.

## QGIS version support
- Both sub-plugins support QGIS `3.22–4.99`
- `rorb_qgis` uses `compat.py` to abstract PyQt5/PyQt6 differences

## User installation URL
Users add this to QGIS Plugin Manager → Settings → Add custom repository:
```
https://raw.githubusercontent.com/tszyilin/QGIS_RORB/main/plugins.xml
```
Then install "RORB Tools" — they get all 4 toolbar icons in one plugin.

## 4 toolbar icons (in order)
| # | Icon file | Label | Action |
|---|-----------|-------|--------|
| 1 | `rorb_catg/icon_create.svg` | Create RORB Layers | Opens `CreateLayersDialog` |
| 2 | `rorb_catg/icon_catg.svg` | Build RORB .catg | Opens `RorbPipelineDialog` |
| 3 | `rorb_catg/icon_run.svg` | Run RORB | Opens `RorbRunDialog` — runs `RORB_CMD.exe` against a `.catg`/`.stm` pair |
| 4 | generated (`_peak_icon()`) | RORB Results Viewer | Opens `RorbResultsDialog` (dockable) |

`RorbRunDialog` (`rorb_catg/run_rorb_dialog.py`) builds a `.par` parameter file and calls
`RORB_CMD.exe` (bundled with a separately-licensed RORBwin install, not in the plugin zip).
It parses the `.catg`'s interstation areas (including zero-area "dummy" stations) into an
editable table so each area's `kc`/`m`/IL/CL can be set individually, or lumped to one
shared `kc`/`m`. On success it can hand the resulting `.out` folder straight to the
Results Viewer via `RorbResultsDialog.add_scenario()`.
