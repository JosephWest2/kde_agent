# Agent Desktop Toolkit

A local CLI for a dedicated headless KWin desktop. The production CLI currently
provides command parsing, structured contracts, and a private generation-bound
worker transport, durable records, and generation-owned service lifecycle.
Managed status/stop are implemented; an absent stop succeeds. Desktop operations
remain unwired. `doctor` and public `session start` remain explicitly unsupported
until their prerequisite/readiness checks are implemented. See the
[service lifecycle boundary](docs/LIFECYCLE.md).

See [CLI installation and command contracts](docs/CLI.md), the authoritative
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Prerequisite setup](docs/SETUP.md) and the [M1 decision](docs/M1_DECISION.md)
document the separately tested feasibility tools. They do not establish production
CLI support. No command controls the personal desktop.
