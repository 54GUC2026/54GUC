# Offline portable profile definitions

The completed read-definition set is **48 separate self-contained JSON files**
under `profiles/`. The owned semantic catalogue is `field-definitions.v2.json`;
stable IDs remain in `field_ids.v1.json`. Each profile embeds the full catalogue,
its own native parsing operations and request/context metadata. No driver source
or another profile is needed for replay. This is offline tooling; the running
application still uses its existing internal profiles.

Read the [implemented definition standard](../../docs/CGF_MUST_READ/MODEM_PROFILE_DEFINITION_STANDARD_V2.md)
and [folder-loader migration architecture](../../docs/CGF_MUST_READ/PORTABLE_PROFILE_FOLDER_ARCHITECTURE_2026-10-19.md).
The latter specifies the transition through the current public profile facade
and eventual deletion of internal profile definitions.

All 2,260 declared readable slots are accounted for: 2,212 match existing driver
decoding through native reply inputs; 48 are explicit NR producer gaps in four
MikroTik profiles. Those gaps are not successful NR measurements. Full native
shape/type/units and selected contradiction/state regressions supplement the
field comparisons; no physical firmware qualification is implied.

From the canonical source root, using native Windows Python:

```powershell
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' -m tools.modem_profile_exchange.export_portable --verify-folder tools/modem_profile_exchange/profiles
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' -m tools.modem_profile_exchange.export_portable --copy-owned --output-folder 'D:/CODEX/54b/my-new-profile-folder'
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' -m pytest tools/modem_profile_exchange -q
```

`--copy-owned` preserves exact file bytes, never regenerates rules and never
overwrites a folder. For an explicitly reviewed new candidate only, use
`--candidate-from-current-code --output-folder <new folder> --receipt <new file>`.
That mode executes offline synthetic/sanitized native conformance through the
current drivers with network calls denied. Compare the candidate before changing
owned definitions; a driver edit must not rewrite them automatically.

`portable.schema.json` and the closed vocabulary/semantic validator are both
required. Unknown operations, changed field meanings/units, unsupported populated
slots, invented driver-gap reads, broken hashes and private instance literals
are rejected. Hashes are integrity checks, not signatures. The experimental
evaluator is not certified as an untrusted-profile sandbox or a live adapter.

Native write stages, authentication execution, scheduling, acquisition proof,
live profile loading and firmware qualification remain with existing code until
the documented integration stages are implemented and qualified. No app build,
restart, History/settings change or modem connection is performed by this tool.

## Historical migration snapshot exporter

The remainder describes the separate original migration format. Its zero
approved-rule/null-definition markers remain truthful for that format; they do
not describe the 48 independent portable definitions above.

[Definition draft](../../docs/CGF_MUST_READ/MODEM_PROFILE_DEFINITION_DRAFT_V1.md)
defines the proposed portable format and full field appendix.
[Architecture, completion status and evidence](../../docs/CGF_MUST_READ/MODEM_PROFILE_EXCHANGE_V1_2026-10-19.md)
distinguishes it from this implemented migration snapshot.

This is an **exporter** (existing source to JSON). A runtime importer (JSON into
the application) is future work. The entry point is `export_profiles.py`.

This tool creates a sanitized JSON **migration snapshot** from the current
54GUC source. It does not connect to a router, load a driver, read credentials,
read History, run parser code, modify profiles, or build/start 54GUC.

## Definition ownership

`fields.tsv` defines all **155 distinct fields currently declared by the 48
profiles**, combining the existing 169 profile names through ModemLive's sole
alias owner. It defines meanings, value types, units and missing-value handling.
Capabilities/actions and exact model/firmware selectors are exported separately.
This scope is the entire current profile catalogue, not every internal runtime,
Brain or GUI diagnostic field.

`field_ids.v1.json` assigns permanent integer IDs. Row order and driver refactors
cannot renumber fields. Append IDs for new fields; never recycle an old ID. A
meaning/type/unit change requires a new definition version and explicit review.
The running application does not consume these new definitions.

The snapshot has one slot per field in every profile. Unsupported and non-useful
fields have `parsing: null`. An unclassified field also has a null slot but keeps
its distinct `unclassified` state: absence of a declaration is not unsupported.
Expected, confirmed, synthetic and untested trust remain distinct. All actual
observed-value slots are null. No measured value or expected router reply is
invented.

## Current parsing logic versus an approved definition

The exporter embeds the exact current functions, helper functions and constants
found through static analysis, rather than only referring back to changeable
driver files. Comments/docstrings are omitted; Python syntax is normalized with
`ast.unparse` without executing it. Each snippet has a hash. ZWRT inheritance,
function-local helper imports and final ModemLive normalizers are included.
Declared profile derivations and the full static access/timing contract are kept.

**These embedded Python rules are migration evidence, not the language-neutral
portable parsing definition.** A field-name occurrence may be a producer,
consumer, diagnostic or control path. Accordingly every candidate is explicitly
marked `static_occurrence_requires_review`, and every parsing definition remains
`needs_semantic_review`. External/dynamic calls are listed, not guessed. A copied
implementation is not semantic validation and must not become the contract by
accident. The schema forbids marking these snapshots `runtime_ready`.

The remaining work to obtain a runnable portable profile is to convert each
native producer into a reviewed declarative rule: request/response selector,
raw syntax/type, conversion, bounds, branches, missing/invalid behavior,
cross-field conditions, and sanitized input/expected-output vectors. The file
already contains the source material for that conversion, but this exporter
does **not** claim to have completed it. Do not execute the embedded source or
treat static field coverage as physical firmware qualification.

## Run

From the repository root, using the native Windows development Python:

```powershell
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' tools/modem_profile_exchange/export_profiles.py --output 'D:/CODEX/54b/profiles.snapshot.json'
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' tools/modem_profile_exchange/export_profiles.py --validate 'D:/CODEX/54b/profiles.snapshot.json'
& 'D:/CODEX/54b/windows_dev_venv/Scripts/python.exe' -m pytest tools/modem_profile_exchange/test_export_profiles.py -q
```

The destination parent must exist. Existing output is never overwritten. The
whole document is validated and serialized first, then published atomically.
An unsupported filesystem or failed validation leaves no published partial file.
An old export is self-contained and is never refreshed in place when a driver
changes. Exporting again requires a different destination.

`schema.json` specifies JSON structure. The CLI additionally checks whole-field
coverage, IDs, trust/null consistency, profile metadata allowlists, parser/helper
references, source and document hashes, and privacy. Hashes detect alteration;
they are not digital signatures or proof of origin.

## Privacy boundary

Only static catalogue metadata and source definitions enter the exporter. No
support bundle, configuration file, environment credential, observed reply,
account, IMSI/IMEI/serial/IP value, or local History is an input. A fixed public
wire-account name in a firmware authentication contract is protocol metadata,
not an instance username/password. Source comments and docstrings are removed.
Private values, credential tokens, email addresses and absolute personal paths
are rejected; parsing semantics are never redacted to manufacture an export.

This is not a general-purpose sanitizer for arbitrary bundles or untrusted
source repositories. The structural validator and pattern checks cannot prove
that arbitrary prose is non-personal. Use only the reviewed source tree, retain
null observation slots and review the generated snapshot before public release.
No publication is performed by this tool.
