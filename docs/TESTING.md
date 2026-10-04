# Testing

## Unit tests

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

These need no desktop. Some use real native resources (libei, pipes, sockets)
but never start KWin.

## End-to-end smoke test

```sh
pip install .                       # the session service runs the installed package
python tests/integration/smoke.py   # about 5 seconds
python tests/integration/smoke.py --loop 5
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
   client position.
4. **gnome-text-editor:** launch, focus, `wait --for focus`, `type`, then a window
   query until the title contains the typed text. A click on the header bar's
   "New Tab" button must switch the same window to a "New Document", and
   `ctrl+page_up` must bring the typed document back.
   Then `key ctrl+a`, a full and a
   window screenshot, and `kill`. It runs with the session's private HOME/XDG
   directories, so your own editor state is untouched.
5. `session stop`. No process may remain in the generation's cgroup, the systemd
   unit must be gone, the manifest must say `stopped` (not `failed`), and the
   shutdown record and screenshots must exist.

Stop, the leak check and the artifact check each run even when an earlier step,
or stop itself, fails. Stop is by session name, so it also runs if `session start`
timed out before reporting its generation. A failing run prints the step, the
error payload and the kept artifact directory.
Passing runs delete their artifacts unless you pass `--keep-artifacts`.

Options:
- `--cli PATH`: an `agent-desktop` that isn't on PATH, such as a venv.
- `--dependency-root PATH`: defaults to `.local/dependencies`.
- `--no-fixture`: skip the native fixture (it needs gcc, wayland-protocols and xkbcommon headers).
- `--no-editor`: skip gnome-text-editor.
- `--verbose`: print every result.

**When to run it:** after system updates (KWin, libei, PyGObject, Python), after
rebuilding kdotool, and before merging changes to input, capture, windows or
lifecycle code. If `doctor` warns about an untested version and the smoke test
passes, update `TESTED_KWIN_VERSION` in `prerequisites.py` or `TESTED_VERSION`
in `libei_binding.py`.
