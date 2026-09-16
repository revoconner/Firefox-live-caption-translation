# Builds the backend for distribution: proc_loopback.exe if missing, the PyInstaller onedir tree into packaging\dist, then the Inno Setup installer into packaging\out once installer.iss exists.
param([switch]$SkipInstaller)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root "capvenv\Scripts\python.exe"
$native = Join-Path $root "backend\native\proc_loopback.exe"
if (-not (Test-Path $native)) { & (Join-Path $root "backend\native\build.ps1") }

& $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $PSScriptRoot "dist") --workpath (Join-Path $PSScriptRoot "build") (Join-Path $PSScriptRoot "LiveCaptionTranslate.spec")
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

$iss = Join-Path $PSScriptRoot "installer.iss"
$iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not $SkipInstaller -and (Test-Path $iss)) {
    & $iscc $iss
    if ($LASTEXITCODE -ne 0) { throw "inno setup failed" }
}
Write-Host "done"
