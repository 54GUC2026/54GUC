param(
    [string]$StageDir = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

if (-not $StageDir) {
    throw "StageDir is required."
}

$ResolvedStageDir = (Resolve-Path $StageDir).Path
if ($ResolvedStageDir -notmatch '^[A-Za-z]:\\') {
    throw "StageDir must be a local Windows drive path. Got: $ResolvedStageDir"
}

$ExePath = Join-Path $ResolvedStageDir "54GUC.exe"
if (-not (Test-Path $ExePath)) {
    throw "Missing portable executable: $ExePath"
}

$RuntimeRoot = Join-Path $ResolvedStageDir "portable_runtime"
$RuntimeOutput = Join-Path $RuntimeRoot "guc_output"
$ManifestPath = Join-Path $ResolvedStageDir "version_manifest.json"
$BundleZip = Join-Path (Split-Path $ResolvedStageDir -Parent) ((Split-Path $ResolvedStageDir -Leaf) + ".zip")

if ($PythonExe -and (Test-Path $BundleZip)) {
    & $PythonExe -m arch4.windows_bundle_validation --stage-dir $ResolvedStageDir --bundle-zip $BundleZip
    if ($LASTEXITCODE -ne 0) {
        throw "Python-side bundle validation failed for $ResolvedStageDir"
    }
}

$planJson = (& $ExePath --print-launch-plan | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "54GUC.exe --print-launch-plan failed for $ResolvedStageDir"
}

$plan = $planJson | ConvertFrom-Json
if ($plan.profile -ne "bootstrap") {
    throw "Expected bootstrap launch plan in public Windows portable stage. Got: $($plan.profile)"
}

& $ExePath --portable-validate | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "54GUC.exe --portable-validate failed for $ResolvedStageDir"
}

$ValidationPack = Get-ChildItem -Path $RuntimeRoot -Directory -Filter "validation_*" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $ValidationPack) {
    throw "No validation_* artifact pack was created under $RuntimeRoot"
}

$ValidationSummary = Join-Path $ValidationPack.FullName "portable_validation.txt"
$ValidationManifest = Join-Path $ValidationPack.FullName "validation_manifest.json"

if (-not (Test-Path $RuntimeOutput)) {
    throw "portable_runtime\\guc_output was not created under $ResolvedStageDir"
}
if (-not (Test-Path $ValidationSummary)) {
    throw "portable_validation.txt was not created under $($ValidationPack.FullName)"
}
if (-not (Test-Path $ValidationManifest)) {
    throw "validation_manifest.json was not created under $($ValidationPack.FullName)"
}
if (-not (Test-Path $ManifestPath)) {
    throw "Missing version_manifest.json in $ResolvedStageDir"
}

Write-Host "Validated Windows portable stage:"
Write-Host "  $ResolvedStageDir"
