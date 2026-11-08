# Offline router profile tools

The portable definitions under `profiles/` contain field meanings, types,
units, native parsing rules and request context. `field_ids.v1.json` supplies
stable IDs; `portable.schema.json` and the semantic validator check definitions.
The running application's profiles are in `modem_profiles/` at the source root.

From the source root with the test environment prepared:

```powershell
.\.venv\Scripts\python.exe -m tools.modem_profile_exchange.export_portable --verify-folder tools/modem_profile_exchange/profiles
.\.venv\Scripts\python.exe -m tools.modem_profile_exchange.export_portable --copy-owned --output-folder .\profile-copy
```

The copy command uses a new folder and preserves definition bytes. Offline
replay does not authenticate with, read from or change a physical router.
Hash checks establish integrity, not trust in arbitrary executable input.
Use [profile development](../../docs/ROUTER_PROFILES.md) and the
[offline test guide](../../docs/TESTING.md) when changing a definition.
