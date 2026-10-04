# Agent Desktop Toolkit

A local CLI for a dedicated headless KWin desktop. `doctor` reports installed
runtime prerequisites; `session start`, `status` and `stop` manage private,
generation-owned services. Start requires real control, structured window query,
resumed input and complete screenshot probes. Live bus/compositor observations
and a systemd watchdog detect essential failures. Independent service hooks
terminate owned descendants, remove private settings and preserve terminal
artifacts even when the worker dies or freezes.

Supported today:
- launch, with durable cgroup ownership;
- structured window discovery;
- verified focus;
- bounded waits;
- keyboard chords and US-layout text, with worker-owned key release;
- left, right and middle clicks (single, double, triple) in window or screen coordinates;
- full-screen or single-window screenshots;
- graceful close and explicit kill.

Input reset and logs are next (#67, #68). JSON results give
every application and window a ready-to-use `ref` string for `--app` and
`--window`. [docs/TESTING.md](docs/TESTING.md) covers the unit tests and the end-to-end smoke
test (`python tests/integration/smoke.py`). See [application ownership](docs/APPLICATIONS.md),
[window discovery](docs/WINDOWS.md) and [CLI commands](docs/CLI.md).
The readiness screenshot is an internal diagnostic artifact.

See [CLI installation and commands](docs/CLI.md), [service lifecycle](docs/LIFECYCLE.md),
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Prerequisite setup](docs/SETUP.md) prepares the pinned local dependency build;
[the M1 decision](docs/M1_DECISION.md) records the source adapter evidence. [Validation history](docs/VALIDATION.md) summarizes completed work; [the revised plan](planning/REVISED_PLAN.md) tracks what remains.
No command controls the personal desktop.
