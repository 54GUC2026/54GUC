param(
    [string]$SourceRepo = "",
    [string]$Ref = "HEAD",
    [string]$StagingRoot = "",
    [string]$Stamp = "",
    [ValidateSet("Build", "FullTest")]
    [string]$Purpose = "Build",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cgf_windows_path_registry.ps1")

if (-not $Stamp) {
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
}

if (-not $SourceRepo) {
    $SourceRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not $StagingRoot) {
    $StagingRoot = if ($Purpose -eq "FullTest") {
        $CgfWindowsFullTestStageRoot
    } else {
        $CgfWindowsBuildStagingRoot
    }
}
if ($Purpose -eq "FullTest") {
    [void](Assert-CgfWindowsFullTestStagePath -PathValue $StagingRoot -Label "Full-test StagingRoot")
} else {
    [void](Assert-CgfWindowsBuildPath -PathValue $StagingRoot -Label "Build StagingRoot")
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

function Convert-WslUncPath {
    param([string]$PathValue)

    if ($PathValue -notmatch '^\\\\wsl(?:\.localhost)?\\([^\\]+)\\?(.*)$') {
        return $null
    }

    $distro = $Matches[1]
    $rest = $Matches[2]
    $linux = "/"
    if ($rest) {
        $linux += ($rest -replace '\\', '/')
    }
    return [pscustomobject]@{
        distro = $distro
        linux_path = $linux
    }
}

function Resolve-WslDistroName {
    param([string]$RequestedDistro)

    if ($RequestedDistro) {
        return $RequestedDistro
    }
    $names = @(
        & wsl.exe -l -q | ForEach-Object {
            $_.Replace([string][char]0, "").Trim()
        } | Where-Object { $_ }
    )
    if ($LASTEXITCODE -ne 0 -or $names.Count -eq 0) {
        throw "Unable to resolve the default WSL distro."
    }
    return [string]$names[0]
}

function Convert-WslLinuxPathToUnc {
    param(
        [string]$Distro,
        [string]$LinuxPath
    )

    if (-not $Distro -or -not $LinuxPath.StartsWith("/")) {
        throw "A WSL distro and absolute Linux path are required."
    }
    $relative = $LinuxPath.TrimStart("/") -replace '/', '\'
    return "\\wsl.localhost\$Distro\$relative"
}

function Invoke-WindowsGit {
    param(
        [string[]]$Arguments
    )

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git failed: git $($Arguments -join ' ')"
    }
}

function Invoke-WslCommand {
    param(
        [string]$Distro,
        [string[]]$Arguments
    )

    $full = @()
    if ($Distro) {
        $full += @("-d", $Distro)
    }
    $full += @("-e") + $Arguments
    & wsl.exe @full
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed for distro ${Distro}: $($Arguments -join ' ')"
    }
}

function Remove-WslTempArchive {
    param(
        [string]$Distro,
        [string]$ArchiveLinuxPath
    )

    $args = @()
    if ($Distro) {
        $args += @("-d", $Distro)
    }
    $args += @("-e", "rm", "-f", "--", $ArchiveLinuxPath)
    & wsl.exe @args
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Unable to remove temporary WSL source archive: $ArchiveLinuxPath"
    }
}

Assert-LocalWindowsPath -PathValue $StagingRoot -Label "StagingRoot"

$BuildRoot = Join-Path $StagingRoot ("54GUC_windows_build_{0}" -f $Stamp)
$SourceStage = Join-Path $BuildRoot "src"
$ArchivePath = Join-Path $BuildRoot "source_snapshot.tar"
$ManifestPath = Join-Path $BuildRoot "export_manifest.json"

if ((Test-Path $BuildRoot) -and (-not $Force)) {
    throw "Build root already exists: $BuildRoot (use -Force to replace it)."
}

Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $SourceStage | Out-Null

$resolvedSourceRepo = $SourceRepo
$sourceKind = "windows"
$distro = ""
$linuxSourcePath = ""
$commit = ""
$branch = ""

$wslSpec = Convert-WslUncPath -PathValue $SourceRepo
if (($null -eq $wslSpec) -and ($SourceRepo -match '^/')) {
    $wslSpec = [pscustomobject]@{
        distro = ""
        linux_path = $SourceRepo
    }
}
if ($null -eq $wslSpec) {
    $resolvedSourceRepo = (Resolve-Path $SourceRepo).Path
    Assert-LocalWindowsPath -PathValue $resolvedSourceRepo -Label "SourceRepo"
}
if ($null -ne $wslSpec) {
    $sourceKind = "wsl"
    $distro = Resolve-WslDistroName -RequestedDistro $wslSpec.distro
    $linuxSourcePath = $wslSpec.linux_path
    $wslBase = @()
    if ($distro) {
        $wslBase += @("-d", $distro)
    }
    $commit = (& wsl.exe @wslBase -e git -C $linuxSourcePath rev-parse $Ref | Out-String).Trim()
    if (-not $commit) {
        throw "Unable to resolve git commit for $SourceRepo at $Ref"
    }
    $branch = (& wsl.exe @wslBase -e git -C $linuxSourcePath branch --show-current | Out-String).Trim()
    $archiveLinux = "/tmp/54guc_source_snapshot_{0}_{1}.tar" -f $Stamp, [Guid]::NewGuid().ToString("N")
    try {
        Invoke-WslCommand -Distro $distro -Arguments @(
            "git", "-C", $linuxSourcePath, "archive", "--format=tar", "-o", $archiveLinux, $commit
        )
        $archiveUnc = Convert-WslLinuxPathToUnc -Distro $distro -LinuxPath $archiveLinux
        if (-not (Test-Path -LiteralPath $archiveUnc -PathType Leaf)) {
            throw "WSL source archive is missing at its Windows boundary: $archiveUnc"
        }
        Copy-Item -LiteralPath $archiveUnc -Destination $ArchivePath -Force
    }
    finally {
        Remove-WslTempArchive -Distro $distro -ArchiveLinuxPath $archiveLinux
    }
} else {
    $commit = (& git -C $resolvedSourceRepo rev-parse $Ref | Out-String).Trim()
    if (-not $commit) {
        throw "Unable to resolve git commit for $resolvedSourceRepo at $Ref"
    }
    $branch = (& git -C $resolvedSourceRepo branch --show-current | Out-String).Trim()
    Invoke-WindowsGit -Arguments @(
        "-C", $resolvedSourceRepo, "archive", "--format=tar", ("--output={0}" -f $ArchivePath), $commit
    )
}

if ($ArchivePath) {
    tar.exe -xf $ArchivePath -C $SourceStage
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract $ArchivePath into $SourceStage"
    }
    Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue
}

$manifest = [pscustomobject]@{
    source_kind = $sourceKind
    source_repo = $resolvedSourceRepo
    source_ref = $Ref
    source_commit = $commit
    source_branch = $branch
    wsl_distro = $distro
    wsl_source_path = $linuxSourcePath
    build_root = $BuildRoot
    source_stage = $SourceStage
    stamp = $Stamp
    snapshot_mode = "git_archive"
    exported_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

$manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $ManifestPath -Encoding ASCII
$manifest
