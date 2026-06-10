# install.ps1
# Links the plugin source into QGIS so any git pull takes effect immediately.
# Run once after cloning. Re-run if the QGIS profile changes.

$source = Join-Path $PSScriptRoot "runoff_model"
$pluginsDir = "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins"
$target = Join-Path $pluginsDir "runoff_model"

if (-not (Test-Path $pluginsDir)) {
    Write-Error "QGIS plugins directory not found: $pluginsDir"
    Write-Error "Adjust the path in this script to match your QGIS profile."
    exit 1
}

if (Test-Path $target) {
    Write-Host "Removing existing plugin at $target"
    Remove-Item $target -Recurse -Force
}

# Use a directory junction (no admin rights required on Windows)
New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Installed: $target -> $source"
Write-Host ""
Write-Host "To update: git pull  (changes apply on next QGIS plugin reload)"
