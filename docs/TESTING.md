# Testing

## Unit tests

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

These need no desktop. Some use real native resources (libei, pipes, sockets)
but never start KWin.

Tests that need a facility only the real host has check for it and skip with
the reason (`tests/host_facilities.py`). On the host, insist on full coverage:

```sh
AGENT_DESKTOP_REQUIRE_HOST_TESTS=1 PYTHONPATH=src python -m unittest discover -s tests
```

With `AGENT_DESKTOP_REQUIRE_HOST_TESTS=1` a missing facility fails the test instead
of skipping it. On a complete host nothing skips either way.

### Categories

| Category | Tests | What they need |
| --- | --- | --- |
| Portable | 731 | Python 3.11+, PyGObject (GLib/Gio), dbus-python, Pillow, `dbus-daemon`, `/usr/bin/python`, `/usr/bin/git` |
| Needs host services | 4 | A user systemd manager on `/run/user/$UID/bus` with a visible `app.slice` cgroup |
| Needs native build | 9 | libei at `/usr/lib/libei.so.1`; some need the exact reviewed build, `cc`, `pkg-config` and the libei header |

Skipped outside the host (all other modules are portable):

| Module | Skipped tests | Condition |
| --- | --- | --- |
| `test_lifecycle_process` | all 4 | No user service manager, user bus or `app.slice` cgroup: they start real `systemd-run --user` services |
| `test_libei_probe` | 6 of 10 | No `/usr/lib/libei.so.1`, or not the SHA-256 that `tools/libei_binding.py` pins; the two header audits (the probe's table and the production `agent_desktop.libei_binding` table) also need `/usr/bin/cc`, `/usr/bin/pkg-config`, a `libei-1.0` entry it resolves without `PKG_CONFIG_PATH`, and `/usr/include/libei-1.0/libei.h` |
| `test_input_connection` | 3 of 33 | `agent_desktop.libei_binding.describe()` rejects or cannot find libei |

`test_prerequisites` also checks the real libei only when `/usr/lib/libei.so.1`
exists; the rest of that test always runs.

### CI

`.github/workflows/tests.yml` runs the unit tests on pull requests and pushes to
`main`, on `ubuntu-24.04` with its system Python 3.12 and the distribution's
`python3-gi`, `python3-dbus` and `python3-pil`. It runs the same discovery through

```sh
PYTHONPATH=src python tests/run_ci.py
```

which prints how many tests ran, were skipped and were executed, with each skip's
reason, to the log and the job summary. It checks skips by test id, not by output
text: a skip in any module other than the three above fails the job, as do failures,
errors and an empty run. A new host skip has to be added to `HOST_MODULES` there on
purpose. The runner has no libei at `/usr/lib`, so the 9 libei tests skip. It does
have a user systemd manager, so `test_lifecycle_process` runs there: 735 of the 744
tests. CI never runs the smoke, failure-path or application tests below.
`test_app_checks` covers the application tests' desktop-free checks (finding the
canvas, the exported pixels, version and skip handling) and is portable.

## End-to-end smoke test

```sh
tools/setup.sh                      # installs the package; the session service runs it
python tests/integration/smoke.py --cli .local/dependencies/venv/bin/agent-desktop   # about 11 seconds
python tests/integration/smoke.py --cli .local/dependencies/venv/bin/agent-desktop --loop 5
```

The smoke test drives the installed `agent-desktop` through separate CLI
invocations, the way an agent would:

1. `doctor`, printing the KWin, libei, kdotool and Python versions it ran against,
   plus any untested-version warnings.
2. `session start`.
3. **Native fixture** (built once into `.local/smoke/`, rebuilt when its source changes):
   launch, windows, focus, `key ctrl+shift+t`, `key --hold 0.2 w`, `type 'aB!'`,
   `click --x 100 --y 50`, a right-button double click, `move --x 200 --y 100`,
   `scroll --x 210 --y 110 --dx -1 --dy 3` and a screen-coordinate `scroll --dy -2`.
   Then `screenshot --window` and graceful `close`. After the fixture exits, its
   complete Wayland key log must show the exact key order, the effective Ctrl/Shift
   state when each key went down (back to none at the end), the text and a hold of
   about 200ms, and its button log must show each click's exact button and
   client position. After the last click its pointer log must show exactly the
   three motions and five wheel frames in order: each frame at the scroll point,
   with `axis_value120` ±120 per notch (horizontal first in the diagonal step),
   `axis` values of the same sign, and no `axis_stop`. `logs --app` must then report both logs complete, at the
   `.log` paths launch returned, with a tail matching the file.
   A second fixture then gets `drag --from 100,50 --to 300,150 --duration 200
   --modifiers ctrl,shift`, `click --count 2 --modifiers alt`, `scroll --dy 2
   --modifiers ctrl` and `drag --button middle --duration 0`. Its complete log must
   show exactly: the motion to the start, ctrl and shift down, the button down,
   20 motions of (10, 5) to the end, the button up there, shift and ctrl up; alt
   around both clicks; ctrl around both wheel frames; and the middle drag's single
   motion between its press and release. Each pointer event is checked against the
   XKB modifier mask in effect when it arrived (none at the end), and the drag's
   press to last motion must take about 200ms.
4. **gnome-text-editor:** launch, focus, `wait --for focus`, `type`, then
   `wait --for title` until the title contains the typed text. A click on the header bar's
   "New Tab" button must switch the same window to a "New Document"
   (`wait --for title --regex '^New Document'`), and
   `ctrl+page_up` must bring the typed document back (`wait --for title`).
   `ctrl+o` opens the "Pick Files" dialog as a second window; `escape` closes it,
   `wait --for gone` confirms that, and the editor must be the app's only window. Before that, a right-click on
   empty header-bar space opens KWin's window menu: the full `windows` query must
   list it as a `compositor` row while the editor stays active, `key --window`
   must fail with `target_lost` (reason `compositor_surface_open`, outcome
   `not_started`), and a screen click outside both must close it again.
   After the dialog, `type` adds a marker line 12 lines down and enough blank
   lines to overflow, and `ctrl+home` goes to the top. Window screenshots are
   reduced to text bands, the tops of runs of rows with at least 3 non-background
   pixels; this ignores the caret and crops the overlay scrollbar. After
   `scroll --dy 3` the last band (the marker) must rise by more than 10px, and after
   `scroll --dy -3` the bands must return to within 3px of where they started.
   Each check polls screenshots until two in a row agree.
   Then `key ctrl+a`, a full and a
   window screenshot, and `kill`. It runs with the session's private HOME/XDG
   directories, so your own editor state is untouched.
5. `session stop`. No process may remain in the generation's cgroup, the systemd
   unit must be gone, the manifest must say `stopped` (not `failed`) and list
   the KWin, libei and kdotool versions, the observed output and both
   applications, and the shutdown record and screenshots must exist.

Stop, the leak check and the artifact check each run even when an earlier step,
or stop itself, fails. Stop is by session name, so it also runs if `session start`
timed out before reporting its generation. A failing run prints the step, the
error payload and the kept artifact directory.
Passing runs delete their artifacts unless you pass `--keep-artifacts`.

Options:
- `--cli PATH`: an `agent-desktop` that isn't on PATH, such as the one
  `tools/setup.sh` installs (default: `agent-desktop` on PATH).
- `--dependency-root PATH`: defaults to `.local/dependencies`.
- `--no-fixture`: skip the native fixture (it needs gcc, wayland-protocols and xkbcommon headers).
- `--no-editor`: skip gnome-text-editor.
- `--verbose`: print every result.

## Failure-path tests

```sh
python tests/integration/failures.py --cli .local/dependencies/venv/bin/agent-desktop   # all scenarios, about 30 seconds
python tests/integration/failures.py --cli .local/dependencies/venv/bin/agent-desktop focus-loss cancel-hold --loop 3
```

Each scenario starts its own session with the native fixture, breaks something on
purpose, checks what is reported, then stops the session and requires that no
process remains in the generation's cgroup and the systemd unit is gone:

| Scenario | What it does | What must happen |
| --- | --- | --- |
| `focus-loss` | `kdotool` activates a second fixture window 0.8s into `key --hold 2 w` | `target_lost`/`focus_lost` within 1.6s; the fixture sees the release |
| `cancel-hold` | Ctrl-C on the client 0.6s into `key --hold 2 w` | `cancelled`, exit 130, release within 1s, and the next `key` works |
| `cancel-type` | Ctrl-C 0.8s into typing 1500 characters | `cancelled`; no character arrives after the cancel |
| `scroll-interrupted` | Ctrl-C once the fixture has 3 wheel steps of `scroll --dy 50`, then a `scroll --dy 1`, then `kdotool` activates a second fixture window once a second `scroll --dy 50` has 3 steps | `cancelled` (exit 130) and the wheel stops; the next scroll works; then `target_lost`/`focus_lost` (outcome `unknown`) whose `steps_sent`/`dy_sent` equal the steps the fixture received on either window; never an `axis_stop` |
| `drag-interrupted` | A 2s `drag --from 20,20 --to 420,320 --modifiers ctrl,shift`, three times: Ctrl-C after 5 motions; `kdotool` activates a second fixture window after 10; `session stop` after 5 | each time the fixture sees exactly the motion to the start, ctrl, shift, the button, some motions under ctrl+shift, then button, shift and ctrl up and nothing after. Ctrl-C gives `cancelled` (exit 130) with release within 0.5s; focus loss gives `target_lost`/`focus_lost` (outcome `unknown`) with `steps_sent` equal to the motions received and release within 1s; stop fails the client and releases within 0.5s |
| `generation` | `--generation` and a window ref from another generation | `generation_mismatch` for both |
| `compositor-death` | SIGKILL `kwin_wayland` | session `failed`, input refused with `session_unavailable`; `session status` and the manifest's `failure` name the compositor and SIGKILL; the app is `ended_by_session_stop` |
| `bus-death` | SIGKILL the private `dbus-daemon` | as above, naming the bus |
| `worker-sigkill` | SIGKILL the worker once the fixture sees the held key | the client gets `completion_unknown` |
| `worker-stopped` | SIGSTOP the worker, then `session stop` | stop still completes cleanly (about 2s) |
| `title-gone` | a fixture with a sibling and a dialog whose `--on-sigusr1` steps retitle the primary, close the sibling, then close the dialog; each signal is sent only after the running wait's first observation artifact appears, so no step depends on launch timing (`AGENT_DESKTOP_TEST_SLOW=SECONDS` adds setup delay to prove it) | `wait --for title` times out (context: phase `title_wait`, window, last query) on a title that never appears, matches the retitle on a later poll, matches a `--regex` on the first poll, ends a runaway `--regex` with `pattern_too_slow`, gives `invalid_regex` from the helper for a bad pattern sent straight over the transport (skipping the CLI's compile), and fails with `target_lost` when the sibling closes mid-wait; `wait --for gone` on the dialog succeeds (`already_gone: false`) while the primary stays listed, then reports `already_gone`; a title wait on the gone dialog is `target_not_found` and a stale ref `generation_mismatch` |

It takes the same `--cli`, `--dependency-root`, `--keep-artifacts` and `--verbose`
options as the smoke test. A failing scenario keeps its artifact directory and stops
the run.

## MCP tests

```sh
python tests/integration/mcp.py --cli .local/dependencies/venv/bin/agent-desktop   # all four, about 10 seconds
python tests/integration/mcp.py --cli .local/dependencies/venv/bin/agent-desktop cancel disconnect --loop 3
```

A minimal MCP client runs `agent-desktop mcp` over stdio, as Claude Code would,
and drives the native fixture through it ([MCP server](MCP.md)). Each scenario
starts its own session through the server and ends with the same leak check as
the other tests:

| Scenario | What it does | What must happen |
| --- | --- | --- |
| `flow` | legacy `initialize` (2025-11-25), `tools/list`, then `session_start`, `launch`, `windows`, `focus`, `type 'aB!'`, `key ctrl+shift+t`, `screenshot` of the window and of the screen, `close`, `session_stop` | the fixture's key receipts in order; each screenshot has one PNG image block whose size and SHA-256 match the result and whose bytes equal the stored file; the full screen is 1280×720; text, `structuredContent` and `isError` agree |
| `cancel` | modern per-request `_meta` (2026-07-28, after `server/discover`); `notifications/cancelled` while `key --hold 2 w` is held | release within 0.5s of the cancel (fixture clock); no response for the cancelled call; the next `key` works |
| `disconnect` | the client closes the server's stdin while the key is held | release within 0.5s; the server exits 0; the session is still `ready` (CLI `session status`) and takes input |
| `server-kill` | SIGKILL the server while the key is held | release within 0.5s (the call process's parent-death signal); the session keeps running |

It takes the same `--cli`, `--dependency-root`, `--loop`, `--keep-artifacts` and
`--verbose` options as the failure-path tests, and prints the server's stderr if
there was any.

## Application tests

```sh
python tests/integration/apps.py --cli .local/dependencies/venv/bin/agent-desktop   # all three, about 25 seconds
python tests/integration/apps.py --cli .local/dependencies/venv/bin/agent-desktop gimp blender --loop 3
```

Optional tests against real target applications, which only the host has. Each
scenario starts its own session, drives the application through the CLI, checks
what the application wrote to disk, then requires the same clean stop, leak and
artifact checks as the smoke test. Per-application quirks and the keyboard paths
are in [TARGET_APPS.md](TARGET_APPS.md).

| Scenario | Application | What it does | What must hold |
| --- | --- | --- | --- |
| `text-editor` | `/usr/bin/gnome-text-editor` | types three lines (punctuation, a tab), `ctrl+s`, the path into the `Save a File` dialog, `return`; then appends a line and `ctrl+s` again | the dialog closes (`wait --for gone`), the title names `saved.txt`, and the file's bytes are exactly the typed text plus a final newline, both times; the second save opens no dialog; `close` exits |
| `gimp` | `/usr/bin/gimp`, version 3 or later | `ctrl+n`, 320×240 in the new-image dialog, one `click` on image pixel (80, 60) (found in a window screenshot), one `drag` from image pixel (140, 190) to (280, 120), `ctrl+shift+e`, the path, `return` in both export dialogs; `ctrl+q`, `ctrl+d` | the exported PNG is 320×240, dark at (80, 60) and at 9 evenly spaced points from (140, 190) to (280, 120), white (≥ 250) everywhere more than 34px from both the dot and the stroke's line segment, and the dot's dark area is centered on (80, 60) within 3px; GIMP exits |
| `blender` | `/usr/bin/blender`, 4.2 or later | prepares `scene.blend` and a `userpref.blend` with `blender -b`, opens the scene with `--factory-startup`, `move` into the viewport, `shift+d` `return`, `ctrl+shift+s`, the name `edited.blend` in the file view, `return` twice | the title names `edited.blend` without `*`; `blender -b` lists Camera, Cube, Cube.001 and Light in it and the original three objects in `scene.blend`; `close` exits |

An application that isn't installed, or is older than shown, is skipped with the
reason (`skip gimp: /usr/bin/gimp is not installed`), and the run still passes.
`AGENT_DESKTOP_REQUIRE_HOST_TESTS=1` makes that a failure, as for the unit tests.
Version probes and `blender -b` run outside the session with no display and HOME
in the work directory.

Each scenario gets a temporary work directory for the files it saves and the
application's own profile (`GIMP3_DIRECTORY`, `BLENDER_USER_RESOURCES`); the rest
of its state is in the session's private HOME. A passing scenario deletes the work
directory and artifacts; a failing one keeps both and prints where. The options are
the same as for the failure-path tests.

## Owner-loop profile

An opt-in measurement of how late the worker's owner loop runs and what held it
up (#80; results in [VALIDATION.md](VALIDATION.md#80-owner-loop-stalls)). Set
`AGENT_DESKTOP_PROFILE_OWNER=1` in the environment of `session start`. The CLI
passes this one setting into the worker's otherwise clean environment, and each
generation then writes `logs/owner-profile.jsonl`. Without it the worker doesn't
import the profiler and nothing is wrapped.

```sh
mkdir -p ~/profile-runs        # the tests put artifacts in $TMPDIR
AGENT_DESKTOP_PROFILE_OWNER=1 TMPDIR=~/profile-runs python tests/integration/smoke.py --cli .local/dependencies/venv/bin/agent-desktop --keep-artifacts --loop 5
AGENT_DESKTOP_PROFILE_OWNER=1 TMPDIR=~/profile-runs python tests/integration/failures.py --cli .local/dependencies/venv/bin/agent-desktop --keep-artifacts --loop 2
python tools/owner_profile.py ~/profile-runs
```

`/tmp` is often tmpfs, where fsync costs nothing. Point `TMPDIR` at the real disk
to see what the default artifacts root (`.agent-desktop/artifacts` under the
caller's directory) costs. Remove the directory afterwards.

The profile records:
- **Probe lateness.** A 5ms GLib timeout records how far past due each probe
  ran. Probes 10ms or more late are written one by one, with the callbacks that
  overlapped them.
- **Callbacks.** Each owner callback (a `Server.service` turn, a D-Bus reply,
  the EIS fd watch) is timed with the calls nested in it: request dispatch,
  scheduler steps, Store methods with their calling `file:line`, `os.fsync` with
  its caller, child spawns, private-bus calls, screenshot and window-query steps,
  imports and garbage collection. Since #96 the worker's Store calls run on
  the writer thread, which the profile does not time; on the owner they show as
  `Journal.submit` and `Journal.drain`, and the response wait as
  `Scheduler.settle`. Totals per call path are written every 5s.
  Callbacks of 10ms or more are written one by one, with their spans of 0.5ms or
  more.
- **Input and responses.** Each press, release, scroll and move, with its
  intended hold or gap, the held count before and after, and whether input
  was left uncertain; focus recheck start (and when it was due) and end; and
  each response's error code.
- **Its own writes.** Profile lines are written on the owner thread too. Their
  time is recorded as `profiler.write`, so a slow write shows up as its own
  cause and isn't added to the next callback.

`tools/owner_profile.py` takes artifact directories or profile files. It prints:
- lateness per generation and overall (p50, p95, p99, max, and counts over 50,
  100 and 250ms);
- self time by category, and Store time split into fsync and the rest;
- the top contributors by total and by worst single call, with their heaviest
  path;
- the worst late probes, with the spans behind them;
- what triggered each stall of 50ms or more;
- stalls during a held key or button. A hold ends only at a confirmed release
  (nothing held afterwards and input not uncertain); otherwise it runs to the
  end of the profile;
- startup failures;
- input timing against its intended hold, gap, scroll pace and recheck time;
- timeouts, with the owner lateness that overlapped each one in the same
  profile;
- profiles that stopped early, at the byte budget or after a profiler fault.

It leaves `worker-stopped` out of the totals (`--exclude`), because that
scenario freezes the worker on purpose.

The profile costs about 1µs per wrapped call, plus 2–3ms on the owner for the
summary it writes every 5s. A smoke run took as long with it as without (about
11s on tmpfs). A smoke generation's profile is about 1.2 MB.

Limits:
- Recording stops at 8 MiB per generation, after a final summary and a
  `truncated` record. That is a few minutes of smoke-like activity.
- A fault in the profiler turns it off with a `failed` record. The worker
  carries on, and the profiler still unwraps everything at exit.
- The profile's own writes are synchronous on the owner thread, so on a
  throttled or saturated disk they can block the owner like any other write.
  They are charged to `profiler.write`. On btrfs that was 0.7–0.8% of owner
  time idle and 0.1% under write load, and no single write took over 7ms
  ([VALIDATION.md](VALIDATION.md#budgets-now-writer-thread-next)).

## When to run them

Run both after system updates (KWin, libei, PyGObject, Python), after rebuilding
kdotool, and before merging changes to input, capture, windows or lifecycle code.
Run the application tests too after updating one of those applications or changing
input or window code.
If `doctor` warns about an untested version and both pass, update `TESTED_KWIN_VERSION` in `prerequisites.py` or `TESTED_VERSION`
in `libei_binding.py`.
