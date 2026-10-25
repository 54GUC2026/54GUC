# 54GUC — 5G/4G Ultimate Controller

Explore your mobile-broadband connection: signal, traffic, connection history and the choices your router offers. 54GUC brings those readings together in a Windows app, with router profiles, an advisory engine and a Site Report you can use to understand your setup.

**54GUC is open source, and we'd love your help expanding it.** Our biggest opportunity is testing the current software on more 4G/5G routers and building new profiles together. A router on your desk, an unfamiliar reading or a useful idea can be the beginning of a contribution.

## Start here

| I'd like to… | Go to |
| --- | --- |
| Run the Windows app | [Download R25.7](https://github.com/54GUC2026/54GUC-Releases/releases/tag/R25.7) · [First connection](docs/CGF_MUST_READ/BETA_PACKAGE/START_HERE.txt) |
| Help test a router | [Contributing](CONTRIBUTING.md#help-test-more-routers) |
| Add or improve a profile | [Profile workshop](docs/ROUTER_PROFILES.md) |
| Explore ideas and priorities | [Project goals](docs/PROJECT_GOALS.md) |
| Try the source | [Offline tests](docs/TESTING.md) · [Windows build](docs/BUILDING.md) |
| See where things stand | [Project status](docs/PROJECT_STATUS.md) |

## What can I explore?

- Live LTE/5G signal and traffic readings, with the detail each router provides.
- Connection history and advice about the connection's behavior.
- Manual and automatic controls available through the selected router profile.
- A plain-language Site Report and deeper offline diagnostic tools.
- Editable profiles that connect native router fields to useful app features.

The catalogue contains **48 profiles across six protocol families**. Capabilities vary by model and firmware, so there is plenty of room for new readings, better format handling and wider hardware coverage. The [hardware guide](docs/CGF_MUST_READ/BETA_PACKAGE/SUPPORTED_HARDWARE_AND_LIMITATIONS.txt) describes the devices checked so far.

## Join in, with or without code

Try a router, describe a confusing screen, share a format example, or experiment with a draft profile. Reports about partially working or unsupported hardware are especially useful. You can start with a short issue; a polished patch isn't a prerequisite.

Fork the project and take it in your own direction. If your work could help others here, we'd be happy to hear about it.

[Find a way to contribute](CONTRIBUTING.md) or [open an issue](https://github.com/54GUC2026/54GUC/issues/new/choose).

## Find your way around the source

| Folder | What's inside |
| --- | --- |
| `modem_profiles/` | Router profiles and their native field mappings |
| `arch4/modem/` | Device-family protocol readers and controls |
| `arch4/modem_live/` | Collection, conversion and reading freshness |
| `arch4/core/`, `arch4/core_contracts/` | Application state and action contracts |
| `arch4/foundation/` | Lifecycle services, reports and exports |
| `arch4/ngui/` | The desktop interface |
| `arch4/new_horizon/` | Advice and connection-history consumers |
| `arch4/tests/` | Offline checks, examples and simulators |

R25.7 is available as a Windows portable ZIP and a matching source archive. The app is free, activation remains unchanged, and donations are optional. See [project status](docs/PROJECT_STATUS.md) for the release links and test scope.

<sub>For the licence link and practical sharing tips, see [project notes](docs/PROJECT_NOTES.md).</sub>
