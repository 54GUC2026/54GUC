# 54GUC R29.0 functional specification

## Purpose and supported environment

54GUC runs on native 64-bit Windows and connects to a selected router on the local network. It uses the router's existing management interface; it does not install router firmware. Profiles identify the model, firmware, protocol, available fields and controls. Unsupported information remains unavailable rather than being invented.

## Connection and identity

The Device page selects a profile, address, credentials and Observe or Live mode. Authentication follows the selected device protocol. Router identity and SIM context scope measurements, activation and saved data. Live control requires the applicable permission and activation; monitoring can operate without enabling Brain actions.

## Acquisition and Matrix

Device adapters parse native replies and normalize values, units, field names and availability. Matrix publishes the common representation to consumers. Missing, invalid, expired, unsupported and carrier-absent data remain distinguishable. A successful transport request does not make every returned field valid.

Modem access is serialized by the existing scheduler and profile-specific timing rules. FAST supplies frequent measurements including traffic. The READ display measures actual FAST start-to-start time, including delays. Maintenance collects less frequent settings and optional data. Session-static information can be cached until its context changes.

## User interface

- Device: connection, identity, SIM/network/APN settings and available router capabilities.
- Graphs: LTE/NR signal, traffic, latency, congestion and connection status over time.
- Dashboard: current radio information, connection quality and antenna-position comparisons.
- Developer Control: detailed measurements and available band, network, cell and scheduling controls.
- Referee: measured assessment results and forecasts, with unavailable inputs left explicit.
- Brain: current decisions, their reasons and the router's saved learning.

## Controls and recovery

Core admits commands according to current identity, ownership, permissions, pending work and recovery state. An acknowledgement is followed by matching configured-state evidence where required. Conflicting readback does not prove success. Emergency and recovery policy can request a supported 5G-to-4G fallback when its conditions are met. Protocol support and native safeguards are profile-specific.

Brain is optional. Its preparation, decisions and actions depend on qualified current inputs and supported controls. Disabling it prevents its autonomous actions while ordinary monitoring continues. Referee evaluates recorded connection conditions; it does not manufacture missing modem measurements.

## Local storage and diagnostics

Connection History and learning are local and router-scoped. The separate chart database retains up to 24 hours of graphs and restores them after successful login at low priority. Deleting that chart database starts a new chart record; it is excluded from support bundles.

History preparation, compression and disk writing use background work so ordinary publication does not wait for storage. Diagnostic settings can enable existing recording points with bounded retention. Support export applies privacy filtering; a rejected export is reported rather than releasing an unsafe archive.

## Profiles and protocol families

The source includes Huawei HiLink, MikroTik RouterOS, TP-Link DEV2, Xiaomi management interfaces, ZTE GoForm and ZTE ZWRT/UBUS adapters. Similar products may use different APIs or reply shapes. See ROUTER_PROFILES.md for field mapping and offline examples. Profiles do not grant controls that their firmware cannot perform.

## Build and modification

BUILDING.md describes the existing Windows portable coordinator and pinned dependencies. TESTING.md lists offline checks whose required inputs are distributed. Tests needing separate hardware or captures require those inputs. Required third-party notices remain with the source and package.
