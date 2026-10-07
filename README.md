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
- pointer hover (`move`) and mouse-wheel scrolling (`scroll`) at a window or screen point;
- drags with any button (`drag`), and ctrl, shift or alt held around a click, scroll or drag (`--modifiers`);
- full-screen or single-window screenshots;
- graceful close and explicit kill;
- bounded log tails for the session and each application.

Losing focus during a hold, a long `type`, a long `scroll` or a `drag` releases and stops the input.

## Quickstart for coding agents

Set up once with `tools/setup.sh` ([setup](docs/SETUP.md): Arch packages, rustup,
what it does). It builds the pinned dependencies, installs the CLI into
`.local/dependencies/venv/bin/agent-desktop` (the session service runs the
installed package) and runs `agent-desktop doctor`. Rerun it after pulling changes.
Then, from the checkout:

```sh
export PATH="$PWD/.local/dependencies/venv/bin:$PATH"
D="agent-desktop --json"
$D session start                                 # about 1s; reuses a running session
$D launch --wait-window -- gnome-text-editor     # → result.application.ref, result.windows[].window.ref
$D windows --app APP_REF                         # client/frame geometry, title, active
$D focus --window WIN_REF                        # input requires a focused window
$D type --window WIN_REF 'Hello'
$D key --window WIN_REF ctrl+a                    # chords; ctrl+s would open a modal Save dialog
$D wait --for title --window WIN_REF --match 'Hello'   # did the app react? (or --regex '^Hello')
$D wait --for gone --window DIALOG_REF            # a dialog closed; the app keeps running
$D click --window WIN_REF --x 107 --y 23         # client-area pixels, as in a window screenshot
$D scroll --window WIN_REF --x 350 --y 300 --dy 3 # 3 wheel notches down at that point (negative: up)
$D drag --window WIN_REF --from 100,50 --to 300,150 --modifiers shift  # press, move, release
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
  window. It does not mean the application handled it. Confirm with
  `wait --for title` or `wait --for gone` when the title or a window changes,
  otherwise with a screenshot or the app's logs.
- **Nothing is retried.** If a response is lost (`completion_unknown`), the action
  may or may not have happened; look before repeating it.
- **Artifacts** (screenshots, logs, `manifest.json` with versions, the failure
  cause and the apps that ran) stay under `.agent-desktop/artifacts/generations/GEN/`
  after the session stops ([layout](docs/ARTIFACTS.md)).

**MCP.** `agent-desktop mcp` serves the same commands as MCP tools over stdio,
with screenshots returned as images. Register it with
`claude mcp add agent-desktop -- $PWD/.local/dependencies/venv/bin/agent-desktop mcp --dependency-root $PWD/.local/dependencies`
(run from the checkout); see [MCP server](docs/MCP.md) for the tools, cancellation
and what happens to sessions when the client disconnects (nothing: they keep running).

[Agent recipes](docs/AGENT_RECIPES.md) cover stale refs, focus before input,
modal dialogs, waiting for the app to react (`wait --for title|gone`), client
versus screen coordinates, and recovering from
`completion_unknown` and `input_uncertain`.

[Target applications](docs/TARGET_APPS.md) records how gnome-text-editor, GIMP 3
and Blender behave in the private desktop: startup, splash and first-run dialogs,
file dialogs and keyboard paths that work.

[docs/TESTING.md](docs/TESTING.md) covers the unit tests, the end-to-end smoke
test, failure-path tests, MCP tests and optional application tests
(`python tests/integration/smoke.py`, `failures.py`, `mcp.py` and `apps.py`, each with
`--cli .local/dependencies/venv/bin/agent-desktop` unless that directory is on PATH). See [application ownership](docs/APPLICATIONS.md),
[window discovery](docs/WINDOWS.md) and [CLI commands](docs/CLI.md).
The readiness screenshot is an internal diagnostic artifact.

See [CLI installation and commands](docs/CLI.md), [service lifecycle](docs/LIFECYCLE.md),
[requirements](REQUIREMENTS.md), and [architecture](ARCHITECTURE.md).
[Setup](docs/SETUP.md) covers the host, prerequisites and `tools/setup.sh`;
[the M1 decision](docs/M1_DECISION.md) records the original adapter evidence. [Validation history](docs/VALIDATION.md) summarizes completed work; [the revised plan](planning/REVISED_PLAN.md) is the historical plan, and open work is tracked in GitHub issues.
No command controls the personal desktop.
