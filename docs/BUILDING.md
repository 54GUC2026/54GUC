# Build your own 54GUC on Windows

Building the app is a useful way to try a new profile, experiment with an idea or see how your changes feel in everyday use. This guide follows the existing Windows builder, using native 64-bit Windows, Python 3.13, Git for Windows and Windows `tar.exe`.

## Start with your source

Fork and clone the repository, make your changes in a branch, and commit the version you want to build. The coordinator builds a Git commit, so committing also gives you a convenient checkpoint to return to.

If you're starting from the released source ZIP instead of a Git checkout, extract it into a fresh folder and create a local repository there:

```powershell
git init --initial-branch=source-build
if ($LASTEXITCODE -ne 0) { throw 'Git initialization failed.' }
git add --all
if ($LASTEXITCODE -ne 0) { throw 'Source staging failed.' }
git -c user.name='Local build' -c user.email='local-build@example.invalid' `
    commit -m 'Import verified application source snapshot'
if ($LASTEXITCODE -ne 0) { throw 'Local source commit failed.' }
```

For an existing clone, you can continue directly to the environment setup.

## Set up a build workspace

From the source root, run:

```powershell
$ErrorActionPreference = 'Stop'
$appSourceRoot = (Get-Location).Path
$buildRoot = Join-Path $env:LOCALAPPDATA '54GUC-build'
$env:CGF_WINDOWS_CODEX_ROOT = $buildRoot
$outputRoot = Join-Path $buildRoot '54b'
$stageRoot = Join-Path $buildRoot 'b'
$bootstrapEnv = Join-Path $outputRoot 'build-bootstrap'
New-Item -ItemType Directory -Force -Path $outputRoot, $stageRoot | Out-Null
py -3.13 -m venv $bootstrapEnv
if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
$buildPython = Join-Path $bootstrapEnv 'Scripts\python.exe'
```

This gives the coordinator a separate workspace: `b` for build files and `54b` for finished packages. A clean source checkout keeps router settings and connection History separate from your development work; the builder can include a `portable_runtime` folder if one is present.

## Build

Choose a fresh build stamp and run the coordinator:

```powershell
$buildStamp = 'R25.7_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
& .\scripts\windows_portable_control.ps1 `
    -Action build `
    -SourceRepo $appSourceRoot `
    -Ref HEAD `
    -StagingRoot $stageRoot `
    -OutputRoot $outputRoot `
    -PythonExe $buildPython `
    -ReleaseTag '54GUC' `
    -Stamp $buildStamp
if ($LASTEXITCODE -ne 0) { throw 'Build failed; review the saved output.' }
```

The coordinator creates a fresh dependency environment, installs the pinned runtime and validates the package. Its output points to the finished folder, ZIP and build records. If a check fails, the saved output is a useful starting point for an issue or a fix.

## Try your experiment

Extract your build into a separate folder and try your change. For a new router profile, the [profile workshop](ROUTER_PROFILES.md) suggests a first monitoring experiment. The [test guide](TESTING.md) helps turn an interesting result into an offline regression.

The source includes the app modules, all 48 profiles, six codecs, schemas, fonts, configuration templates, build scripts, hooks and customer guides used by this route. These live together so that changes can be tested and shared as a complete app.

The official R25.7 archives and test results are linked in [project status](PROJECT_STATUS.md). Your local build has its own source commit and build records; identical bytes across different machines aren't promised.

## Explore further

The Python offline Checker and code-generation tools are also in the source. Their separate executable packaging is independent of the main app build above. The current app's activation works as before, and no signing-key setup is needed for this build.

Interested in a different build workflow or platform? Explore it in your branch and share what you find.

<sub>Practical sharing tips and the licence link are in [project notes](PROJECT_NOTES.md).</sub>
