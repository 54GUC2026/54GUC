# Project status

## R26.2 is available

The Windows portable app and matching GPL source were released on **27 October 2026**.

- [Windows app and source downloads](https://github.com/54GUC2026/54GUC-Releases/releases/tag/R26.2)
- [Public source repository](https://github.com/54GUC2026/54GUC)
- [Project website](https://54guc2026.54guc2026.workers.dev)

The app is free. Existing activation codes continue to work, and donations are optional. Updates are downloaded manually from the release page.

## Where your help matters most

We are expanding hardware coverage. The source contains 48 profiles across six protocol families, with different levels of testing for individual models and firmware versions. Some have physical-router results; others are starting points supported by simulations or format research.

See the [hardware guide](CGF_MUST_READ/BETA_PACKAGE/SUPPORTED_HARDWARE_AND_LIMITATIONS.txt) for the current device details. A report from another firmware, a missing field or a partly working router helps choose the next improvement. [Contributing](../CONTRIBUTING.md) and the [profile workshop](ROUTER_PROFILES.md) explain how to start.

## Release checks

For R26.2, the original full native run recorded **17,995 passes, nine failures and 18 skips**. All nine failures were closed in failed/related groups: combined **18,004 distinct passes, 18 skips and no unresolved failures**. This is not a fresh all-pass full run. The 54 affected R26.2 release checks and original/copied/extracted Windows package checks passed. Matching source and Windows archives passed integrity, source-correspondence and privacy review. The public offline selection's731 passes/one skip remain historical R25.7 evidence, not a newly claimed R26.2 run.

Hardware coverage grows through real use as well as tests. In this release's simulator work, four cases passed, one completed with standalone-5G/action limitations, and seven planned cases were deferred. Those are useful software checks alongside the physical-router results in the hardware guide.

The public [test guide](TESTING.md) identifies the tests whose inputs are included in this repository. It is a practical place to start experimenting without a router.

<sub>Practical sharing tips and the licence link are in [project notes](PROJECT_NOTES.md).</sub>
