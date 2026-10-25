# 54GUC — 5G/4G Ultimate Controller

54GUC monitors supported LTE and 5G routers and provides the controls exposed by their selected model and firmware profiles. It includes live radio and traffic readings, connection history, an advisory engine and a shareable site report.

The biggest contribution we need is **testing the current software on more
routers and helping develop exact profiles, one step at a time**. You do not
need to write code to help. Tell us what your exact model and firmware can
actually report, what remains unknown and what the app gets wrong. Start with
[how to contribute a router test](CONTRIBUTING.md#help-test-more-routers) and
[the project goals](docs/PROJECT_GOALS.md).

The first-party 54GUC source is licensed under **GNU GPL version 3 only (GPL-3.0-only)**. See [LICENSE](LICENSE) and [licensing information](LICENSING.md). This source identifies R25.7. Use its published release record for the matching Windows package, source archive and checks actually completed; a source checkout alone is not a verified Windows package. Existing activation remains unchanged.

## Run a released application

Download the Windows portable ZIP from [54GUC Releases](https://github.com/54GUC2026/54GUC-Releases/releases), extract it to a writable folder and follow the release instructions. Choose the portable application asset; automatic repository source archives are not the Windows application. Select the exact router model and firmware. Readings and controls depend on that profile and the native reply it supplies.

The customer guides are in `docs/CGF_MUST_READ/BETA_PACKAGE`.
Activation for supported router controls stays unchanged. Licences are free;
optional donations are not required. Keep your existing valid modem-bound code.
The published source includes the existing code-generation logic, so anyone
with it can generate valid codes; that is an intentionally accepted consequence
of this free open-source release. See [licensing information](LICENSING.md).

## Understand the source

- `arch4/modem/`: existing device-family protocol readers and controls.
- `modem_profiles/`: selected-model native read/write definitions and capability boundaries.
- `arch4/modem_live/`: acquisition, freshness and conversion into accepted observations.
- `arch4/core/` and `arch4/core_contracts/`: accepted state and action contracts.
- `arch4/foundation/`: lifecycle services, reports and export ownership.
- `arch4/ngui/`: user-interface projections and pages.
- `arch4/new_horizon/`: advisory and historical evidence consumers.
- `arch4/tests/`: regressions, retained-format fixtures and offline simulators.

Missing, invalid, stale and unsupported readings are different states. A compatible read does not grant control permission. Cell identity and commands require coherent same-device, SIM, profile and acquisition evidence. Simulator success is software/protocol evidence, not physical firmware qualification.

## Prepare a development environment

Use native Windows Python 3.13 and the pinned requirement files. From the source root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Start with the changed owner's offline tests. For example:

```powershell
.\.venv\Scripts\python.exe -m pytest arch4/tests/test_huawei_parsers.py -q
```

See [Testing a change](docs/TESTING.md) for the public test scope and [Building on Windows](docs/BUILDING.md) for the existing coordinator and its folder requirements. The reviewed public offline selection is listed separately; the full private development suite also uses operational documents and retained hardware captures.

Router-connected tests and application launches are separate, explicit actions. Never use another router's settings, identifiers or History as a test population. See the contribution guide for evidence and privacy expectations.

## Contribute and report issues

Our main need is careful testing on routers or firmware without enough hardware
evidence, followed by small, reviewed profile improvements. A working login or
a signal reading does not prove that a radio control is safe. If your exact
profile is missing, report that gap; do not choose a similar model just to force
a connection or enable controls.

See [Contributing](CONTRIBUTING.md) for the router-test, profile-proposal and bug
report forms, and [Security reports](SECURITY.md) for private security reporting.
Keep public evidence small and sanitized. Do not post raw support ZIPs,
passwords, activation codes, SIM/device identifiers, complete History or private
configuration. A report of missing support is useful even without an attachment.

## Licence and third-party components

The project uses [GPL-3.0-only](LICENSE). Third-party components retain their own copyright and licence terms; preserve their notices and matching-source obligations. [Protocol references](docs/PROTOCOL_REFERENCES.md) document field research. They do not grant a code licence or establish physical firmware qualification.
