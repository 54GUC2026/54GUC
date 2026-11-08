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
if ($LASTEXITCODE -ne 0) { throw 'An offline test failed.' }
```

Use the explicit list rather than bare `pytest`: some additional tests require
recordings or inputs that are not distributed. PUBLIC_TEST_SCOPE.json describes
the included input groups. The offline checks cover parsers, input identity,
capabilities, activation/storage, reports, privacy and source packaging.

## Test a change

Run the affected module first, then its related consumers. For example:

```powershell
.\.venv\Scripts\python.exe -m pytest arch4/tests/test_huawei_parsers.py -q
```

Check the complete path from native reply through decoding and Matrix to its
display or command result. Include missing, malformed, valid zero, conflicting,
stale and wrong-device replies. A successful request must not hide a missing
value or an unsuccessful command. Use synthetic identities for privacy checks.

Offline tests use mocked transports or synthetic saved inputs. They must not
contact a physical router, change its settings or reuse personal History.
Simulator results describe software/protocol behaviour, not physical radio
performance. Hardware tests require access to the exact model and firmware.

Router profiles, codecs and their declarations are included with the source.
Missing recordings are unavailable inputs, not successful replays. Preserve
actual device values when adding a regression; do not change them to match an
incorrect result. Standalone Checker packaging is separate from the router app.

See [Building on Windows](BUILDING.md), [profiles](ROUTER_PROFILES.md) and
[contributing](../CONTRIBUTING.md).
