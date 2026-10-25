# Experiment with 54GUC offline

You can work on profiles, parsers, reports and many other parts of 54GUC without owning every router. A small saved-reply example or synthetic input is often enough to explore an idea and turn it into a useful test.

## Get a working starting point

Use native Windows Python 3.13 and run this from the source root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$publicTests = @(Get-Content -LiteralPath .\docs\PUBLIC_TEST_SELECTION.txt |
    Where-Object { $_.Trim() -and -not $_.StartsWith('#') })
.\.venv\Scripts\python.exe -m pytest @publicTests -q
if ($LASTEXITCODE -ne 0) { throw 'A public offline test failed.' }
```

The supplied selection is a convenient first run: its inputs are included in the public repository. It passed **731 tests with one platform-specific skip** for R25.7. The selection and its scope are described in [PUBLIC_TEST_SELECTION.txt](PUBLIC_TEST_SELECTION.txt) and [PUBLIC_TEST_SCOPE.json](PUBLIC_TEST_SCOPE.json). Some other tests in the repository refer to private hardware recordings that aren't included here, so running the selection first avoids chasing missing inputs.

## Pick a small experiment

Find a test near the part you're changing. For example, a Huawei parser check can be run with:

```powershell
.\.venv\Scripts\python.exe -m pytest arch4/tests/test_huawei_parsers.py -q
```

Copy the shape of a nearby test, add an example of the behavior you'd like to explore, and run it. Seeing the example fail before your change and pass afterward makes the improvement easy to explain.

Interesting examples include another firmware's field name, a nested response, an explicit unit, a missing value or a valid zero. Trying both the familiar format and the new one helps you see whether a more flexible reader still gives useful results.

## Share what you learned

Your issue or pull request can include the test command, the result and any questions still open. A draft is useful even if you haven't solved everything. Other contributors may have the hardware, a related example or an idea for the next step.

Synthetic tests and simulators are especially useful for exploring software behavior. Testing the same change on a real router adds another useful result; label each so other people can compare their findings.

The [profile workshop](ROUTER_PROFILES.md), [build guide](BUILDING.md) and [project goals](PROJECT_GOALS.md) suggest places to go next.

<sub>Practical sharing and hardware-experiment tips are collected in [project notes](PROJECT_NOTES.md).</sub>
