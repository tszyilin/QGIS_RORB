# build.ps1
# Packages the plugin zip and syncs the version/date in plugins.xml.
# Run this before committing a new release.

$pluginDir  = Join-Path $PSScriptRoot "runoff_model"
$zipPath    = Join-Path $PSScriptRoot "runoff_model.zip"
$xmlPath    = Join-Path $PSScriptRoot "plugins.xml"

# Read version from metadata.txt
$version = (Get-Content "$pluginDir\metadata.txt" |
    Where-Object { $_ -match '^version=' }) -replace 'version=', ''

if (-not $version) {
    Write-Error "Could not read version from metadata.txt"
    exit 1
}

# Build zip (plugin folder must be top-level inside the zip for QGIS)
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Push-Location $PSScriptRoot
Compress-Archive -Path $pluginDir -DestinationPath $zipPath
Pop-Location

# Update plugins.xml — version attribute, <version> tag, and <update_date>
$today = Get-Date -Format "yyyy-MM-dd"
$xml = Get-Content $xmlPath -Raw
$xml = $xml -replace '(?<=<pyqgis_plugin name="[^"]+" version=")[^"]+(?=")', $version
$xml = $xml -replace '(?<=<version>)[^<]+(?=</version>)', $version
$xml = $xml -replace '(?<=<update_date>)[^<]+(?=</update_date>)', $today
[System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::UTF8)

Write-Host "Built runoff_model.zip  (v$version)"
Write-Host "Updated plugins.xml     (v$version, $today)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  git add runoff_model.zip plugins.xml .gitignore"
Write-Host "  git commit -m `"Release v$version`""
Write-Host "  git push"
