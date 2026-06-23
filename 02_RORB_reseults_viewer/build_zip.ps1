# Rebuild rorb_qgis.zip with the correct top-level directory structure
# QGIS requires: rorb_qgis/<files> inside the zip

$baseDir = $PSScriptRoot
$srcDir = Join-Path $baseDir "rorb_qgis"
$zipPath = Join-Path $baseDir "rorb_qgis.zip"
$tmpDir = Join-Path $env:TEMP "rorb_qgis_build"

if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
New-Item -ItemType Directory -Path $tmpDir | Out-Null

Copy-Item -Path $srcDir -Destination $tmpDir -Recurse
Get-ChildItem -Path (Join-Path $tmpDir "rorb_qgis") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $tmpDir "rorb_qgis") -DestinationPath $zipPath

Remove-Item $tmpDir -Recurse -Force
Write-Host "Built: $zipPath"
