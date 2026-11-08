# 54GUC R29.0

54GUC is a Windows application for monitoring and controlling supported LTE/5G routers through their existing firmware interfaces. It is free and open source under GPL-3.0-only.

- [Download the Windows app](https://github.com/54GUC2026/54GUC-Releases/releases/tag/R29.0)
- [Project website and router catalogue](https://54guc2026.54guc2026.workers.dev/)
- [Functional specification](docs/FUNCTIONAL_SPEC.md)
- [Build on Windows](docs/BUILDING.md)
- [Run offline tests](docs/TESTING.md)
- [Edit a router profile](docs/ROUTER_PROFILES.md)
- [User guide](docs/user/USER_GUIDE.txt)

## Features

- LTE/5G signal, serving-cell, traffic, latency and connection monitoring.
- Live graphs, locally stored connection History and chart restoration.
- Manual band controls, schedules and cell controls where the router profile supports them.
- Optional Brain decisions and Referee assessments based on available measurements.
- Router-specific activation, diagnostic exports and configurable observation settings.
- Browser download through UPDATES; no automatic installation or restart.

There are 49 router profiles across six protocol families. Available measurements and controls depend on the exact model and firmware. Xiaomi CB0501 stock software 3.0.16 supports the available monitoring fields; band selection, band lock and cell lock are unsupported. A similar model name alone does not establish compatibility.

## Source layout

`arch4/modem/` contains device-family interfaces. `arch4/modem_live/` acquires and normalizes data. Matrix supplies common fields to Core, Brain, the GUI and local persistence. `arch4/ngui/` contains the desktop interface; `arch4/tests/` and `arch4/intelligence/tests/` contain offline tests. Router definitions are in `modem_profiles/`; Windows packaging is in `scripts/`.

This repository contains the R29.0 source snapshot and the files needed to work from it. Use a separate writable development folder and synthetic test identities. Keep router passwords, activation codes, connection databases and personal captures out of commits and public reports.

See [LICENSE](LICENSE), [third-party notices](LICENSING.md), [security reporting](SECURITY.md) and [contributing](CONTRIBUTING.md).
