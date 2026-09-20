# Agent Desktop Toolkit

A local CLI for a dedicated headless KWin desktop. The production CLI currently
provides command parsing, structured contracts, and a private generation-bound
worker transport. Desktop operations remain unwired: absent sessions report
`session_not_found`, and a transport-only worker reports `unsupported_operation`.
`doctor` and `session start` remain explicitly unsupported.

See [CLI installation and command contracts](docs/CLI.md), the authoritative
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Prerequisite setup](docs/SETUP.md) and the [M1 decision](docs/M1_DECISION.md)
document the separately tested feasibility tools. They do not establish production
CLI support. No command controls the personal desktop.
