param(
    [string]$SourceRepo = "",
    [string]$OutputRoot = "",
    [string]$PythonExe = "",
    [switch]$RequireAll,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cgf_windows_path_registry.ps1")

if (-not $PythonExe) {
    $PythonExe = $CgfWindowsPython
}

if (-not $OutputRoot) {
    $OutputRoot = $CgfWindowsArtifactRoot
}
[void](Assert-CgfWindowsArtifactPath -PathValue $PythonExe -Label "PythonExe")
[void](Assert-CgfWindowsArtifactPath -PathValue $OutputRoot -Label "OutputRoot")

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

function New-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )

    [pscustomobject]@{
        name = $Name
        ok = $Ok
        detail = $Detail
        required = $false
    }
}

function Resolve-CommandPath {
    param([string]$CommandName)

    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $cmd) {
        return ""
    }
    return [string]$cmd.Source
}

$checks = New-Object System.Collections.Generic.List[object]

$pythonCmd = Resolve-CommandPath -CommandName $PythonExe
$pythonVersion = ""
if ($pythonCmd) {
    try {
        $pythonVersion = (& $PythonExe --version 2>&1 | Out-String).Trim()
    }
    catch {
        $pythonVersion = "version probe failed: $($_.Exception.Message)"
    }
}
$checks.Add((New-Check -Name "python" -Ok ([bool]$pythonCmd) -Detail ($(if ($pythonCmd) { "$pythonCmd :: $pythonVersion" } else { "missing command: $PythonExe" }))))

$gitCmd = Resolve-CommandPath -CommandName "git"
$checks.Add((New-Check -Name "git" -Ok ([bool]$gitCmd) -Detail ($(if ($gitCmd) { $gitCmd } else { "missing command: git" }))))

$tarCmd = Resolve-CommandPath -CommandName "tar.exe"
$checks.Add((New-Check -Name "tar" -Ok ([bool]$tarCmd) -Detail ($(if ($tarCmd) { $tarCmd } else { "missing command: tar.exe" }))))

Assert-LocalWindowsPath -PathValue $OutputRoot -Label "OutputRoot"
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$outputWritable = $false
$probePath = Join-Path $OutputRoot ".bootstrap_probe"
try {
    Set-Content -Path $probePath -Value "ok" -Encoding ASCII
    $outputWritable = $true
}
finally {
    Remove-Item $probePath -Force -ErrorAction SilentlyContinue
}
$checks.Add((New-Check -Name "output_root" -Ok $outputWritable -Detail "$OutputRoot"))

$sourceKind = "unspecified"
$wslDistro = ""
$resolvedSource = ""
if ($SourceRepo) {
    $wslSpec = Convert-WslUncPath -PathValue $SourceRepo
    if ($null -ne $wslSpec) {
        $sourceKind = "wsl"
        $wslDistro = $wslSpec.distro
        $resolvedSource = $SourceRepo
        $wslCmd = Resolve-CommandPath -CommandName "wsl.exe"
        $checks.Add((New-Check -Name "wsl" -Ok ([bool]$wslCmd) -Detail ($(if ($wslCmd) { $wslCmd } else { "missing command: wsl.exe" }))))
        if ($wslCmd) {
            try {
                $gitVersion = (& wsl.exe -d $wslDistro -e git --version | Out-String).Trim()
                $checks.Add((New-Check -Name "wsl_git" -Ok $true -Detail "$wslDistro :: $gitVersion"))
            }
            catch {
                $checks.Add((New-Check -Name "wsl_git" -Ok $false -Detail "git unavailable in distro $wslDistro"))
            }
        }
    } else {
        $sourceKind = "windows"
        $resolvedSource = (Resolve-Path $SourceRepo).Path
        Assert-LocalWindowsPath -PathValue $resolvedSource -Label "SourceRepo"
        $checks.Add((New-Check -Name "source_repo" -Ok $true -Detail $resolvedSource))
    }
}

$requiredNames = @("python", "tar", "output_root")
if ($SourceRepo) {
    if ($sourceKind -eq "wsl") {
        $requiredNames += @("wsl", "wsl_git")
    } elseif ($sourceKind -eq "windows") {
        $requiredNames += @("git", "source_repo")
    }
}
foreach ($check in $checks) {
    $check.required = ($requiredNames -contains $check.name)
}

$requiredFailures = @($checks | Where-Object { $_.required -and -not $_.ok })
if ($RequireAll -and $requiredFailures.Count -gt 0) {
    $summary = ($requiredFailures | ForEach-Object { "$($_.name): $($_.detail)" }) -join [Environment]::NewLine
    throw "Windows native bootstrap checks failed.`n$summary"
}

$checksArray = @($checks.ToArray())

$report = [pscustomobject]@{
    source_kind = $sourceKind
    source_repo = $resolvedSource
    wsl_distro = $wslDistro
    output_root = $OutputRoot
    python_exe = $PythonExe
    checks = $checksArray
    ok = ($requiredFailures.Count -eq 0)
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
} else {
    Write-Host "Windows Native Bootstrap"
    Write-Host "  Source kind: $sourceKind"
    if ($resolvedSource) {
        Write-Host "  Source repo: $resolvedSource"
    }
    Write-Host "  Output root: $OutputRoot"
    foreach ($check in $checks) {
        if ($check.required) {
            $mark = if ($check.ok) { "[PASS]" } else { "[FAIL]" }
        } else {
            $mark = if ($check.ok) { "[INFO]" } else { "[WARN]" }
        }
        Write-Host ("  {0} {1}: {2}" -f $mark, $check.name, $check.detail)
    }
    if ($report.ok) {
        Write-Host "Bootstrap status: PASS"
    } else {
        Write-Host "Bootstrap status: FAIL"
    }
}

$report
