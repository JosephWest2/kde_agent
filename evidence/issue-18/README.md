# Issue #18 service lifecycle evidence

The complete suite passed **165 tests in 14.721s** on the recorded systemd
261.3-1 target. It includes real user services, parallel duplicate starts,
start/stop ordering, worker death, child/grandchild cgroup cleanup, frozen-worker
fallback, generation replacement rejection and deterministic ownership/deadline
failure cases. An existing dependency-test `/proc` read was narrowly corrected to
accept kernel reaping between open/read; its initial failing run is not presented
as successful validation.

`installed-smoke.json` was produced by `installed_smoke.py` using a freshly built,
non-editable wheel in `/tmp/kde-agent-issue18-venv`, exposing distribution gi.
The production default worker command and independent installed CLI invocations
ran from `/` and `/tmp` without PYTHONPATH. A tests-only fixture separately supplied
ordinary descendants and the ignored-SIGTERM/frozen-record-lock condition. It is
not a production plugin or fake-desktop mode.

The receipt records actual service properties, generation identities, PID birth
identities and terminal manifests. Ordinary stop completed around 0.1s; fallback
with a missing control socket and frozen worker ignoring SIGTERM completed around
3.1s. A status with a 100ms budget returned unavailable around 0.14s including fresh
CLI startup. Exact timings and wheel checksum are in `summary.json`. All recorded
owned lifetimes exited, terminal artifacts survived stop, repeated stop succeeded,
and stale start/status/stop requests left the replacement untouched. Every test
unit was stopped/reset in teardown.

`source-sha256.json` matches all Python files in the installed package and current
source. Reproduce with the distribution Python, a live user manager, and no global
installation:

```sh
/usr/bin/python -m unittest discover -s tests -p 'test_*.py'
/usr/bin/python -m venv --system-site-packages /tmp/kde-agent-issue18-venv
/tmp/kde-agent-issue18-venv/bin/pip install --no-build-isolation --force-reinstall .
/tmp/kde-agent-issue18-venv/bin/python -I evidence/issue-18/installed_smoke.py
```

Every observed service is infrastructure only: `desktop_ready: false`. Public
`session start` remains gated on #20. Private KWin/settings (#19), capability
readiness/essential health (#20), and autonomous terminal cleanup plus graceful
release/close orchestration (#21) remain outside this receipt. This does not qualify
a production desktop or close parent #3.
