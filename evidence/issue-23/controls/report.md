Installed-wheel controls/lifetime evidence passed on `e7d8827618c5eec1a72f45566582f37fd12713d6`.

Interpreter: `/home/josephwest/development/kde-agent/.local/issue23-final-venv/bin/python`. The receipt's installed `.py`/`.js` hashes exactly match frozen source. Fixed evidence-only missing-completion source: `callDBus = function () {};`, suppressing kdotool's completion callback while KWin remains responsive. Each stalled query's exact native script registration was observed before controls.

- priority_cancel: status 23.14 ms; cancellation dispatch 5.08 ms; complete cleanup 37.46 ms; maximum GLib gap 61.93 ms (includes startup). Query child reaped, exact native script absent, temporary directory removed; application retained running with identical PID birth identity and cgroup; filtered recovery query succeeded.
- disconnect: status 15.41 ms; cancellation dispatch 4.99 ms; complete cleanup 30.34 ms; maximum GLib gap 61.93 ms (includes startup). Query child reaped, exact native script absent, temporary directory removed; application retained running with identical PID birth identity and cgroup; filtered recovery query succeeded.
- Native empty title/app-ID mode: caption `''`, class `'wayland-fixture'`. Actual compositor defaults are retained.
- Native omitted title/app-ID mode: caption `''`, class `'wayland-fixture'`. Actual compositor defaults are retained.
- Native fixture timer closed the surface after 1600 ms, with a raw `close`/`shutdown` receipt; associated rows changed from one to zero, retained application state `all-exited`.
- Restart produced a new generation. Old application and explicit old generation queries returned `generation_mismatch`; fresh windows query succeeded.
- Both final generations and all earlier attempt generations are stopped; cgroups are absent or unpopulated (see `cleanup-audit.json`).

Raw evidence is retained in `receipt.json` and `artifacts/`. Earlier attempts remain separately in the sibling controls-first/second/third/fourth directories. The final runner retires only its own verified fixture through pidfd between cases to respect one-active-application admission.

This evidence does not qualify a release; replacement issue 35 remains the release gate.
