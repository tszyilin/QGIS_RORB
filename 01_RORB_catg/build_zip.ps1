# Rebuild rorb_catg.zip with the correct top-level directory structure
# QGIS requires: rorb_catg/<files> inside the zip
# Output goes to the repo root so plugins.xml download_url works

$baseDir   = $PSScriptRoot
$repoRoot  = Split-Path $baseDir -Parent
$srcDir    = Join-Path $baseDir "rorb_catg"
$zipPath   = Join-Path $repoRoot "rorb_catg.zip"
$tmpDir    = Join-Path $env:TEMP "rorb_catg_build"

if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
New-Item -ItemType Directory -Path $tmpDir | Out-Null

Copy-Item -Path $srcDir -Destination $tmpDir -Recurse
Get-ChildItem -Path (Join-Path $tmpDir "rorb_catg") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $tmpDir "rorb_catg") -DestinationPath $zipPath

Remove-Item $tmpDir -Recurse -Force
Write-Host "Built: $zipPath"
