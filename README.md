# Agent Desktop Toolkit

A local CLI for a dedicated headless KWin desktop. `doctor` reports installed
runtime prerequisites; `session start`, `status` and `stop` manage private,
generation-owned services. Start requires real control, structured window query,
resumed input and complete screenshot probes. Live bus/compositor observations
and a systemd watchdog detect essential failures. Independent service hooks
terminate owned descendants, remove private settings and preserve terminal
artifacts even when the worker dies or freezes.

Readiness currently uses a packaged **M1 provisional provider**. Results report
`release_qualified: false` and replacement issue #35 (M7.1). Public desktop
launch and structured window discovery are supported with durable cgroup
ownership and conservative process association. Window waits, input and
screenshots remain explicitly unsupported; see [application ownership](docs/APPLICATIONS.md)
and [window discovery](docs/WINDOWS.md).
The readiness screenshot is an internal diagnostic artifact.

See [CLI installation and commands](docs/CLI.md), [service lifecycle](docs/LIFECYCLE.md),
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Prerequisite setup](docs/SETUP.md) prepares the pinned local dependency build;
[the M1 decision](docs/M1_DECISION.md) records the source adapter evidence.
No command controls the personal desktop.
