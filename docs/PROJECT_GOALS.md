# Project goals

54GUC aims to make supported mobile-broadband routers easier to understand and
manage, while being honest about what each model and firmware can do. These
are contribution goals, not a promise that every router or feature is supported.
They do not replace the app's existing safety checks or the hardware evidence
listed in the [hardware and limits guide](CGF_MUST_READ/BETA_PACKAGE/SUPPORTED_HARDWARE_AND_LIMITATIONS.txt).

## Our main need: real router testing

The most valuable help is testing the **current software** on routers or
firmware that lack complete hardware evidence, and gradually developing exact
profiles from those findings. People who can test their own hardware can help
even if they do not write code.

A report can start small: the exact model and firmware, the app version, what
can be monitored, a field that is missing or has a different format, and what
has not been tried. If the exact profile is absent, report the gap before
trying another model's profile. Use **Observe only** first where an exact
profile exists. Never bypass a guard or send an unknown command to force support.

See [Contributing](../CONTRIBUTING.md#help-test-more-routers) for the process
and issue forms. Keep evidence small and safe to share; do not post raw support
bundles, credentials, activation codes, device/SIM identifiers or History.

## Build support in useful steps

1. Identify the exact device and firmware; a similar name is not enough.
2. Understand its readable fields, their format and units. Keep unknown or
   missing information explicit instead of inventing a value.
3. Add a small profile correction with a realistic regression test. Preserve
   working replies and the facts in earlier hardware captures.
4. Review replay and simulator results as software evidence. Test the actual
   device separately when a willing owner and a safe plan are available.
5. Verify each control separately before calling it supported. A successful
   read, a sent command or a returned success message is not sufficient proof
   of a changed setting; a fresh later read must confirm the actual result.

Monitoring-only support is useful progress. More readable fields can improve
reports and decisions without enabling unproved controls. A new firmware or
protocol may need application work as well as a profile; the profile format
cannot make an unknown command safe by itself.

## Keep the information useful

Explain readings and limits in plain English. Separate current readings from
older retained values, a requested change from its final outcome, and an app
estimate from a provider guarantee. Make Site Report useful to an ordinary
user while keeping deeper evidence available to the offline Log Checker.

Keep each physical router's History separate. Profile improvements apply to
new readings; they must not rewrite earlier evidence to make a test pass.
Use the existing acquisition, control, privacy and learning owners rather than
adding a second controller or bypassing identity and freshness checks.

## Contribute within clear evidence and software rights

Use [Testing a change](TESTING.md) and [Building on Windows](BUILDING.md).
Report which checks actually ran and which inputs they used. A selected test
pass, simulator run, hardware test and Windows build prove different things.
Unpublished private inputs are not public test coverage, and an untested
router remains untested.

The original project uses [GPL-3.0-only](../LICENSE). Contributors keep their
copyright; third-party notices and licence terms remain in force. See
[licensing information](../LICENSING.md) and the contribution guide. Existing
activation is unchanged, licences remain free and donations optional.

This page announces no new source tag, download, supported-version policy,
automatic review service or delivery date. Use the actual release record and
the version shown in the app.
