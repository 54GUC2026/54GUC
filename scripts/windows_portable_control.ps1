param(
    [ValidateSet("bootstrap", "build", "launch", "status", "stop", "validate", "logs", "bundle", "plan")]
    [string]$Action = "status",
    [string]$StageDir = "",
    [string]$StagingRoot = "",
    [string]$OutputRoot = "",
    [string]$SourceRepo = "",
    [string]$Ref = "HEAD",
    [string]$Stamp = "",
    [string]$PythonExe = "",
    [string]$ReleaseTag = "54GUC",
    [string]$HistoryPath = "",
    [switch]$SkipDependencyInstall,
    [switch]$SkipBundleValidation,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

if (-not $Stamp) {
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
}

$ScriptPath = (Resolve-Path $MyInvocation.MyCommand.Path).Path
$ScriptDir = Split-Path -Parent $ScriptPath
$SelfBundleDir = if (Test-Path (Join-Path $ScriptDir "54GUC.exe")) { $ScriptDir } else { "" }

function Assert-NotDisplacedCodexPath {
    param([string]$PathValue, [string]$Label)
    if ($PathValue -and $PathValue -match '(?i)^C:\\(?:54b|b)(?:\\|$)') {
        throw "$Label uses a retired Codex path: $PathValue"
    }
}

$pathRegistry = Join-Path $ScriptDir "cgf_windows_path_registry.ps1"
$hasPathRegistry = Test-Path -LiteralPath $pathRegistry -PathType Leaf
if ($hasPathRegistry) {
    . $pathRegistry
}

if ($Action -in @("bootstrap", "build")) {
    if (-not $hasPathRegistry) {
        throw "Current Windows path registry is missing: $pathRegistry"
    }
    if (-not $OutputRoot) { $OutputRoot = $CgfWindowsArtifactRoot }
    if (-not $StagingRoot) { $StagingRoot = $CgfWindowsBuildStagingRoot }
    if (-not $PythonExe) { $PythonExe = $CgfWindowsPython }
    [void](Assert-CgfWindowsArtifactPath -PathValue $OutputRoot -Label "OutputRoot")
    [void](Assert-CgfWindowsBuildPath -PathValue $StagingRoot -Label "StagingRoot")
    [void](Assert-CgfWindowsArtifactPath -PathValue $PythonExe -Label "PythonExe")
} elseif (-not $OutputRoot) {
    $OutputRoot = if ($hasPathRegistry) {
        $CgfWindowsArtifactRoot
    } else {
        Join-Path $env:LOCALAPPDATA "54GUC\portable_dist"
    }
}
Assert-NotDisplacedCodexPath -PathValue $OutputRoot -Label "OutputRoot"
Assert-NotDisplacedCodexPath -PathValue $StagingRoot -Label "StagingRoot"
Assert-NotDisplacedCodexPath -PathValue $StageDir -Label "StageDir"
Assert-NotDisplacedCodexPath -PathValue $PythonExe -Label "PythonExe"
Assert-NotDisplacedCodexPath -PathValue $HistoryPath -Label "HistoryPath"

$selectedLaunchHistoryPath = ""
if (-not [string]::IsNullOrWhiteSpace($HistoryPath)) {
    if ($Action -ne "launch") {
        throw "HistoryPath is accepted only for a connected portable launch."
    }
    if (-not $hasPathRegistry) {
        throw "Connected portable History validation requires the current Windows path registry."
    }
    $selectedLaunchHistoryPath = Assert-CgfWindowsHistoryPath `
        -PathValue $HistoryPath `
        -Label "Connected portable History"
    if (-not (Test-Path -LiteralPath $selectedLaunchHistoryPath -PathType Leaf)) {
        throw "Expected connected-portable History store is missing: $selectedLaunchHistoryPath"
    }
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

function Resolve-LatestStageDir {
    param([string]$BaseOutputRoot)

    Assert-LocalWindowsPath -PathValue $BaseOutputRoot -Label "OutputRoot"
    if (-not (Test-Path $BaseOutputRoot)) {
        throw "OutputRoot does not exist: $BaseOutputRoot"
    }
    $stage = Get-ChildItem -Path $BaseOutputRoot -Directory -Filter "54GUC_Windows_portable_*" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $stage) {
        throw "No Windows portable stages found under $BaseOutputRoot"
    }
    return $stage.FullName
}

function Resolve-StageDir {
    param(
        [string]$RequestedStageDir,
        [string]$BaseOutputRoot
    )

    if ($RequestedStageDir) {
        $resolved = (Resolve-Path $RequestedStageDir).Path
        Assert-LocalWindowsPath -PathValue $resolved -Label "StageDir"
        return $resolved
    }
    if ($SelfBundleDir) {
        return $SelfBundleDir
    }
    return (Resolve-LatestStageDir -BaseOutputRoot $BaseOutputRoot)
}

function Resolve-SourceRoot {
    param([string]$RequestedSourceRepo)

    if ($RequestedSourceRepo) {
        return $RequestedSourceRepo
    }
    $candidate = Resolve-Path (Join-Path $ScriptDir "..") -ErrorAction SilentlyContinue
    if ($null -eq $candidate) {
        throw "SourceRepo is required for build/bootstrap actions."
    }
    return $candidate.Path
}

function Get-StagePaths {
    param([string]$ResolvedStageDir)

    $runtimeRoot = Join-Path $ResolvedStageDir "portable_runtime"
    $launcherLogs = Join-Path $runtimeRoot "launcher_logs"
    $launcherState = Join-Path $runtimeRoot "launcher_state"
    $lifecycleRoot = Join-Path $env:LOCALAPPDATA "54GUC\application_lifecycle"
    [pscustomobject]@{
        stage_dir = $ResolvedStageDir
        exe_path = Join-Path $ResolvedStageDir "54GUC.exe"
        manifest_path = Join-Path $ResolvedStageDir "version_manifest.json"
        runtime_root = $runtimeRoot
        runtime_output = Join-Path $runtimeRoot "guc_output"
        runtime_memory = Join-Path $runtimeRoot "guc_memory"
        launcher_logs = $launcherLogs
        launcher_state = $launcherState
        control_state = Join-Path $launcherState "control_state.json"
        application_lifecycle_root = $lifecycleRoot
        application_lease = Join-Path $lifecycleRoot "application_lease.json"
        ngui_page_truth = Join-Path (Join-Path $runtimeRoot "guc_output") "ngui_page_truth.json"
    }
}

function Ensure-StageRuntimeLayout {
    param([object]$Paths)

    foreach ($path in @($Paths.runtime_root, $Paths.runtime_output, $Paths.runtime_memory, $Paths.launcher_logs, $Paths.launcher_state, $Paths.application_lifecycle_root)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }
}

function Read-JsonFile {
    param([string]$PathValue)

    if (-not (Test-Path $PathValue)) {
        return @{}
    }
    try {
        return Get-Content -Path $PathValue -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return @{}
    }
}

function Read-LiveJsonFile {
    param(
        [string]$PathValue,
        [int]$Attempts = 8,
        [int]$RetryBaseMilliseconds = 30
    )

    if (-not (Test-Path $PathValue)) {
        return @{}
    }
    $lastError = $null
    $boundedAttempts = [Math]::Max(1, $Attempts)
    for ($attempt = 0; $attempt -lt $boundedAttempts; $attempt++) {
        try {
            return Get-Content -Path $PathValue -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            $lastError = $_
            if ($attempt + 1 -lt $boundedAttempts) {
                $delay = [int]($RetryBaseMilliseconds * [Math]::Pow(2, $attempt))
                Start-Sleep -Milliseconds $delay
            }
        }
    }
    throw "Live JSON read failed after $boundedAttempts attempts: $PathValue`: $lastError"
}

function Test-ApplicationLeaseReceipt {
    param(
        [object]$Lease
    )

    if ($Lease -isnot [System.Management.Automation.PSCustomObject]) {
        return $false
    }
    $state = [string]$Lease.state
    $terminalState = $state -in @("stopped", "failed")
    return (
        [string]$Lease.schema -eq "ApplicationLeaseReceipt.v1" -and
        [string]$Lease.owner -eq "54guc.application_lease" -and
        [string]$Lease.lease_id -and
        [int64]$Lease.generation -gt 0 -and
        [int]$Lease.process_id -gt 0 -and
        [int64]$Lease.process_started_at_unix_ms -gt 0 -and
        [string]$Lease.profile -and
        [string]$Lease.executable_path -and
        [string]$Lease.stage_dir -and
        $state -in @("claimed", "starting", "process_running", "product_ready", "stopping", "stopped", "failed") -and
        [int64]$Lease.sequence -gt 0 -and
        [double]$Lease.claimed_at -gt 0.0 -and
        [double]$Lease.updated_at -ge [double]$Lease.claimed_at -and
        [bool]$Lease.terminal -eq $terminalState
    )
}

function Read-ApplicationLeaseReceipt {
    param([object]$Paths)

    if (-not (Test-Path -LiteralPath $Paths.application_lease)) {
        return $null
    }
    $lease = Read-LiveJsonFile -PathValue $Paths.application_lease
    if (-not (Test-ApplicationLeaseReceipt -Lease $lease)) {
        throw "Authoritative ApplicationLease receipt is corrupt or incomplete: $($Paths.application_lease)"
    }
    return $lease
}

function Resolve-LatestNguiPageTruthPath {
    param([object]$Paths)

    $candidates = @()
    if (Test-Path $Paths.ngui_page_truth) {
        $candidates += Get-Item $Paths.ngui_page_truth -ErrorAction SilentlyContinue
    }
    if (Test-Path $Paths.runtime_root) {
        $runtimeDirs = @(Get-ChildItem -Path $Paths.runtime_root -Directory -Filter "guc_output*" -ErrorAction SilentlyContinue)
        foreach ($dir in $runtimeDirs) {
            $candidate = Join-Path $dir.FullName "ngui_page_truth.json"
            if (Test-Path $candidate) {
                $candidates += Get-Item $candidate -ErrorAction SilentlyContinue
            }
        }
    }
    $latest = $candidates | Where-Object { $null -ne $_ } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $latest) {
        return $Paths.ngui_page_truth
    }
    return $latest.FullName
}

function Get-StageManifest {
    param([object]$Paths)

    $manifest = Read-JsonFile -PathValue $Paths.manifest_path
    if ($manifest -is [System.Management.Automation.PSCustomObject]) {
        return $manifest
    }
    return [pscustomobject]@{}
}

function Write-ControlLog {
    param(
        [object]$Paths,
        [string]$Message
    )

    Ensure-StageRuntimeLayout -Paths $Paths
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path (Join-Path $Paths.launcher_logs "control.log") -Value ("[{0}] {1}" -f $stamp, $Message) -Encoding ASCII
}

function Enter-StageLifecycleMutex {
    param(
        [string]$ResolvedStageDir,
        [int]$TimeoutSeconds = 30
    )

    $mutex = [System.Threading.Mutex]::new($false, "Local\54GUC-application-lifecycle")
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds([Math]::Max(1, $TimeoutSeconds)))
    }
    catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
    }
    if (-not $acquired) {
        $mutex.Dispose()
        throw "Timed out waiting for the application lifecycle operation lock: $ResolvedStageDir"
    }
    return $mutex
}

function Exit-StageLifecycleMutex {
    param([System.Threading.Mutex]$Mutex)

    if ($null -eq $Mutex) {
        return
    }
    try {
        $Mutex.ReleaseMutex()
    }
    finally {
        $Mutex.Dispose()
    }
}

function Get-PortableProcess {
    param(
        [string]$ExePath,
        [int]$ProcessId,
        [long]$ProcessStartedAtUnixMs
    )

    if ($ProcessId -le 0 -or $ProcessStartedAtUnixMs -le 0) {
        return $null
    }
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $proc -or [string]$proc.ExecutablePath -ne $ExePath) {
        return $null
    }
    $created = ([DateTimeOffset]([DateTime]$proc.CreationDate).ToUniversalTime()).ToUnixTimeMilliseconds()
    if ([long]$created -ne [long]$ProcessStartedAtUnixMs) {
        return $null
    }
    return $proc
}

function Wait-ExactPortableProcessExit {
    param(
        [string]$ExePath,
        [int]$ProcessId,
        [long]$ProcessStartedAtUnixMs,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(0, $TimeoutSeconds))
    do {
        $proc = Get-PortableProcess `
            -ExePath $ExePath `
            -ProcessId $ProcessId `
            -ProcessStartedAtUnixMs $ProcessStartedAtUnixMs
        if ($null -eq $proc) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-ApplicationLeaseForProcess {
    param(
        [object]$Paths,
        [int]$ProcessId,
        [int]$TimeoutSeconds = 10
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        $lease = Read-ApplicationLeaseReceipt -Paths $Paths
        if (
            $null -ne $lease -and
            [int]$lease.process_id -eq $ProcessId -and
            [string]$lease.executable_path -eq [string]$Paths.exe_path -and
            [string]$lease.stage_dir -eq [string]$Paths.stage_dir -and
            [string]$lease.state -in @("claimed", "starting", "process_running", "product_ready", "stopping")
        ) {
            $proc = Get-PortableProcess `
                -ExePath $Paths.exe_path `
                -ProcessId $ProcessId `
                -ProcessStartedAtUnixMs ([long]$lease.process_started_at_unix_ms)
            if ($null -ne $proc) {
                return $lease
            }
        }
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $null
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Wait-MatchingApplicationLeaseTerminal {
    param(
        [object]$Paths,
        [string]$LeaseId,
        [long]$Generation,
        [int]$TimeoutSeconds = 5
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(0, $TimeoutSeconds))
    do {
        $lease = Read-ApplicationLeaseReceipt -Paths $Paths
        if ($null -ne $lease) {
            if (
                [string]$lease.lease_id -ne $LeaseId -or
                [long]$lease.generation -ne $Generation
            ) {
                throw "ApplicationLease identity changed before matching terminal receipt."
            }
            if ([string]$lease.state -in @("stopped", "failed")) {
                return $lease
            }
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Remove-ControlStateArtifacts {
    param([object]$Paths)

    Remove-Item $Paths.control_state -Force -ErrorAction SilentlyContinue
}

function Get-StageStatusObject {
    param([object]$Paths)

    $manifest = Get-StageManifest -Paths $Paths
    $lease = Read-ApplicationLeaseReceipt -Paths $Paths
    $leaseId = if ($null -ne $lease) { [string]$lease.lease_id } else { "" }
    $processId = if ($null -ne $lease) { [int]($lease.process_id) } else { 0 }
    $processStartedAtUnixMs = if ($null -ne $lease) { [long]($lease.process_started_at_unix_ms) } else { 0 }
    $leaseState = if ($null -ne $lease) { [string]$lease.state } else { "" }
    $leaseActive = (
        $null -ne $lease -and
        $leaseState -in @("claimed", "starting", "process_running", "product_ready", "stopping")
    )
    $proc = $null
    if ($leaseActive) {
        $proc = Get-PortableProcess `
            -ExePath ([string]$lease.executable_path) `
            -ProcessId $processId `
            -ProcessStartedAtUnixMs $processStartedAtUnixMs
    }
    $pageTruthPath = Resolve-LatestNguiPageTruthPath -Paths $Paths
    $pageTruth = @{}
    $pageTruthError = ""
    if (Test-Path -LiteralPath $pageTruthPath) {
        try {
            $pageTruth = Read-LiveJsonFile -PathValue $pageTruthPath
        }
        catch {
            $pageTruthError = [string]$_
        }
    }
    $pageLease = if (
        $pageTruth -is [System.Management.Automation.PSCustomObject] -and
        $pageTruth.PSObject.Properties.Name -contains "application_lease"
    ) { $pageTruth.application_lease } else { $null }
    $pageTruthBound = (
        $null -ne $lease -and
        $pageLease -is [System.Management.Automation.PSCustomObject] -and
        [string]$pageLease.lease_id -eq $leaseId -and
        [long]$pageLease.generation -eq [long]$lease.generation
    )
    $requestedStageOwnsLease = (
        $null -ne $lease -and
        [string]$lease.stage_dir -eq [string]$Paths.stage_dir -and
        [string]$lease.executable_path -eq [string]$Paths.exe_path
    )
    [pscustomobject]@{
        stage_dir = $Paths.stage_dir
        exe_path = $Paths.exe_path
        version = [string]($manifest.version)
        profile = [string]($manifest.profile)
        running = ($null -ne $proc -and $requestedStageOwnsLease)
        global_application_running = ($null -ne $proc)
        lease_stage_matches_request = $requestedStageOwnsLease
        process_id = if ($null -ne $proc -and $requestedStageOwnsLease) { [int]$proc.ProcessId } else { 0 }
        process_started_at_unix_ms = $processStartedAtUnixMs
        lease_id = $leaseId
        lease_generation = if ($null -ne $lease) { [long]($lease.generation) } else { 0 }
        lease_state = $leaseState
        lease_stage_dir = if ($null -ne $lease) { [string]$lease.stage_dir } else { "" }
        lease_sequence = if (
            $null -ne $lease -and
            $lease.PSObject.Properties.Name -contains "sequence"
        ) { [int]($lease.sequence) } else { 0 }
        lease_receipt = $Paths.application_lease
        lease_receipt_valid = ($null -ne $lease)
        lease_identity_valid = ($null -ne $proc)
        ngui_page_truth_path = $pageTruthPath
        ngui_page_truth_exists = (Test-Path $pageTruthPath)
        ngui_page_truth_valid = (
            (Test-Path -LiteralPath $pageTruthPath) -and
            (-not $pageTruthError)
        )
        ngui_page_truth_error = $pageTruthError
        ngui_page_truth_lease_bound = $pageTruthBound
        ngui_page_truth_sequence = if (
            $pageTruth -is [System.Management.Automation.PSCustomObject] -and
            $pageTruth.PSObject.Properties.Name -contains "sequence"
        ) { [int]($pageTruth.sequence) } else { 0 }
    }
}

function Write-StatusOutput {
    param(
        [object]$Status,
        [switch]$AsJson
    )

    if ($AsJson) {
        $Status | ConvertTo-Json -Depth 5
        return
    }

    Write-Host "54GUC Windows Portable Status"
    Write-Host "  Stage:           $($Status.stage_dir)"
    Write-Host "  EXE:             $($Status.exe_path)"
    Write-Host "  Version:         $($Status.version)"
    Write-Host "  Profile:         $($Status.profile)"
    Write-Host "  Running:         $($Status.running)"
    if ($Status.running) {
        Write-Host "  PID:             $($Status.process_id)"
    }
    if ($Status.lease_id) {
        Write-Host "  Lease ID:        $($Status.lease_id)"
        Write-Host "  Lease state:     $($Status.lease_state)"
        Write-Host "  Lease generation:$($Status.lease_generation)"
    }
    Write-Host "  Lease identity:  $($Status.lease_identity_valid)"
    Write-Host "  Lease receipt:   $($Status.lease_receipt_valid)"
    Write-Host "  NGUI page truth: $($Status.ngui_page_truth_exists)"
    if ($Status.ngui_page_truth_exists) {
        Write-Host "  Page truth path: $($Status.ngui_page_truth_path)"
        Write-Host "  Page sequence:   $($Status.ngui_page_truth_sequence)"
    }
}

function Invoke-BootstrapCheck {
    param(
        [string]$ResolvedSourceRepo,
        [string]$BaseOutputRoot,
        [string]$SelectedPythonExe,
        [switch]$AsJson
    )

    $bootstrapScript = Join-Path $ScriptDir "windows_native_bootstrap.ps1"
    if (-not (Test-Path $bootstrapScript)) {
        throw "Missing bootstrap script: $bootstrapScript"
    }
    $args = @{
        OutputRoot = $BaseOutputRoot
        PythonExe = $SelectedPythonExe
        RequireAll = $true
    }
    if ($ResolvedSourceRepo) {
        $args["SourceRepo"] = $ResolvedSourceRepo
    }
    if ($AsJson) {
        $args["Json"] = $true
    }
    $bootstrapResult = & $bootstrapScript @args
    if ($null -eq $bootstrapResult) {
        throw "Bootstrap checks failed."
    }
    if ($bootstrapResult -is [System.Array]) {
        $bootstrapResult = $bootstrapResult | Select-Object -Last 1
    }
    if (-not $bootstrapResult.ok) {
        throw "Bootstrap checks failed."
    }
    return $bootstrapResult
}

function Invoke-BuildAction {
    param(
        [string]$ResolvedSourceRepo,
        [string]$ResolvedStagingRoot,
        [string]$BaseOutputRoot,
        [string]$SelectedRef,
        [string]$SelectedStamp,
        [string]$SelectedPythonExe,
        [string]$SelectedReleaseTag,
        [switch]$SelectedSkipDependencyInstall,
        [switch]$SelectedSkipBundleValidation
    )

    $buildScript = Join-Path $ScriptDir "build_windows_portable.ps1"
    if (-not (Test-Path $buildScript)) {
        throw "build action is only available from the source repo scripts directory."
    }
    Invoke-BootstrapCheck -ResolvedSourceRepo $ResolvedSourceRepo -BaseOutputRoot $BaseOutputRoot -SelectedPythonExe $SelectedPythonExe
    $args = @{
        SourceRepo = $ResolvedSourceRepo
        Ref = $SelectedRef
        OutputRoot = $BaseOutputRoot
        ReleaseTag = $SelectedReleaseTag
        Stamp = $SelectedStamp
    }
    if ($ResolvedStagingRoot) {
        $args["StagingRoot"] = $ResolvedStagingRoot
    }
    if ($SelectedPythonExe) {
        $args["PythonExe"] = $SelectedPythonExe
    }
    if ($SelectedSkipDependencyInstall) {
        $args["SkipDependencyInstall"] = $true
    }
    if ($SelectedSkipBundleValidation) {
        $args["SkipBundleValidation"] = $true
    }
    & $buildScript @args
    if ($LASTEXITCODE -ne 0) {
        throw "Windows portable build failed."
    }
}

function Invoke-PortableValidation {
    param([object]$Paths)

    $testScript = Join-Path $ScriptDir "test_windows_portable.ps1"
    if (Test-Path $testScript) {
        & $testScript -StageDir $Paths.stage_dir
        if ($LASTEXITCODE -ne 0) {
            throw "Windows portable validation failed for $($Paths.stage_dir)"
        }
        return
    }

    if (-not (Test-Path $Paths.exe_path)) {
        throw "Missing portable executable: $($Paths.exe_path)"
    }
    $planJson = (& $Paths.exe_path --print-launch-plan | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "54GUC.exe --print-launch-plan failed."
    }
    $null = $planJson | ConvertFrom-Json
    & $Paths.exe_path --portable-validate | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "54GUC.exe --portable-validate failed."
    }
    $validationPack = Get-ChildItem -Path $Paths.runtime_root -Directory -Filter "validation_*" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $validationPack) {
        throw "No validation_* artifact pack was created under $($Paths.runtime_root)"
    }
}

switch ($Action) {
    "bootstrap" {
        $resolvedSource = Resolve-SourceRoot -RequestedSourceRepo $SourceRepo
        Invoke-BootstrapCheck -ResolvedSourceRepo $resolvedSource -BaseOutputRoot $OutputRoot -SelectedPythonExe $PythonExe -AsJson:$Json
        break
    }

    "build" {
        $resolvedSource = Resolve-SourceRoot -RequestedSourceRepo $SourceRepo
        Invoke-BuildAction `
            -ResolvedSourceRepo $resolvedSource `
            -ResolvedStagingRoot $StagingRoot `
            -BaseOutputRoot $OutputRoot `
            -SelectedRef $Ref `
            -SelectedStamp $Stamp `
            -SelectedPythonExe $PythonExe `
            -SelectedReleaseTag $ReleaseTag `
            -SelectedSkipDependencyInstall:$SkipDependencyInstall `
            -SelectedSkipBundleValidation:$SkipBundleValidation
        break
    }
}

if ($Action -in @("bootstrap", "build")) {
    return
}

$resolvedStage = Resolve-StageDir -RequestedStageDir $StageDir -BaseOutputRoot $OutputRoot
Assert-LocalWindowsPath -PathValue $resolvedStage -Label "StageDir"
$paths = Get-StagePaths -ResolvedStageDir $resolvedStage

if (-not (Test-Path $paths.exe_path)) {
    throw "Missing portable executable: $($paths.exe_path)"
}

$lifecycleMutex = $null
if ($Action -in @("launch", "status", "stop")) {
    $lifecycleMutex = Enter-StageLifecycleMutex -ResolvedStageDir $paths.stage_dir
}

try {
switch ($Action) {
    "launch" {
        Ensure-StageRuntimeLayout -Paths $paths
        $status = Get-StageStatusObject -Paths $paths
        if ($status.global_application_running) {
            Write-StatusOutput -Status $status -AsJson:$Json
            break
        }
        $historyEnvironmentName = "ARCH_NEW_HORIZON_HISTORY_PATH"
        $previousHistoryEnvironment = [Environment]::GetEnvironmentVariable(
            $historyEnvironmentName,
            [EnvironmentVariableTarget]::Process
        )
        try {
            # The coordinator owns the child environment.  A normal portable
            # cannot inherit a stale connected-lane path from this shell.
            [Environment]::SetEnvironmentVariable(
                $historyEnvironmentName,
                $(if ($selectedLaunchHistoryPath) { $selectedLaunchHistoryPath } else { $null }),
                [EnvironmentVariableTarget]::Process
            )
            $proc = Start-Process -FilePath $paths.exe_path -WorkingDirectory $paths.stage_dir -PassThru
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                $historyEnvironmentName,
                $previousHistoryEnvironment,
                [EnvironmentVariableTarget]::Process
            )
        }
        $lease = Wait-ApplicationLeaseForProcess `
            -Paths $paths `
            -ProcessId ([int]$proc.Id) `
            -TimeoutSeconds 10
        if ($null -eq $lease) {
            $current = Get-StageStatusObject -Paths $paths
            if ($current.global_application_running) {
                Write-ControlLog -Paths $paths -Message (
                    "launch_rejected_existing lease={0} generation={1} stage={2}" -f
                    $current.lease_id,
                    $current.lease_generation,
                    $current.lease_stage_dir
                )
                Write-StatusOutput -Status $current -AsJson:$Json
                break
            }
            throw "54GUC.exe did not publish an exact ApplicationLease for stage $($paths.stage_dir)"
        }
        Write-ControlLog -Paths $paths -Message (
            "launch lease={0} generation={1} pid={2} birth_ms={3} history={4}" -f
            $lease.lease_id,
            $lease.generation,
            $lease.process_id,
            $lease.process_started_at_unix_ms,
            $(if ($selectedLaunchHistoryPath) { $selectedLaunchHistoryPath } else { "portable_default" })
        )
        Write-StatusOutput -Status (Get-StageStatusObject -Paths $paths) -AsJson:$Json
        break
    }

    "status" {
        Write-StatusOutput -Status (Get-StageStatusObject -Paths $paths) -AsJson:$Json
        break
    }

    "stop" {
        Ensure-StageRuntimeLayout -Paths $paths
        $status = Get-StageStatusObject -Paths $paths
        if (
            $status.global_application_running -and
            -not $status.lease_stage_matches_request
        ) {
            throw (
                "Portable stop refused: the global ApplicationLease belongs to another stage; lease={0} generation={1}" -f
                $status.lease_id, $status.lease_generation
            )
        }
        if ($status.running) {
            $gui = Get-Process -Id ([int]$status.process_id) -ErrorAction SilentlyContinue
            $closeRequested = $false
            if ($null -ne $gui) {
                $closeRequested = [bool]$gui.CloseMainWindow()
            }
            Write-ControlLog -Paths $paths -Message (
                "stop_close_requested lease={0} pid={1} accepted={2}" -f
                $status.lease_id, $status.process_id, $closeRequested
            )
            $exited = Wait-ExactPortableProcessExit `
                -ExePath $paths.exe_path `
                -ProcessId ([int]$status.process_id) `
                -ProcessStartedAtUnixMs ([long]$status.process_started_at_unix_ms) `
                -TimeoutSeconds 90
            $forced = $false
            if (-not $exited) {
                $forced = $true
                $exact = Get-PortableProcess `
                    -ExePath $paths.exe_path `
                    -ProcessId ([int]$status.process_id) `
                    -ProcessStartedAtUnixMs ([long]$status.process_started_at_unix_ms)
                if ($null -ne $exact) {
                    Stop-Process -Id ([int]$exact.ProcessId) -Force -ErrorAction SilentlyContinue
                }
                $exited = Wait-ExactPortableProcessExit `
                    -ExePath $paths.exe_path `
                    -ProcessId ([int]$status.process_id) `
                    -ProcessStartedAtUnixMs ([long]$status.process_started_at_unix_ms) `
                    -TimeoutSeconds 5
            }
            if (-not $exited) {
                throw (
                    "Portable stop failed; exact leased process remains: lease={0} pid={1}" -f
                    $status.lease_id, $status.process_id
                )
            }
            $terminalLease = Wait-MatchingApplicationLeaseTerminal `
                -Paths $paths `
                -LeaseId ([string]$status.lease_id) `
                -Generation ([long]$status.lease_generation) `
                -TimeoutSeconds 5
            if ($null -eq $terminalLease) {
                if ($forced) {
                    throw (
                        "Portable stop degraded: forced exact process exit without matching terminal ApplicationLease receipt; lease={0} generation={1}" -f
                        $status.lease_id, $status.lease_generation
                    )
                }
                throw (
                    "Portable stop degraded: graceful process exit without matching terminal ApplicationLease receipt; lease={0} generation={1}" -f
                    $status.lease_id, $status.lease_generation
                )
            }
            if ([string]$terminalLease.state -ne "stopped") {
                throw (
                    "Portable stop completed with failed ApplicationLease receipt; lease={0} generation={1}" -f
                    $status.lease_id, $status.lease_generation
                )
            }
            Write-ControlLog -Paths $paths -Message (
                "stop_complete lease={0} pid={1} forced={2}" -f
                $status.lease_id, $status.process_id, $forced
            )
        }
        Remove-ControlStateArtifacts -Paths $paths
        Write-StatusOutput -Status (Get-StageStatusObject -Paths $paths) -AsJson:$Json
        break
    }

    "validate" {
        Invoke-PortableValidation -Paths $paths
        Write-Host "Portable validation: PASS"
        break
    }

    "logs" {
        Ensure-StageRuntimeLayout -Paths $paths
        Start-Process explorer.exe -ArgumentList $paths.runtime_root | Out-Null
        Write-ControlLog -Paths $paths -Message "open_logs"
        Write-Host "Opened logs: $($paths.runtime_root)"
        break
    }

    "bundle" {
        Ensure-StageRuntimeLayout -Paths $paths
        & $paths.exe_path --create-support-bundle | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Support bundle creation failed for $($paths.stage_dir)"
        }
        Write-ControlLog -Paths $paths -Message "support_bundle"
        break
    }

    "plan" {
        & $paths.exe_path --print-launch-plan | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Launch plan failed for $($paths.stage_dir)"
        }
        break
    }

    default {
        throw "Unsupported action: $Action"
    }
}
}
finally {
    Exit-StageLifecycleMutex -Mutex $lifecycleMutex
}
