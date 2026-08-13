# Build RORB Results Viewer as a standalone Windows exe
# Run this script from the standalone_viewer/ directory:
#   cd standalone_viewer
#   .\build.ps1
#
# Two things this script works around, both learned the hard way:
#
# 1. NON-ASCII REPO PATH.  This repo lives under a Google Drive folder whose
#    name is not ASCII (G:\我的雲端硬碟\...).  Qt's QLibraryInfo returns its
#    own plugin path mangled through the ANSI codepage ("G:/??????/..."), so
#    PyInstaller's PyQt5 hook aborts with "Qt plugin directory does not exist".
#    Fix: stage the sources into an ASCII-only work directory, build there, and
#    copy the finished zip back to the repo root.  (PYTHONUTF8 does not help —
#    the mangling happens inside Qt.)
#
# 2. ANACONDA'S MKL NUMPY.  Building in the Anaconda base env bundles ~330 MB
#    of mkl_*.dll, pushing the zip to 230 MB — over GitHub's 100 MB file limit.
#    Fix: build inside a dedicated venv, where the PyPI numpy wheel uses
#    OpenBLAS (~15 MB) and the zip stays near 85 MB.
#
# Pass -Fresh to rebuild the venv from scratch.

param([switch]$Fresh)

Set-Location $PSScriptRoot

# Anaconda is used only as the *base interpreter* for the venv (a known-good
# Python for PyQt5 wheels) and for the two expat binaries bundled below.
$anaconda = "C:\ProgramData\anaconda3"
$work     = Join-Path $env:LOCALAPPDATA "rorb_viewer_build"   # ASCII-only path
$venv     = Join-Path $work ".buildenv"
$venvPy   = Join-Path $venv "Scripts\python.exe"
$zip      = Join-Path $PSScriptRoot "..\RORB_Results_Viewer.zip"

if ($Fresh -and (Test-Path $work)) {
    Write-Host "Removing existing work dir..." -ForegroundColor Cyan
    Remove-Item $work -Recurse -Force
}
if (-not (Test-Path $work)) { New-Item -ItemType Directory -Path $work | Out-Null }

Write-Host "Staging sources to $work ..." -ForegroundColor Cyan
foreach ($f in @("main.py", "viewer.py", "engine.py", "requirements.txt")) {
    Copy-Item (Join-Path $PSScriptRoot $f) $work -Force
}

if (-not (Test-Path $venvPy)) {
    Write-Host "Creating build venv..." -ForegroundColor Cyan
    & "$anaconda\python.exe" -m venv $venv
    if (-not (Test-Path $venvPy)) {
        Write-Host "Failed to create venv." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install pyqt5 matplotlib numpy pyinstaller --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dependency install failed." -ForegroundColor Red
    exit 1
}

Write-Host "Cleaning previous build..." -ForegroundColor Cyan
foreach ($d in @("dist", "build")) {
    $p = Join-Path $work $d
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
$spec = Join-Path $work "RORB_Results_Viewer.spec"
if (Test-Path $spec) { Remove-Item $spec -Force }

Write-Host "Building exe..." -ForegroundColor Cyan
Push-Location $work
& $venvPy -m PyInstaller `
  --onedir `
  --windowed `
  --name "RORB_Results_Viewer" `
  --hidden-import "PyQt5.sip" `
  --hidden-import "matplotlib.backends.backend_qt5agg" `
  --hidden-import "matplotlib.backends.backend_agg" `
  --exclude-module "tkinter" `
  --exclude-module "IPython" `
  --exclude-module "pandas" `
  --exclude-module "scipy" `
  --add-binary "$anaconda\Library\bin\libexpat.dll;." `
  --add-binary "$anaconda\DLLs\pyexpat.pyd;." `
  main.py
Pop-Location

$exe = Join-Path $work "dist\RORB_Results_Viewer\RORB_Results_Viewer.exe"
if (-not (Test-Path $exe)) {
    Write-Host "Build failed — check output above." -ForegroundColor Red
    exit 1
}

Write-Host "Zipping..." -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $work "dist\RORB_Results_Viewer") -DestinationPath $zip

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Done!  Output: $zip  ($mb MB)" -ForegroundColor Green
Write-Host "Build tree kept at $work (delete it or pass -Fresh to start clean)."
if ($mb -gt 95) {
    Write-Host "WARNING: over GitHub's 100 MB file limit — do not commit this zip." -ForegroundColor Yellow
}
Write-Host "Your friend unzips it and runs RORB_Results_Viewer\RORB_Results_Viewer.exe"
