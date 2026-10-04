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
- graceful close and explicit kill;
- bounded log tails for the session and each application.

Losing focus during a hold or long `type` releases and stops the input.

## Quickstart for coding agents

Install once with `pip install .` (the session service runs the installed
package) and check the host with `agent-desktop doctor`. Then:

```sh
D="agent-desktop --json"
$D session start                                 # about 1s; reuses a running session
$D launch --wait-window -- gnome-text-editor     # → result.application.ref, result.windows[].window.ref
$D windows --app APP_REF                         # client/frame geometry, title, active
$D focus --window WIN_REF                        # input requires a focused window
$D type --window WIN_REF 'Hello'
$D key --window WIN_REF ctrl+s
$D click --window WIN_REF --x 107 --y 23         # client-area pixels, as in a window screenshot
$D screenshot --window WIN_REF                   # → result.path (PNG of the client area)
$D logs --app APP_REF --tail 50                  # app stdout/stderr tails and paths
$D close --app APP_REF                           # or: kill --app APP_REF
$D session stop
```

- **Refs** are `GENERATION:ID` strings copied from results. They belong to one
  session generation; after `session stop`/`start` every ref is stale and is
  refused with `generation_mismatch`. Query again rather than reusing old refs.
- **One application per session.** A second `launch` while one is running fails
  with `application_active`; close or kill it first.
- **Results:** stdout carries one JSON object with `ok`, `result` or `error`.
  `error.code` is stable and maps to the exit status
  ([table](docs/CLI.md#requests-results-and-errors)). The common ones:
  `target_lost` (window gone or not focused: `focus` it again),
  `timeout` (raise `--timeout`, up to each command's maximum),
  `session_unavailable` or `session_failed` (`session status` says why; stop and
  start a new session), and `input_uncertain` (stop and start).
- **`dispatched: true`** means the input reached the compositor for the focused
  window. It does not mean the application handled it. Confirm with a screenshot,
  `windows` (for example the title) or the app's logs.
- **Nothing is retried.** If a response is lost (`completion_unknown`), the action
  may or may not have happened; look before repeating it.
- **Artifacts** (screenshots, logs, `manifest.json` with versions, the failure
  cause and the apps that ran) stay under `.agent-desktop/artifacts/generations/GEN/`
  after the session stops ([layout](docs/ARTIFACTS.md)).

[docs/TESTING.md](docs/TESTING.md) covers the unit tests and the end-to-end smoke
test (`python tests/integration/smoke.py`), and failure-path tests
(`python tests/integration/failures.py`). See [application ownership](docs/APPLICATIONS.md),
[window discovery](docs/WINDOWS.md) and [CLI commands](docs/CLI.md).
The readiness screenshot is an internal diagnostic artifact.

See [CLI installation and commands](docs/CLI.md), [service lifecycle](docs/LIFECYCLE.md),
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Prerequisite setup](docs/SETUP.md) prepares the pinned local dependency build;
[the M1 decision](docs/M1_DECISION.md) records the source adapter evidence. [Validation history](docs/VALIDATION.md) summarizes completed work; [the revised plan](planning/REVISED_PLAN.md) tracks what remains.
No command controls the personal desktop.
