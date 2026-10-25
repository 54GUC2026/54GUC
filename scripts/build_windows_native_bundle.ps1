param(
    [string]$ReleaseTag = "54GUC",
    [string]$Stamp = "",
    [string]$Profile = "bootstrap",
    [string]$PythonExe = "",
    [string]$SourceCommit = "",
    [string]$ExpectedModel = "",
    [string]$ExpectedSerial = "",
    [string]$ExpectedFirmware = "",
    [string]$DefaultHost = "",
    [string]$DefaultUser = "",
    [string]$DefaultPassword = "",
    [string]$PingInterface = "",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cgf_windows_path_registry.ps1")

if (-not $Stamp) {
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
[void](Assert-CgfWindowsBuildPath -PathValue $RepoRoot -Label "Native build source stage")
Set-Location $RepoRoot

if (-not $PythonExe) {
    $PythonExe = $CgfWindowsPython
}
[void](Assert-CgfWindowsArtifactPath -PathValue $PythonExe -Label "PythonExe")

$SpecPath = Join-Path $RepoRoot "LUC-Windows-Native.spec"
$VenvDir = Join-Path $RepoRoot ".build\windows_native_venv"
$WorkDir = Join-Path $RepoRoot ".build\windows_native_work"
$PyInstallerDist = Join-Path $RepoRoot ".build\windows_native_dist"
$PyExe = Join-Path $VenvDir "Scripts\python.exe"
$BuiltAppDir = Join-Path $PyInstallerDist "54GUC"
$VersionPath = Join-Path $RepoRoot "VERSION"
$UpdateChannelPath = Join-Path $RepoRoot "UPDATE_CHANNEL.json"
$EmbeddedModulePath = Join-Path $RepoRoot "arch4\windows_bundle_embedded.py"
$EmbeddedBackupPath = Join-Path $RepoRoot (".build\windows_bundle_embedded_{0}.bak" -f $Stamp)
$PyInstallerRthooksDat = Join-Path $VenvDir "Lib\site-packages\_pyinstaller_hooks_contrib\rthooks.dat"
$PyInstallerRthooksBackup = Join-Path $RepoRoot (".build\rthooks_{0}.bak" -f $Stamp)

if (-not (Test-Path $SpecPath)) {
    throw "Missing PyInstaller spec: $SpecPath"
}
if (-not (Test-Path $VersionPath)) {
    throw "Missing VERSION file: $VersionPath"
}
if (-not (Test-Path $UpdateChannelPath)) {
    throw "Missing update-channel descriptor: $UpdateChannelPath"
}

function Resolve-WindowsPortableBundleBaseName {
    param(
        [string]$SelectedReleaseTag,
        [string]$SelectedProfile,
        [string]$SelectedStamp
    )

    if ($SelectedProfile -eq "bootstrap") {
        return "{0}_Windows_portable_{1}" -f $SelectedReleaseTag, $SelectedStamp
    }
    return "{0}_Windows_portable_{1}_{2}" -f $SelectedReleaseTag, $SelectedProfile, $SelectedStamp
}

$BundleBaseName = Resolve-WindowsPortableBundleBaseName -SelectedReleaseTag $ReleaseTag -SelectedProfile $Profile -SelectedStamp $Stamp
$StageDir = Join-Path $RepoRoot ("dist\{0}" -f $BundleBaseName)
$ZipOut = Join-Path $RepoRoot ("dist\{0}.zip" -f $BundleBaseName)

function Resolve-PythonBootstrapCommand {
    param([string]$RequestedPythonExe)

    if ($RequestedPythonExe -and $RequestedPythonExe -ne "py") {
        return @{
            Command = $RequestedPythonExe
            Args = @()
            Label = $RequestedPythonExe
        }
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($versionArg in @("-3.13", "-3.12")) {
            & py $versionArg -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '$($versionArg.TrimStart('-'))'.split('.'))) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{
                    Command = "py"
                    Args = @($versionArg)
                    Label = "py $versionArg"
                }
            }
        }
    }

    return @{
        Command = $RequestedPythonExe
        Args = @()
        Label = $RequestedPythonExe
    }
}

function Assert-SupportedWindowsBuildPython {
    param([string]$PythonPath)

    $version = (& $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Python version from $PythonPath"
    }
    $version = ($version | Out-String).Trim()
    if ($version -notin @("3.12", "3.13")) {
        throw "Unsupported Windows bundle Python runtime $version at $PythonPath. Use Python 3.12 or 3.13; do not build on Python 3.14."
    }
}

function Get-GitCurrentBranch {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        return ""
    }
    $branch = (& git -C $RepoRoot branch --show-current 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return ($branch | Out-String).Trim()
}

function Get-GitDirtyLines {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        return @()
    }
    $lines = (& git -C $RepoRoot status --porcelain --untracked-files=all 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    if ($null -eq $lines) {
        return @()
    }
    return @($lines | ForEach-Object { "$_".TrimEnd() } | Where-Object { $_ })
}

function Test-IsAllowedBuildDirtyLine {
    param([string]$Line)
    return $Line -match '(^|\s)(\.build|dist|tmp)[/\\]'
}

function Assert-WorkspaceReadyForWindowsBuild {
    if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
        return
    }

    $branch = Get-GitCurrentBranch
    if (-not $branch) {
        return
    }
    if ($branch -eq "HEAD") {
        throw "Detached HEAD is not allowed for Windows build/release work."
    }
    if ($branch -eq "main") {
        throw "main is stale and must not be used for Windows build/release work."
    }

    $dirty = @(Get-GitDirtyLines | Where-Object { -not (Test-IsAllowedBuildDirtyLine $_) })
    if ($dirty.Count -gt 0) {
        $sample = ($dirty | Select-Object -First 20) -join [Environment]::NewLine
        throw "Workspace is dirty outside .build/dist/tmp and is not safe for Windows build work.`n$sample"
    }
}

Assert-WorkspaceReadyForWindowsBuild

if (-not (Test-Path $VenvDir)) {
    $bootstrap = Resolve-PythonBootstrapCommand -RequestedPythonExe $PythonExe
    Write-Host "Creating Windows build venv with $($bootstrap.Label)"
    & $bootstrap.Command @($bootstrap.Args + @("-m", "venv", $VenvDir))
}

if (-not (Test-Path $PyExe)) {
    throw "Missing Windows build python in $VenvDir"
}

Assert-SupportedWindowsBuildPython -PythonPath $PyExe

function Test-PythonModuleSet {
    param(
        [string]$PythonPath,
        [string[]]$Modules
    )

    $quoted = ($Modules | ForEach-Object { "'$_'" }) -join ", "
    $script = @"
import importlib.util
import sys

modules = [$quoted]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    print(*missing, sep=chr(44))
    raise SystemExit(1)
raise SystemExit(0)
"@

    & $PythonPath -c $script | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Repair-WindowsGuiDependencies {
    param(
        [string]$PythonPath
    )

    & $PythonPath -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Windows build pip installation failed." }
    & $PythonPath -m pip install --force-reinstall -r (Join-Path $RepoRoot "requirements.txt") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Windows build pinned dependency installation failed." }
}

$RequiredGuiModules = @(
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
    "pyqtgraph",
    "requests",
    "urllib3",
    "idna",
    "charset_normalizer",
    "certifi",
    "routeros_api"
)

if (-not $SkipDependencyInstall) {
    Repair-WindowsGuiDependencies -PythonPath $PyExe
} elseif (-not (Test-PythonModuleSet -PythonPath $PyExe -Modules $RequiredGuiModules)) {
    throw "SkipDependencyInstall was requested but the Windows GUI build env is incomplete. Missing one or more required modules: $($RequiredGuiModules -join ', ')"
}

if (-not (Test-PythonModuleSet -PythonPath $PyExe -Modules $RequiredGuiModules)) {
    throw "Windows GUI dependency verification failed after install/repair."
}

# Import success alone is insufficient, including when installation is skipped.
# The constraints file owns versions; this records what the build really loaded.
$RuntimeDependencyCheck = @'
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

resolved = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    name, separator, expected = line.partition("==")
    if not separator or not name or not expected or any(c in expected for c in ";<>* "):
        raise SystemExit("Runtime requirement must have one exact version: " + line)
    try:
        actual = version(name)
    except PackageNotFoundError:
        raise SystemExit("Missing runtime dependency: " + name)
    if actual != expected:
        raise SystemExit(f"Runtime dependency mismatch: {name} expected {expected}, got {actual}")
    resolved[name] = actual
if not resolved:
    raise SystemExit("Runtime requirements are empty")
print(json.dumps(resolved, sort_keys=True))
'@
$RuntimeDependencyJson = $RuntimeDependencyCheck | & $PyExe - (Join-Path $RepoRoot "requirements-runtime-pins.txt")
if ($LASTEXITCODE -ne 0) { throw "Windows runtime dependency versions do not match the tested pins." }
$RuntimeDependencyVersions = $RuntimeDependencyJson | ConvertFrom-Json

function Get-WindowsBundleProfileMetadata {
    param(
        [string]$PythonPath,
        [string]$SelectedProfile
    )

    $script = @'
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arch4.windows_native_launcher import WINDOWS_BUNDLE_PROFILES

profile_name = sys.argv[1].strip().lower() if len(sys.argv) > 1 else str()
profile = WINDOWS_BUNDLE_PROFILES.get(profile_name)
if profile is None:
    print(json.dumps(dict(error=profile_name, supported=sorted(WINDOWS_BUNDLE_PROFILES))))
    raise SystemExit(2)
print(json.dumps(dict(
    name=profile.name,
    backend=profile.backend,
    env_prefix=profile.env_prefix,
    profile_id=profile.profile_id,
    profile_path=profile.profile_path,
)))
'@

    $scriptDir = Join-Path $RepoRoot ".build"
    New-Item -ItemType Directory -Path $scriptDir -Force | Out-Null
    $scriptPath = Join-Path $scriptDir ("windows_bundle_profile_metadata_{0}.py" -f $Stamp)
    $script | Set-Content -Path $scriptPath -Encoding ASCII
    $json = (& $PythonPath $scriptPath $SelectedProfile)
    if ($LASTEXITCODE -ne 0) {
        throw "Unsupported Windows bundle profile '$SelectedProfile'. Supported profiles come from arch4.windows_native_launcher/WINDOWS_BUNDLE_PROFILES."
    }
    return ($json | ConvertFrom-Json)
}

$SelectedProfileMetadata = Get-WindowsBundleProfileMetadata -PythonPath $PyExe -SelectedProfile $Profile

function New-EmbeddedProfileModule {
    param(
        [string]$TargetPath,
        [string]$SelectedProfile,
        [string]$SelectedEnvPrefix,
        [string]$Model,
        [string]$Serial,
        [string]$Firmware,
        [string]$DefaultHostValue,
        [string]$User,
        [string]$Password,
        [string]$PingIface
    )

    if ($SelectedProfile -eq "bootstrap") {
        $profileValues = @{}
        $json = @{ $SelectedProfile = $profileValues } | ConvertTo-Json -Depth 5 -Compress
    } else {
        $prefix = $SelectedEnvPrefix
        $profileValues = @{}

        if ($prefix) {
            if ($Model) { $profileValues["${prefix}_EXPECTED_MODEL"] = $Model }
            if ($Serial) { $profileValues["${prefix}_EXPECTED_SERIAL"] = $Serial }
            if ($Firmware) { $profileValues["${prefix}_EXPECTED_FIRMWARE"] = $Firmware }
            if ($DefaultHostValue) { $profileValues["${prefix}_HOST"] = $DefaultHostValue }
            if ($User) { $profileValues["${prefix}_USER"] = $User }
            if ($Password) { $profileValues["${prefix}_PASS"] = $Password }
        }
        if ($PingIface) { $profileValues["PING_IFACE"] = $PingIface }

        $json = @{ $SelectedProfile = $profileValues } | ConvertTo-Json -Depth 5 -Compress
    }
    @"
import json

EMBEDDED_WINDOWS_PROFILE_VALUES = json.loads(r'''$json''')
"@ | Set-Content -Path $TargetPath -Encoding ASCII
}

function Copy-BetaPackageDocuments {
    param(
        [string]$SelectedRepoRoot,
        [string]$SelectedStageDir
    )

    $requiredDocuments = @(
        "START_HERE.txt",
        "USER_GUIDE.txt",
        "SUPPORTED_HARDWARE_AND_LIMITATIONS.txt",
        "RECOVERY_AND_SUPPORT.txt",
        "BETA_RELEASE_NOTES.txt"
    )
    $sourceDir = Join-Path $SelectedRepoRoot "docs\CGF_MUST_READ\BETA_PACKAGE"
    $targetDir = Join-Path $SelectedStageDir "BETA_PACKAGE"

    if (-not (Test-Path $sourceDir -PathType Container)) {
        throw "Beta package document source is missing: $sourceDir"
    }

    $sourceDocuments = @(
        Get-ChildItem -Path $sourceDir -File | Select-Object -ExpandProperty Name
    )
    $unexpected = @($sourceDocuments | Where-Object { $_ -notin $requiredDocuments })
    if ($unexpected.Count -gt 0) {
        throw "Beta package document source has unexpected files: $($unexpected -join ', ')"
    }

    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    foreach ($name in $requiredDocuments) {
        $sourcePath = Join-Path $sourceDir $name
        if (-not (Test-Path $sourcePath -PathType Leaf)) {
            throw "Required beta package document is missing: $sourcePath"
        }
        Copy-Item $sourcePath (Join-Path $targetDir $name) -Force
    }
}

function New-PortableControlCmd {
    param(
        [string]$CmdPath
    )

    @"
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0\54GUC_Control.ps1" %*
endlocal
"@ | Set-Content -Path $CmdPath -Encoding ASCII
}

function Copy-PortableRuntimeSeed {
    param(
        [string]$SelectedRepoRoot,
        [string]$SelectedStageDir
    )

    $seed = [ordered]@{
        source = "repo_portable_runtime"
        memory_seeded = $false
        memory_files = 0
        config_seeded = $false
        config_files = 0
    }

    $sourcePortableRuntime = Join-Path $SelectedRepoRoot "portable_runtime"
    if (-not (Test-Path $sourcePortableRuntime)) {
        return $seed
    }

    $sourceMemoryDir = Join-Path $sourcePortableRuntime "guc_memory"
    $targetMemoryDir = Join-Path $SelectedStageDir "portable_runtime\guc_memory"
    if (Test-Path $sourceMemoryDir) {
        $memoryFiles = Get-ChildItem -Path $sourceMemoryDir -File -Recurse -ErrorAction SilentlyContinue
        if ($memoryFiles) {
            Copy-Item (Join-Path $sourceMemoryDir "*") $targetMemoryDir -Recurse -Force | Out-Null
            $seed["memory_seeded"] = $true
            $seed["memory_files"] = @($memoryFiles).Count
        }
    }

    $sourceConfigDir = Join-Path $sourcePortableRuntime "config"
    $targetConfigDir = Join-Path $SelectedStageDir "portable_runtime\config"
    if (Test-Path $sourceConfigDir) {
        $configFiles = Get-ChildItem -Path $sourceConfigDir -File -Recurse -ErrorAction SilentlyContinue
        if ($configFiles) {
            Copy-Item (Join-Path $sourceConfigDir "*") $targetConfigDir -Recurse -Force | Out-Null
            $seed["config_seeded"] = $true
            $seed["config_files"] = @($configFiles).Count
        }
    }

    return [pscustomobject]$seed
}

function Copy-PortableModemProfiles {
    param(
        [string]$SelectedRepoRoot,
        [string]$SelectedStageDir
    )

    # Editable input definitions belong beside the EXE. Fixed format codecs
    # are separately frozen next to their ModemLive owner by the sole spec.
    $profileSource = Join-Path $SelectedRepoRoot "modem_profiles"
    $profileDestination = Join-Path $SelectedStageDir "modem_profiles"
    if (-not (Test-Path -LiteralPath $profileSource -PathType Container)) {
        throw "Portable modem profile source is missing."
    }
    if (Test-Path -LiteralPath $profileDestination) {
        throw "Portable modem profile destination already exists."
    }
    $profileFiles = @(Get-ChildItem -LiteralPath $profileSource -Filter "*.json" -File)
    if ($profileFiles.Count -eq 0) {
        throw "Portable modem profile source is empty."
    }
    New-Item -ItemType Directory -Path $profileDestination | Out-Null
    foreach ($profileFile in $profileFiles) {
        Copy-Item -LiteralPath $profileFile.FullName -Destination $profileDestination
    }
    return @{ directory = "modem_profiles"; files = $profileFiles.Count }
}

function Invoke-CgfNativeFreezer {
    param([string]$PythonPath, [string]$SelectedSpec, [string]$DistPath, [string]$WorkPath)

    # Wheel locations are supplied by the freezer. Ambient tool DLLs must not
    # replace Windows dependencies such as the ICU imported by Qt6Core.
    $systemPaths = @(
        [Environment]::GetFolderPath("System"),
        [Environment]::GetFolderPath("Windows")
    )
    foreach ($systemPath in $systemPaths) {
        if (-not $systemPath -or -not (Test-Path -LiteralPath $systemPath -PathType Container)) {
            throw "Native freezer requires existing Windows system directories."
        }
    }
    $previousPath = $env:PATH
    try {
        $env:PATH = $systemPaths -join [IO.Path]::PathSeparator
        & $PythonPath -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath $WorkPath $SelectedSpec
        if ($LASTEXITCODE -ne 0) {
            throw "Native freezer failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        $env:PATH = $previousPath
    }
}

function Disable-PyqtgraphMultiprocessRuntimeHook {
    param(
        [string]$RthooksPath
    )

    if (-not (Test-Path $RthooksPath)) {
        return $false
    }

    $src = Get-Content -Path $RthooksPath -Raw -Encoding UTF8
    $needle = "'pyqtgraph': ['pyi_rth_pyqtgraph_multiprocess.py'],"
    if (-not $src.Contains($needle)) {
        return $false
    }

    Copy-Item $RthooksPath $PyInstallerRthooksBackup -Force
    $patched = $src.Replace($needle, "'pyqtgraph': [],")
    Set-Content -Path $RthooksPath -Value $patched -Encoding ASCII
    return $true
}

Remove-Item $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $PyInstallerDist -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $StageDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $ZipOut -Force -ErrorAction SilentlyContinue
Remove-Item $EmbeddedBackupPath -Force -ErrorAction SilentlyContinue
Remove-Item $PyInstallerRthooksBackup -Force -ErrorAction SilentlyContinue

& $PyExe -m tools.generate_open_source_manifest `
    --repo-root $RepoRoot `
    --output "open_source_manifest.json"
if ($LASTEXITCODE -ne 0) {
    throw "Open-source dependency manifest generation failed."
}

if (Test-Path $EmbeddedModulePath) {
    Copy-Item $EmbeddedModulePath $EmbeddedBackupPath -Force
}

try {
    New-EmbeddedProfileModule `
        -TargetPath $EmbeddedModulePath `
        -SelectedProfile $Profile `
        -SelectedEnvPrefix $SelectedProfileMetadata.env_prefix `
        -Model $ExpectedModel `
        -Serial $ExpectedSerial `
        -Firmware $ExpectedFirmware `
        -DefaultHostValue $DefaultHost `
        -User $DefaultUser `
        -Password $DefaultPassword `
        -PingIface $PingInterface

    $null = Disable-PyqtgraphMultiprocessRuntimeHook -RthooksPath $PyInstallerRthooksDat

    Invoke-CgfNativeFreezer -PythonPath $PyExe -SelectedSpec $SpecPath -DistPath $PyInstallerDist -WorkPath $WorkDir
}
finally {
    if (Test-Path $EmbeddedBackupPath) {
        Move-Item -Force $EmbeddedBackupPath $EmbeddedModulePath
    } else {
        Remove-Item $EmbeddedModulePath -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $PyInstallerRthooksBackup) {
        Move-Item -Force $PyInstallerRthooksBackup $PyInstallerRthooksDat
    }
}

if (-not (Test-Path $BuiltAppDir)) {
    throw "PyInstaller did not produce $BuiltAppDir"
}

foreach ($requiredPath in @(
    (Join-Path $BuiltAppDir "_internal\PySide6\QtCore.pyd"),
    (Join-Path $BuiltAppDir "_internal\PySide6\QtGui.pyd"),
    (Join-Path $BuiltAppDir "_internal\PySide6\QtWidgets.pyd")
)) {
    if (-not (Test-Path $requiredPath)) {
        throw "Frozen Windows bundle is missing a required Qt module: $requiredPath"
    }
}

Copy-Item $BuiltAppDir $StageDir -Recurse
$PortableModemProfiles = Copy-PortableModemProfiles -SelectedRepoRoot $RepoRoot -SelectedStageDir $StageDir
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "support_bundles") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime\config") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime\guc_output") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime\guc_memory") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime\launcher_logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "portable_runtime\launcher_state") | Out-Null
$PortableRuntimeSeed = Copy-PortableRuntimeSeed -SelectedRepoRoot $RepoRoot -SelectedStageDir $StageDir
Copy-Item (Join-Path $RepoRoot "scripts\windows_portable_control.ps1") (Join-Path $StageDir "54GUC_Control.ps1") -Force
New-PortableControlCmd -CmdPath (Join-Path $StageDir "54GUC_Control.cmd")

$VersionText = (Get-Content -Path $VersionPath -Raw -Encoding UTF8).Trim()
$ReleaseChannelPath = Join-Path $RepoRoot "RELEASE_CHANNEL"
$ReleaseChannelText = "mainline"
if (Test-Path $ReleaseChannelPath) {
    $candidateChannel = (Get-Content -Path $ReleaseChannelPath -Raw -Encoding UTF8).Trim().ToLowerInvariant()
    if ($candidateChannel) {
        $ReleaseChannelText = $candidateChannel
    }
}
$BuildDateText = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

$manifest = [ordered]@{
    portable_modem_profiles = $PortableModemProfiles
    release_tag = $ReleaseTag
    stamp = $Stamp
    profile = $Profile
    bundle_type = "windows_native"
    bundle_layout = "54GUC_portable_v1"
    launcher = "54GUC.exe"
    version = $VersionText
    build_date = $BuildDateText
    release_channel = $ReleaseChannelText
    commit = $SourceCommit
    source_commit = $SourceCommit
    runtime_dependencies = $RuntimeDependencyVersions
    portable_runtime_seed = $PortableRuntimeSeed
}
$ManifestJson = $manifest | ConvertTo-Json -Depth 6
$ManifestJson | Set-Content -Path (Join-Path $BuiltAppDir "version_manifest.json") -Encoding ASCII
$ManifestJson | Set-Content -Path (Join-Path $StageDir "version_manifest.json") -Encoding ASCII
$VersionText | Set-Content -Path (Join-Path $BuiltAppDir "VERSION") -Encoding ASCII
$VersionText | Set-Content -Path (Join-Path $StageDir "VERSION") -Encoding ASCII
$ReleaseChannelText | Set-Content -Path (Join-Path $BuiltAppDir "RELEASE_CHANNEL") -Encoding ASCII
$ReleaseChannelText | Set-Content -Path (Join-Path $StageDir "RELEASE_CHANNEL") -Encoding ASCII
Copy-Item (Join-Path $RepoRoot "open_source_manifest.json") (Join-Path $BuiltAppDir "open_source_manifest.json") -Force
Copy-Item (Join-Path $RepoRoot "open_source_manifest.json") (Join-Path $StageDir "open_source_manifest.json") -Force
Copy-Item $UpdateChannelPath (Join-Path $BuiltAppDir "UPDATE_CHANNEL.json") -Force
Copy-Item $UpdateChannelPath (Join-Path $StageDir "UPDATE_CHANNEL.json") -Force

Copy-BetaPackageDocuments -SelectedRepoRoot $RepoRoot -SelectedStageDir $StageDir

Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipOut -Force

& $PyExe -m arch4.windows_bundle_validation --stage-dir $StageDir --bundle-zip $ZipOut
if ($LASTEXITCODE -ne 0) {
    throw "Windows portable bundle validation failed."
}

Write-Host "Built native Windows stage:"
Write-Host "  $StageDir"
Write-Host "  $ZipOut"
