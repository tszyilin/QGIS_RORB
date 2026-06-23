# Rebuild rorb_suite.zip — combined RORB Tools plugin
# QGIS requires: rorb_suite/<files> inside the zip
# Output goes to the repo root so plugins.xml download_url works

$baseDir  = $PSScriptRoot
$repoRoot = Split-Path $baseDir -Parent
$zipPath  = Join-Path $repoRoot "rorb_suite.zip"
$tmpDir   = Join-Path $env:TEMP "rorb_suite_build"

if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $tmpDir "rorb_suite") | Out-Null

# Copy top-level plugin files
Copy-Item "$baseDir\__init__.py"   "$tmpDir\rorb_suite\"
Copy-Item "$baseDir\metadata.txt"  "$tmpDir\rorb_suite\"
Copy-Item "$baseDir\plugin.py"     "$tmpDir\rorb_suite\"

# Copy sub-packages
Copy-Item -Recurse "$baseDir\rorb_catg" "$tmpDir\rorb_suite\rorb_catg"
Copy-Item -Recurse "$baseDir\rorb_qgis" "$tmpDir\rorb_suite\rorb_qgis"

# Remove __pycache__
Get-ChildItem -Path "$tmpDir\rorb_suite" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$tmpDir\rorb_suite" -DestinationPath $zipPath

Remove-Item $tmpDir -Recurse -Force
Write-Host "Built: $zipPath"
