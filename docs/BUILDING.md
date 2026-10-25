# Build 54GUC on Windows

These instructions describe the R25.7 Windows build route. Use the release
record to identify the matching application ZIP and reviewed source archive.
The public source contains the application and its required build inputs;
private operational history and router recordings are excluded.

Use native 64-bit Windows, Python 3.13, Git for Windows and Windows `tar.exe`.
The existing builder also accepts Python 3.12, but the current candidate was
checked with Python 3.13. Do not use WSL paths for these instructions.

## Prepare an isolated build folder

Start in the source repository root. Use a clean, committed source checkout;
the coordinator exports the selected Git commit, not uncommitted edits.
Download-only repository archives are not the application source.

A reviewed application source ZIP intentionally contains no private Git
history. Check its SHA-256 against the published release record first. Extract
it into a new folder outside another checkout. For that ZIP only, create a
fresh local commit before using the existing builder:

```powershell
git init --initial-branch=source-build
if ($LASTEXITCODE -ne 0) { throw 'Git initialization failed.' }
git add --all
if ($LASTEXITCODE -ne 0) { throw 'Source staging failed.' }
git -c user.name='Local build' -c user.email='local-build@example.invalid' `
    commit -m 'Import verified application source snapshot'
if ($LASTEXITCODE -ne 0) { throw 'Local source commit failed.' }
```

Do not run that initialization block in an existing repository. The new local
commit identifies your build input; it does not recreate the original private
commit or history. Keep the source ZIP's public manifest and published hash as
the link to the reviewed release. A build from a new local commit must not be
called a byte-identical reproduction of the official binary.

The coordinator keeps build files under `b` and finished files under `54b`.
Its selected Python must also be under `54b`. Set the root explicitly so it
does not fall back to a developer's machine layout:

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

Do not put a router's History, licence, passwords or settings in this checkout.
In particular, the source must not contain `portable_runtime`: the existing
builder can copy that folder's configuration and memory into a package.

## Build through the existing coordinator

Choose a new stamp for every build. Leave dependency installation and package
validation enabled. The builder creates its own fresh environment and checks
the runtime versions in `requirements-runtime-pins.txt`.

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

Do not call the internal native builder directly or replace its DLLs. Do not
add skip switches to turn a failed check into a successful build. An offline
package check does not qualify a router connection or a firmware version.

## Files a source release must keep

The application needs its Python modules, all 48 portable router profiles,
six fixed codecs, both field/schema JSON files, fonts, empty configuration
templates, version/update descriptors and dependency manifests. Keep the
coordinator, its export/build/validation helpers, PyInstaller spec and hooks.
Keep the original third-party notices and licence texts.

The native builder also requires exactly these five public guide inputs:

- `docs/CGF_MUST_READ/BETA_PACKAGE/START_HERE.txt`
- `docs/CGF_MUST_READ/BETA_PACKAGE/USER_GUIDE.txt`
- `docs/CGF_MUST_READ/BETA_PACKAGE/SUPPORTED_HARDWARE_AND_LIMITATIONS.txt`
- `docs/CGF_MUST_READ/BETA_PACKAGE/RECOVERY_AND_SUPPORT.txt`
- `docs/CGF_MUST_READ/BETA_PACKAGE/BETA_RELEASE_NOTES.txt`

Do not remove the whole `docs` tree without selecting these files first.
Private runbooks, personal support exports and development history do not
belong in a public source package.

The optional model descriptions and data used in private development are
omitted from the public source plan. Keep the model backend and its existing
built-in synthetic model, all 48 portable router profiles and their required
codecs. Public model tests must select a reviewed available input explicitly.
Silently falling back from an omitted named model does not reproduce its
scenario. Do not replace real captured facts just to make a replay pass.

## Record the result accurately

A release record should identify the exact source commit, source archive hash,
application ZIP hash, runtime pins, build-tool versions and checks actually run.
The runtime pins alone do not make the Windows ZIP byte-for-byte reproducible:
the current coordinator installs the build tool separately. Record its actual
version as well. The official R25.7 binary was built from commit
`176d4e4fd7438b833ffc7a5bbb543df62d44aa0c` after a fresh complete native
run passed 17,891 tests with 18 skips and all 105 critical checks. The public
snapshot may contain later documentation only; its release record binds the
unchanged application/build inputs to that tested commit. This is input
correspondence, not a claim of byte-for-byte reproducible binaries.

Keep local build receipts private if they contain machine paths. Publish a
small release record with the information needed to find the matching source.
Never describe a source snapshot as matching an older binary unless their
actual contents have been checked.

## Private release-only packaging

The old private `tools/prepare_beta_distribution.py` archive cleaner is outside
this public build route and is excluded from the current public source plan.
Do not use it for the new release. If a final release pipeline does use it,
reopen its source-inclusion and external-private-rule checks before publication.
The existing native build and generic bundle validation remain required.
Private names or rule files are not needed to compile the public application.

## Existing activation and code generation

The existing activation, validator and generator remain unchanged. No signing
key, production public-key registration or replacement-code migration is needed
to compile or use this candidate. The source retains the code-generation logic,
so anyone with it can generate a valid code; this is intentional. Licences are
free, and donations are optional.

Keep the actual app build inputs and the existing activation tests in the
reviewed source list. A separate generator executable uses its own existing
builder and validation; this guide does not claim it was rebuilt or tested.
Operator-supplied private source-export rules belong only to source/release
packaging, not to public app compilation. Do not publish private operational
data along with the generating logic.

## Optional offline Checker

The Python Checker modules and selected synthetic tests are included. The
separate frozen Checker executable has its own private operator build gate;
this initial public snapshot does not claim that its standalone builder is
available. The Windows application build above is complete and independent
of that optional tool.
