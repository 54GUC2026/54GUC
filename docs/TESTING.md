# Test 54GUC without a router

Use native Windows Python 3.13, Git for Windows and the pinned requirements.
From the source root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$publicTests = @(Get-Content -LiteralPath .\docs\PUBLIC_TEST_SELECTION.txt |
    Where-Object { $_.Trim() -and -not $_.StartsWith('#') })
.\.venv\Scripts\python.exe -m pytest @publicTests -q
if ($LASTEXITCODE -ne 0) { throw 'A public offline test failed.' }
```

The exact list contains 18 whole test files and ten selected nodes from mixed
files. It passed **731 tests with one platform-specific skip** in the isolated
R25.7 public input snapshot. The skipped POSIX timezone case is identified in
[the public test scope](PUBLIC_TEST_SCOPE.json). These checks cover parsers,
evidence identity, conservative capabilities, existing activation/storage,
synthetic report/privacy cases, the existing generator, source export guards
and the explicit built-in model default.

Do not replace the list with bare `pytest`: helper modules and mixed files can
contain additional tests whose private inputs are intentionally absent.
[PUBLIC_TEST_SCOPE.json](PUBLIC_TEST_SCOPE.json) lists the selected and omitted
file groups and their reasons. The complete private release gate is separate:
17,891 passed, 18 skipped, no failures, and all 105 critical checks passed.
Neither count proves compatibility with untested physical routers.

## Work on a specific owner

Start with the owner you changed. For example:

```powershell
.\.venv\Scripts\python.exe -m pytest arch4/tests/test_huawei_parsers.py -q
```

For changed behavior, retain a realistic failing case, make the smallest
coherent correction, then test that case and its related consumers. Include
missing, malformed, valid zero, conflicting, stale and wrong-device replies
where relevant. Explain what the checks establish and what remains unknown.

Offline tests use mocked transports or synthetic saved inputs. They must not
connect to a physical router, change settings, or borrow its History. Router
runs need the device owner's explicit authorization. Simulator success checks
software/protocol behavior, not actual RF performance or firmware safety.

## Inputs and activation

All 48 portable router profiles and six codecs are included. Optional private
model descriptors/recordings are excluded; the existing built-in synthetic
model is selected explicitly. An absent named recording is not a passed replay.
Retained hardware facts must not be rewritten to make a test pass.

Existing activation and code generation remain unchanged. Publishing their
logic intentionally allows valid codes to be generated. Tests use synthetic
identities, never a user's saved code or History. No signing-key migration is
part of R25.7.

The Python offline Checker modules are included; its separate frozen-tool
build remains outside this initial public build claim. The old private archive
cleaner is unused by the public app build and is not included.

See [Building on Windows](BUILDING.md), [project goals](PROJECT_GOALS.md) and
[contributing](../CONTRIBUTING.md). Our priority is careful testing of the
current app on unsupported or partly verified hardware, then small, reviewed
profiles backed by that exact model and firmware's replies.
