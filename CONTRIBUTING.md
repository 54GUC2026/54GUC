# Contributing

Start from the R29.0 source in this repository. Use the [Windows build guide](docs/BUILDING.md) and [offline test instructions](docs/TESTING.md).

Keep changes focused. For a modem field, test the native reply through normalization, Matrix and each affected consumer. Include valid zero, missing values, equivalent spelling, invalid units, conflicting values and expired readings. For a control, check the request, native acknowledgement and fresh configured-state readback.

Use synthetic identities and sanitized examples. Do not publish real passwords, saved activation codes, personal connection databases or private router captures. Report security problems through [SECURITY.md](SECURITY.md).

See [router profile development](docs/ROUTER_PROFILES.md) and [protocol references](docs/PROTOCOL_REFERENCES.md) when adding support for another firmware.
