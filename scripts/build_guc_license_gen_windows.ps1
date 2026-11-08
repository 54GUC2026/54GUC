param(
    [string]$PythonExe = "",
    [string]$OutputRoot = "",
    [string]$WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
. (Join-Path $ScriptDir "cgf_windows_path_registry.ps1")

if (-not $PythonExe) {
    $PythonExe = $CgfWindowsPython
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $CgfWindowsArtifactRoot "developer_tools\license_generator"
}
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $CgfWindowsBuildStagingRoot (
        "license_generator_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
    )
}

[void](Assert-CgfWindowsArtifactPath -PathValue $PythonExe -Label "License-generator Python")
[void](Assert-CgfWindowsArtifactPath -PathValue $OutputRoot -Label "License-generator output")
[void](Assert-CgfWindowsBuildPath -PathValue $WorkRoot -Label "License-generator work root")

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "[license-build] Windows Python is missing: $PythonExe"
}

New-Item -ItemType Directory -Force -Path $OutputRoot, $WorkRoot | Out-Null
Set-Location $RootDir

Write-Host "[license-build] root=$RootDir"

$specPath = Join-Path $RootDir "GUC_License_Gen.spec"
if (-not (Test-Path $specPath)) {
    throw "[license-build] Missing spec file: $specPath"
}

Write-Host "[license-build] Checking installed PyInstaller and pinned Qt; no dependency changes"
@'
import sys
from pathlib import Path
from importlib.metadata import version
import PyInstaller
assert sys.version_info[:2] == (3, 13), "Native Python3.13 is required"
pins = dict(line.split("==", 1) for line in (Path(sys.argv[1]) / "requirements-runtime-pins.txt").read_text().splitlines() if line and not line.startswith("#"))
for name in ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6"):
    assert version(name) == pins[name], (name, version(name), pins[name])
print("Existing PyInstaller", PyInstaller.__version__, "; pinned Qt verified")
'@ | & $PythonExe - $RootDir
if ($LASTEXITCODE -ne 0) {
    throw "[license-build] Required installed build dependencies are unavailable or differ from the pins."
}

Write-Host "[license-build] Building EXE from GUC_License_Gen.spec"
$systemPaths = @(
    [Environment]::GetFolderPath("System"),
    [Environment]::GetFolderPath("Windows")
)
foreach ($systemPath in $systemPaths) {
    if (-not $systemPath -or -not (Test-Path -LiteralPath $systemPath -PathType Container)) {
        throw "[license-build] Native freezer requires existing Windows system directories."
    }
}
# Match the main app freezer: ambient tool DLLs must not replace the GUI dependencies.
$previousPath = $env:PATH
try {
    $env:PATH = $systemPaths -join [IO.Path]::PathSeparator
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $OutputRoot `
        --workpath $WorkRoot `
        $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "[license-build] PyInstaller failed."
    }
}
finally {
    $env:PATH = $previousPath
}

$exePath = Join-Path $OutputRoot "GUC_License_Gen.exe"
if (-not (Test-Path $exePath)) {
    throw "[license-build] Build finished but EXE missing: $exePath"
}

Write-Host "[license-build] PASS: $exePath"
