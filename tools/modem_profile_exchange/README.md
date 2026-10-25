# Offline profile exchange tools

These utilities help explore profile formats, export definitions and compare
saved inputs. They grew out of the work that made profiles editable.

**For a profile used by the current app, start with
[`modem_profiles/`](../../modem_profiles) and the
[profile workshop](../../docs/ROUTER_PROFILES.md).** This folder's exchange and
migration formats are separate development tools, useful for research and
offline comparisons.

## Explore the portable definitions

The `profiles/` directory contains 48 self-contained JSON read definitions.
They describe field mappings and native parsing operations for offline replay.
`portable.schema.json` describes their structure; the shared field catalogue
and stable IDs help compare equivalent readings between profiles.

From a development environment at the repository root:

```powershell
python -m tools.modem_profile_exchange.export_portable --verify-folder tools/modem_profile_exchange/profiles
python -m tools.modem_profile_exchange.export_portable --copy-owned --output-folder ./my-profile-examples
```

Use a new output folder for each copy. This keeps the original definitions
available as a comparison while you explore your own version.

## Explore a migration snapshot

`export_profiles.py` produces the older migration snapshot format: catalogue
metadata, field slots and source-analysis information for studying a conversion
to declarative profiles. The embedded snippets describe the existing source;
they are not a runtime profile language.

```powershell
python -m tools.modem_profile_exchange.export_profiles --output ./profiles.snapshot.json
python -m tools.modem_profile_exchange.export_profiles --validate ./profiles.snapshot.json
```

The exporter creates a new file and validates its structure and hashes.
Snapshots are useful for comparing definitions; the running app selects its
profiles from `modem_profiles/` instead.

## Bring an idea

An interesting native reply, another format or a clearer authoring workflow
can all be worth exploring. Try a branch, compare a small example and share
what you find through [Contributing](../../CONTRIBUTING.md).

<sub>[Project notes](../../docs/PROJECT_NOTES.md) collect practical sharing
tips and the licence link.</sub>
