# 54GUC — universal 4G/5G router app for Windows

Shared under the [GPL-3.0-only licence](https://www.gnu.org/licenses/gpl-3.0.html).

**[Try the Windows app](https://54guc2026.54guc2026.workers.dev/#start) · [Find your router](https://54guc2026.54guc2026.workers.dev/routers) · [Generate a free router licence](https://54guc2026.54guc2026.workers.dev/#licence)**

54GUC (5G / 4G Ultimate Controller) is free, open-source Windows software for **multi-brand LTE/5G router monitoring, live signal graphs, connection history and band controls**. It brings Huawei, ZTE, TP-Link, MikroTik and Xiaomi router profiles into one desktop app.

Try the existing version, compare your connection over time, or help build a better one. A router on your desk, a useful idea or a confusing reading can be the beginning of a contribution. Experiments, independent branches and forks are welcome.

## Download and get connected

The public release is **R25.7**, supplied as a Windows portable ZIP and matching source archive.

1. [Download the Windows ZIP](https://github.com/54GUC2026/54GUC-Releases/releases/download/R25.7/54GUC_Windows_portable_R25.7_20261025_013103.zip) and extract it to a writable folder.
2. Connect your PC to the router's network and run **54GUC.exe**. No router firmware installation is needed.
3. On the Device page, select your model and firmware profile, then sign in to the router.
4. Use the router identity shown in the app to [generate a free licence](https://54guc2026.54guc2026.workers.dev/#licence), and enter the complete code in the app's LICENSE window.
5. Open Graphs or Dashboard to explore the connection. You can leave Brain off while you get familiar with the readings.

[Release notes and download checks](https://github.com/54GUC2026/54GUC-Releases/releases/tag/R25.7) · [First connection guide](docs/CGF_MUST_READ/BETA_PACKAGE/START_HERE.txt)

## What can I use it for?

- **Watch LTE and 5G signal quality:** follow available bands, RSRP, RSRQ, SINR and other radio readings.
- **Understand the connection:** compare signal graphs with download, upload, latency and local connection history.
- **Compare antenna positions:** use the router's available measurements to see how a new position behaves.
- **Choose bands from a PC:** use LTE band selection, band locking and NR (5G) selections where provided by the selected firmware profile.
- **Explore optional automation:** Brain learns from this router's connection history and can use the controls available to it.
- **Help another router owner:** report a working feature, investigate an unfamiliar reply or improve a profile.

The desktop connects to the router you select. The model and firmware determine which readings and controls appear.

## Find your router

The public catalogue contains **48 profiles across six protocol families**. These links cover **23 named device entries**; each device page lists its hardware, firmware, readable information and accessible features.

| Manufacturer | Device pages |
| --- | --- |
| Huawei | [Huawei B535-232](https://54guc2026.54guc2026.workers.dev/routers/huawei-b535-232) · [Huawei B535-333](https://54guc2026.54guc2026.workers.dev/routers/huawei-b535-333) · [Huawei B818-263](https://54guc2026.54guc2026.workers.dev/routers/huawei-b818-263) · [Huawei 5G CPE Pro](https://54guc2026.54guc2026.workers.dev/routers/huawei-5g-cpe-pro) · [Huawei 5G CPE Pro 2](https://54guc2026.54guc2026.workers.dev/routers/huawei-5g-cpe-pro-2) · [Huawei 5G CPE 5s](https://54guc2026.54guc2026.workers.dev/routers/huawei-5g-cpe-5s) · [Huawei 5G CPE 5](https://54guc2026.54guc2026.workers.dev/routers/huawei-5g-cpe-5) · [Huawei H173-383](https://54guc2026.54guc2026.workers.dev/routers/huawei-h173-383) |
| MikroTik | [MikroTik Chateau LTE12](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-chateau-lte12) · [MikroTik LHG LTE18 kit](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-lhg-lte18) · [MikroTik Chateau 5G](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-chateau-5g) · [MikroTik Chateau 5G ax](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-chateau-5g-ax) · [MikroTik Chateau 5G R16](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-chateau-5g-r16) · [MikroTik ATL 5G R16](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-atl-5g-r16) · [MikroTik Chateau 5G R17 ax](https://54guc2026.54guc2026.workers.dev/routers/mikrotik-chateau-5g-r17-ax) |
| TP-Link | [TP-Link NX520](https://54guc2026.54guc2026.workers.dev/routers/tp-link-nx520) · [TP-Link NX520v](https://54guc2026.54guc2026.workers.dev/routers/tp-link-nx520v) |
| Xiaomi | [Xiaomi 5G CPE Pro](https://54guc2026.54guc2026.workers.dev/routers/xiaomi-5g-cpe-pro) |
| ZTE | [ZTE G5B](https://54guc2026.54guc2026.workers.dev/routers/zte-g5b) · [ZTE G5TC](https://54guc2026.54guc2026.workers.dev/routers/zte-g5tc) · [ZTE MC801A](https://54guc2026.54guc2026.workers.dev/routers/zte-mc801a) · [ZTE MC888](https://54guc2026.54guc2026.workers.dev/routers/zte-mc888) · [ZTE MF296R](https://54guc2026.54guc2026.workers.dev/routers/zte-mf296r) |

[Browse the full device catalogue](https://54guc2026.54guc2026.workers.dev/routers) · [Add or improve a profile](docs/ROUTER_PROFILES.md)

## Common questions

### Is 54GUC a universal router app or a replacement firmware?

It is a multi-brand Windows application that talks to routers through their existing interfaces. It does not replace router firmware. Start with the device catalogue to find your model and firmware.

### Can I lock 5G bands on a ZTE MC888?

MC888 firmware profiles with NR band controls provide an NR (5G) band field and **Apply NR** in Developer Control. LTE selection has its own **Apply LTE** action. Select the profile for your firmware, make your selection and follow the app's action result and subsequent readings. Selecting allowed bands does not guarantee a specific serving cell or faster speeds. [MC888 features and firmware](https://54guc2026.54guc2026.workers.dev/routers/zte-mc888).

### Can I monitor a TP-Link NX520 from Windows?

Yes. The NX520 profiles provide signal monitoring, graphs and connection history. Graphs lets you compare LTE/5G signal readings with download, upload and latency. [NX520 monitoring and features](https://54guc2026.54guc2026.workers.dev/routers/tp-link-nx520).

### Can I use it as a Huawei B818 band selector on my PC?

The B818-263 profile provides LTE band controls in Developer Control. **CA Pool** is the allowed LTE band set; **PBAND** is the preferred LTE band. Use **Apply LTE** and follow the resulting action and fresh radio readings. [B818-263 features](https://54guc2026.54guc2026.workers.dev/routers/huawei-b818-263).

### Is there an Android or iPhone version?

The released application is for Windows. Your PC connects to the router over its local network.

### How can I help if my router isn't listed?

Tell us the model, firmware and what you would like to read or control. Testing, documentation, ideas and code all help. [Start a router report](https://github.com/54GUC2026/54GUC/issues/new/choose) or visit the [profile workshop](docs/ROUTER_PROFILES.md).

## Join in, with or without code

Our biggest opportunity is testing the current software on more routers and gradually improving their profiles. Try a feature, describe a confusing screen, share a format example or explore an idea in your own branch. A polished patch isn't a prerequisite.

| I'd like to… | Start here |
| --- | --- |
| Help test a router | [Contributing](CONTRIBUTING.md#help-test-more-routers) |
| Add or improve a profile | [Profile workshop](docs/ROUTER_PROFILES.md) |
| Explore ideas and priorities | [Project goals](docs/PROJECT_GOALS.md) |
| Try the source | [Offline tests](docs/TESTING.md) · [Windows build](docs/BUILDING.md) |
| See where things stand | [Project status](docs/PROJECT_STATUS.md) |

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

<sub>For the licence link and practical sharing tips, see [project notes](docs/PROJECT_NOTES.md).</sub>
