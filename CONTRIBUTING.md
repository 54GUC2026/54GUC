# Contributing to 54GUC

54GUC uses GNU GPL version 3 only (`GPL-3.0-only`). Read [LICENSE](LICENSE) and [LICENSING.md](LICENSING.md) before contributing. Contributors retain copyright; submitting a contribution does not transfer its ownership.

Submit code only when you have the right to license it under GPL-3.0-only, and identify any separately licensed material. A normal contribution grants no additional right to relicense it as proprietary software. Any additional permission must be agreed explicitly with the relevant copyright holders; this guide does not grant it or promise future proprietary relicensing.

## Help test more routers

Our main contribution need is testing the current software on unsupported or
partly checked hardware, then gradually developing precise profiles for exact
models and firmware. You do not need to write code. Reporting a blank field,
a different native format or a clear limit can be as useful as a code patch.
See [Project goals](docs/PROJECT_GOALS.md) for the direction.

1. Record the actual app version, router model, hardware revision and complete
   firmware version. If you test a source candidate, include its commit or
   profile revision. Do not include the unit's serial, IMEI or SIM identifiers.
2. Read the [hardware and limits guide](docs/CGF_MUST_READ/BETA_PACKAGE/SUPPORTED_HARDWARE_AND_LIMITATIONS.txt).
   A listed family or similar firmware is not proof that your exact unit is
   covered. If the exact profile is absent, open a router-test report first;
   do not bypass the selector with another model's profile.
3. When an exact profile is available, begin in **Observe only** on equipment
   you own or have permission to test. Compare a few app readings with the
   router's own page at roughly the same time. State units and what is missing.
   Some routers allow one login: close the app normally before a separate
   browser login when necessary. Do not try to defeat that restriction.
4. Say separately what was **read**, what was **requested**, and what the router
   **later confirmed**. A sent command or success message alone is not proof
   that a setting changed. A report with no control tests is still useful.
5. Use the [router-test form](.github/ISSUE_TEMPLATE/router-test.yml),
   [profile-proposal form](.github/ISSUE_TEMPLATE/profile-proposal.yml) or
   [bug-report form](.github/ISSUE_TEMPLATE/bug-report.yml). These links name
   the forms in the source tree; use them in the repository's New issue menu
   once those forms are published there.

Do not run unknown commands, force an outage, disable TLS checks or change
bands just to produce evidence. Stop when the profile or app says an operation
is unavailable. Keep a local way to recover the router before any separately
agreed control test. Never use another physical unit's settings or History.

## Grow an exact profile gradually

A useful first result can be monitoring only. Add a field when its native
source, meaning, type and units are understood. Keep unknown values unknown;
missing, valid zero, stale and unsupported are different states. Do not make
a plausible number appear by guessing a scale or borrowing another reading.

Use a separate profile for a newly qualified model/firmware where needed.
Correct only what evidence supports, keeping known working profiles and real
capture facts intact. A profile cannot add a new protocol or command merely
by declaring it supported; changes beyond the existing protocol adapters need
application review. A working read does not grant a write permission.

Develop and validate drafts offline before a separately agreed hardware check.
Do not edit a running installation's profile files to try a change. Keep the
original profile and evidence, state exactly what the candidate changes, and
test the original successful cases as well as the new case. Where it matters,
include missing/malformed input, valid zero, aliases, wrong device/firmware,
stale data, cancellation and an uncertain command result.

Control support needs evidence for the exact native request, the router's
acknowledgement and a later fresh reading of the resulting settings on the same
device. Keep manual overrides, device/SIM checks and no-blind-retry safeguards.
Do not require a tester to make radio changes before accepting a useful
monitoring report. Simulator/replay results and physical results remain separate.

## Keep changes reviewable

Explain the concrete problem and the resulting behavior. Trace the existing owner before adding a helper, cache or transport path. Prefer one bounded correction over parallel state or a second controller. Separate physical firmware evidence, sanitized native captures, simulations and software-only tests.

For behavior changes, retain a realistic failing input first, make the smallest coherent fix, and run the failed and affected cases. Protect successful replies and test missing, malformed, zero, conflict, stale and wrong-device boundaries when they affect the change. Report the actual commands and results; do not call a scoped closure a fresh full-suite pass.

## Preserve data and control boundaries

A read-format improvement may activate an understood field, but it cannot infer units, radix, RAT identity or write permission. Isolate an invalid optional field while keeping independent clear fields from the same response. Do not merge unrelated reads, borrow stale values as fresh, or reuse another device's History.

Changes to radio controls need their existing identity, SIM, profile, acquisition and acknowledgement guards. Hardware claims require evidence scoped to the actual model and firmware. Ordinary offline tests must not contact a router.

## Write useful comments

Explain intent, native input meaning and units, ownership boundaries and non-obvious behavior. Keep API identifiers accurate. Avoid private names, machine paths, personal network details and session-specific narratives. Do not restate obvious code. Keep public research links in the protocol references document when a short technical explanation is clearer in code.

Preserve copyright, licence and required attribution notices. Do not remove another author's rights or claim borrowed implementation as original. Comment-only changes should preserve executable tokens and AST. Review docstrings separately when tools or generated definitions consume them.

## Protect privacy

Use synthetic or explicitly sanitized fixtures. Never submit passwords, tokens, personal addresses, SIM/device identifiers, full support bundles or complete History in an issue or pull request. Report security-sensitive material through the published private security contact.

Before sharing even a short excerpt or screenshot, remove login/session data,
activation codes, serial/IMEI/IMSI/ICCID, phone numbers, personal network addresses,
precise location and private APN/configuration data. Do not post a whole router
backup or firmware capture. A support export's automatic masking is not a
reason to skip your own review. Explain what was removed so missing evidence
is not mistaken for a device failure.

If you cannot make a useful excerpt safe, post the model, app version and a
plain description without an attachment. Use the private project contact in
[SECURITY.md](SECURITY.md) to agree a safe method before sending sensitive
evidence. No upload service or response time is promised.


## Build and test from public source

Use [the Windows build guide](docs/BUILDING.md) and [the offline test guide](docs/TESTING.md). Keep dependency pins, schemas, profiles, fonts and required customer guides in the source package. A test list must identify its actual public inputs; a private full-suite result does not prove that a reduced public snapshot is complete.

Keep required notices and capture provenance. Do not remove privacy tests or rewrite proven hardware captures to make a change pass. Where an input cannot be published safely, use a reviewed sanitized equivalent or state the missing test explicitly.

## Existing activation

This release keeps the existing activation and generator behavior. The public
source can generate valid codes; that consequence is intentional. Licences
remain free and donations optional. Do not add signed-code migration,
replacement-code requirements, or new key provisioning as part of a comment,
documentation or public-source cleanup. Changes to activation need a separate
review and must keep the relevant existing tests meaningful.
