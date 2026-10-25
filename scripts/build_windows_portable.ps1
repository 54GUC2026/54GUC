param(
    [string]$SourceRepo = "",
    [string]$Ref = "HEAD",
    [string]$StagingRoot = "",
    [string]$OutputRoot = "",
    [string]$ReleaseTag = "54GUC",
    [string]$Stamp = "",
    [string]$PythonExe = "",
    [switch]$SkipDependencyInstall,
    [switch]$SkipBundleValidation
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cgf_windows_path_registry.ps1")

if (-not $Stamp) {
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
}

if (-not $OutputRoot) {
    $OutputRoot = $CgfWindowsArtifactRoot
}
if (-not $StagingRoot) {
    $StagingRoot = $CgfWindowsBuildStagingRoot
}
if (-not $PythonExe) {
    $PythonExe = $CgfWindowsPython
}

function Assert-LocalWindowsPath {
    param(
        [string]$PathValue,
        [string]$Label
    )

    if (-not $PathValue -or $PathValue -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be a local Windows drive path. Got: $PathValue"
    }
}

function Resolve-WindowsPortableBundleBaseName {
    param(
        [string]$SelectedReleaseTag,
        [string]$SelectedStamp
    )

    return "{0}_Windows_portable_{1}" -f $SelectedReleaseTag, $SelectedStamp
}

[void](Assert-CgfWindowsArtifactPath -PathValue $OutputRoot -Label "OutputRoot")
[void](Assert-CgfWindowsBuildPath -PathValue $StagingRoot -Label "StagingRoot")
[void](Assert-CgfWindowsArtifactPath -PathValue $PythonExe -Label "PythonExe")
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$ExportArgs = @{
    Ref = $Ref
    Stamp = $Stamp
    Purpose = "Build"
}
if ($SourceRepo) {
    $ExportArgs["SourceRepo"] = $SourceRepo
}
$ExportArgs["StagingRoot"] = $StagingRoot

$ExportManifest = & (Join-Path $PSScriptRoot "export_windows_build.ps1") @ExportArgs
if (-not $ExportManifest) {
    throw "export_windows_build.ps1 did not return a build manifest."
}

$SourceStage = $ExportManifest.source_stage
$BuildScript = Join-Path $SourceStage "scripts\build_windows_native_bundle.ps1"
$TestScript = Join-Path $SourceStage "scripts\test_windows_portable.ps1"

if (-not (Test-Path $BuildScript)) {
    throw "Missing staged Windows build script: $BuildScript"
}

$NativeArgs = @{
    ReleaseTag = $ReleaseTag
    Stamp = $Stamp
    Profile = "bootstrap"
    SourceCommit = [string]$ExportManifest.source_commit
}
$NativeArgs["PythonExe"] = $PythonExe
if ($SkipDependencyInstall) {
    $NativeArgs["SkipDependencyInstall"] = $true
}

& $BuildScript @NativeArgs
if ($LASTEXITCODE -ne 0) {
    throw "Windows native bundle build failed for $SourceStage"
}

$BundleBaseName = Resolve-WindowsPortableBundleBaseName -SelectedReleaseTag $ReleaseTag -SelectedStamp $Stamp
$BuiltStageDir = Join-Path $SourceStage ("dist\{0}" -f $BundleBaseName)
$BuiltZipPath = Join-Path $SourceStage ("dist\{0}.zip" -f $BundleBaseName)

if (-not (Test-Path $BuiltStageDir)) {
    throw "Missing built stage directory: $BuiltStageDir"
}
if (-not (Test-Path $BuiltZipPath)) {
    throw "Missing built portable zip: $BuiltZipPath"
}

if ((-not $SkipBundleValidation) -and (Test-Path $TestScript)) {
    & $TestScript -StageDir $BuiltStageDir
    if ($LASTEXITCODE -ne 0) {
        throw "Windows portable validation failed for $BuiltStageDir"
    }
}

$FinalStageDir = Join-Path $OutputRoot $BundleBaseName
$FinalZipPath = Join-Path $OutputRoot ("{0}.zip" -f $BundleBaseName)
Remove-Item $FinalStageDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $FinalZipPath -Force -ErrorAction SilentlyContinue
Copy-Item $BuiltStageDir $FinalStageDir -Recurse

if ((-not $SkipBundleValidation) -and (Test-Path $TestScript)) {
    & $TestScript -StageDir $FinalStageDir
    if ($LASTEXITCODE -ne 0) {
        throw "Windows portable validation failed for final stage $FinalStageDir"
    }
}

Compress-Archive -Path (Join-Path $FinalStageDir "*") -DestinationPath $FinalZipPath -Force

[pscustomobject]@{
    source_stage = $SourceStage
    output_stage = $FinalStageDir
    output_zip = $FinalZipPath
    release_tag = $ReleaseTag
    stamp = $Stamp
    source_commit = $ExportManifest.source_commit
}
