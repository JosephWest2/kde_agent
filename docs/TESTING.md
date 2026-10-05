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
| Portable | 543 | Python 3.11+, PyGObject (GLib/Gio), dbus-python, Pillow, `dbus-daemon`, `/usr/bin/python`, `/usr/bin/git` |
| Needs host services | 4 | A user systemd manager on `/run/user/$UID/bus` with a visible `app.slice` cgroup |
| Needs native build | 8 | libei at `/usr/lib/libei.so.1`; some need the exact reviewed build, `cc`, `pkg-config` and the libei header |

Skipped outside the host (all other modules are portable):

| Module | Skipped tests | Condition |
| --- | --- | --- |
| `test_lifecycle_process` | all 4 | No user service manager, user bus or `app.slice` cgroup: they start real `systemd-run --user` services |
| `test_libei_probe` | 5 of 9 | No `/usr/lib/libei.so.1`, or not the SHA-256 that `tools/libei_binding.py` pins; the header audit also needs `/usr/bin/cc`, `/usr/bin/pkg-config`, a `libei-1.0` entry it resolves without `PKG_CONFIG_PATH`, and `/usr/include/libei-1.0/libei.h` |
| `test_input_connection` | 3 of 31 | `agent_desktop.libei_binding.describe()` rejects or cannot find libei |

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
purpose. The runner has no libei at `/usr/lib`, so the 8 libei tests skip. It does
have a user systemd manager, so `test_lifecycle_process` runs there: 555 of the 563
tests. CI never runs the smoke or failure-path tests below.

## End-to-end smoke test

```sh
tools/setup.sh                      # installs the package; the session service runs it
python tests/integration/smoke.py --cli .local/dependencies/venv/bin/agent-desktop   # about 7 seconds
python tests/integration/smoke.py --cli .local/dependencies/venv/bin/agent-desktop --loop 5
```

The smoke test drives the installed `agent-desktop` through separate CLI
invocations, the way an agent would:

1. `doctor`, printing the KWin, libei, kdotool and Python versions it ran against,
   plus any untested-version warnings.
2. `session start`.
3. **Native fixture** (built once into `.local/smoke/`, rebuilt when its source changes):
   launch, windows, focus, `key ctrl+shift+t`, `key --hold 0.2 w`, `type 'aB!'`,
   `click --x 100 --y 50` and a right-button double click.
   Then `screenshot --window` and graceful `close`. After the fixture exits, its
   complete Wayland key log must show the exact key order, the effective Ctrl/Shift
   state when each key went down (back to none at the end), the text and a hold of
   about 200ms, and its button log must show each click's exact button and
   client position. `logs --app` must then report both logs complete, at the
   `.log` paths launch returned, with a tail matching the file.
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
| `generation` | `--generation` and a window ref from another generation | `generation_mismatch` for both |
| `compositor-death` | SIGKILL `kwin_wayland` | session `failed`, input refused with `session_unavailable`; `session status` and the manifest's `failure` name the compositor and SIGKILL; the app is `ended_by_session_stop` |
| `bus-death` | SIGKILL the private `dbus-daemon` | as above, naming the bus |
| `worker-sigkill` | SIGKILL the worker once the fixture sees the held key | the client gets `completion_unknown` |
| `worker-stopped` | SIGSTOP the worker, then `session stop` | stop still completes cleanly (about 2s) |
| `title-gone` | a fixture with a sibling and a dialog whose `--on-sigusr1` steps retitle the primary, close the sibling, then close the dialog; each signal is sent only after the running wait's first observation artifact appears, so no step depends on launch timing (`AGENT_DESKTOP_TEST_SLOW=SECONDS` adds setup delay to prove it) | `wait --for title` times out (context: phase `title_wait`, window, last query) on a title that never appears, matches the retitle on a later poll, matches a `--regex` on the first poll, ends a runaway `--regex` with `pattern_too_slow`, and fails with `target_lost` when the sibling closes mid-wait; `wait --for gone` on the dialog succeeds (`already_gone: false`) while the primary stays listed, then reports `already_gone`; a title wait on the gone dialog is `target_not_found` and a stale ref `generation_mismatch` |

It takes the same `--cli`, `--dependency-root` and `--verbose` options as the smoke
test. A failing scenario keeps its artifact directory and stops the run.

## When to run them

Run both after system updates (KWin, libei, PyGObject, Python), after rebuilding
kdotool, and before merging changes to input, capture, windows or lifecycle code.
If `doctor` warns about an untested version and both pass, update `TESTED_KWIN_VERSION` in `prerequisites.py` or `TESTED_VERSION`
in `libei_binding.py`.
