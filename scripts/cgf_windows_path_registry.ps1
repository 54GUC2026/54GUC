# Sole Windows artifact-path registry. HP defaults to D:\CODEX; an explicitly
# configured native node uses the same contained runtime/test/build layout.

$CgfWindowsCodexRoot = "D:\CODEX"
if (-not [string]::IsNullOrWhiteSpace($env:CGF_WINDOWS_CODEX_ROOT)) {
    $configuredRoot = $env:CGF_WINDOWS_CODEX_ROOT
    if ($configuredRoot -notmatch '^[A-Za-z]:\\') {
        throw "CGF_WINDOWS_CODEX_ROOT must be an explicit, non-retired local Windows directory."
    }
    $CgfWindowsCodexRoot = [IO.Path]::GetFullPath($configuredRoot).TrimEnd('\')
    if ($CgfWindowsCodexRoot.Length -le 3 -or
        $CgfWindowsCodexRoot -match '(?i)^C:\\(?:54b|b)(?:\\|$)') {
        throw "CGF_WINDOWS_CODEX_ROOT cannot be a drive root or retired directory."
    }
}
$CgfWindowsArtifactRoot = Join-Path $CgfWindowsCodexRoot "54b"
$CgfWindowsBuildStagingRoot = Join-Path $CgfWindowsCodexRoot "b"
$CgfWindowsPython = Join-Path $CgfWindowsArtifactRoot "hp_source_run_venv\Scripts\python.exe"
$CgfWindowsSourceRunRoot = Join-Path $CgfWindowsArtifactRoot "hp_source_run_runtime\gui_cleanroom"
$CgfWindowsFullTestRoot = Join-Path $CgfWindowsArtifactRoot "hp_full_test"
$CgfWindowsFullTestStageRoot = Join-Path $CgfWindowsFullTestRoot "stages"
$CgfWindowsB818Root = Join-Path $CgfWindowsCodexRoot "B818"
$CgfWindowsNx520Root = Join-Path $CgfWindowsCodexRoot "NX520"
$CgfWindowsB818HistoryPath = Join-Path $CgfWindowsB818Root "_new_horizon_history\new_horizon.sqlite3"
$CgfWindowsB535232HistoryPath = Join-Path $CgfWindowsArtifactRoot "b535_232\_new_horizon_history\new_horizon.sqlite3"
$CgfWindowsB535333HistoryPath = Join-Path $CgfWindowsSourceRunRoot "b535_333\_new_horizon_history\new_horizon.sqlite3"
$CgfWindowsZteMf296rHistoryPath = Join-Path $CgfWindowsArtifactRoot "zte_mf296r\_new_horizon_history\new_horizon.sqlite3"

function Assert-CgfWindowsPathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$AllowedRoot,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($PathValue -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be a local Windows drive path. Got: $PathValue"
    }
    $full = [IO.Path]::GetFullPath($PathValue).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\')
    if (
        -not $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label must stay under $root. Got: $PathValue"
    }
    return $full
}

function Assert-CgfWindowsArtifactPath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$Label
    )
    return Assert-CgfWindowsPathUnderRoot `
        -PathValue $PathValue `
        -AllowedRoot $CgfWindowsArtifactRoot `
        -Label $Label
}

function Assert-CgfWindowsSourceRuntimePath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$Lane,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $allowedRoot = $CgfWindowsArtifactRoot
    if ($Lane.Trim() -eq "b818") { $allowedRoot = $CgfWindowsB818Root }
    if ($Lane.Trim() -eq "nx520_srba1_250902") { $allowedRoot = $CgfWindowsNx520Root }
    return Assert-CgfWindowsPathUnderRoot -PathValue $PathValue -AllowedRoot $allowedRoot -Label $Label
}

function Assert-CgfWindowsBuildPath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$Label
    )
    return Assert-CgfWindowsPathUnderRoot `
        -PathValue $PathValue `
        -AllowedRoot $CgfWindowsBuildStagingRoot `
        -Label $Label
}

function Assert-CgfWindowsFullTestStagePath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$Label
    )
    return Assert-CgfWindowsPathUnderRoot `
        -PathValue $PathValue `
        -AllowedRoot $CgfWindowsFullTestStageRoot `
        -Label $Label
}

function Assert-CgfWindowsHistoryPath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($PathValue -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an explicit local Windows drive path. Got: $PathValue"
    }
    $full = [IO.Path]::GetFullPath($PathValue)
    if (
        -not [IO.Path]::GetFileName($full).Equals(
            "new_horizon.sqlite3",
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [IO.Path]::GetFileName(
            [IO.Path]::GetDirectoryName($full)
        ).Equals(
            "_new_horizon_history",
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "$Label must end in _new_horizon_history\new_horizon.sqlite3. Got: $PathValue"
    }
    return $full
}
