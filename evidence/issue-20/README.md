# Issue #20: real capability readiness and essential health

Implementation commit `bb028ab144c6b2e0a201bf6f35f15fcfb1d67a89` enables installed
public start only after correlated control, structured window metadata, persistent
resumed EIS input and a complete ScreenShot2 PNG all pass. Status uses live service
and worker health. A 5s native systemd watchdog with explicit 3s abort escalation
terminates frozen workers independently of later client requests.

The full suite passes **250 tests**. These include asynchronous bus authentication
and Hello stalls, late reply/FD disposal, every missing capability, strict query
and capture acceptance deadlines, retained specific failures, stale-generation
and old-metadata reconciliation, status budgets, and observing essential failure
before queued scheduler effects. Unit/provider fault seams are internal Python
fixtures, never public fake-readiness flags. The installed evidence below uses
real bus/KWin/libei/capture services and a wheel run from `/`.

This is **M1 provisional readiness**, not production release qualification. Ready
responses explicitly report `m1-provisional`, `release_qualified: false`,
`desktop_operations_supported: false` and replacement owner #35 (M7.1). Public
launch/window/input/screenshot operations remain unsupported. #21 still owns
ordered graceful shutdown and the autonomous terminal-record/runtime-cleanup hook.

Reproduce using fresh absolute output directories:

```sh
python -m venv --system-site-packages .local/issue20-venv
.local/issue20-venv/bin/python -m pip install --no-deps --no-build-isolation .
.local/issue20-venv/bin/python -I evidence/issue-20/installed_health.py \
  /absolute/new/health-run /absolute/project/.local/dependencies
.local/issue20-venv/bin/python -I evidence/issue-20/installed_query.py \
  /absolute/new/query-run
PYTHONWARNINGS=ignore python -m unittest discover -s tests -q
```

The query evidence script builds only the existing native Wayland test fixture.
Runtime prerequisites and the pinned local kdotool build must already exist;
these commands neither install native dependencies nor touch the personal desktop.
Final selected receipt manifests include exact source/binary hashes and original
paths. Earlier local implementation/recorder attempts remain development history;
only the final `bb028ab` runs are attributed to the committed implementation.

## Installed lifecycle and autonomous health evidence

`health/health.json` records all nine passing cases from implementation commit `bb028ab144c6b2e0a201bf6f35f15fcfb1d67a89` using the installed wheel and independent CLI processes with cwd `/`. Every installed module/resource hash exactly matches the source hashes recorded for that commit. Doctor passed without launching a desktop; healthy start, duplicate reuse, live status, and normal stop passed. Maximum observed public start was 0.841s; maximum observed status was 0.089s.

| Injected fault | Owned cgroup first observed empty |
| --- | ---: |
| bus-SIGKILL | 0.269s |
| bus-SIGSTOP | 2.249s |
| compositor-SIGKILL | 0.250s |
| compositor-SIGSTOP | 2.249s |
| worker-SIGKILL | 0.025s |
| worker-SIGSTOP | 4.774s |
| frozen-starting | 5.026s |

The infrastructure-only installed worker remained healthy in `starting` for 6.124s before the separate frozen-starting test. Both frozen-worker cases received no client request between SIGSTOP and independently observed empty cgroup; systemd reported `Result=watchdog`. Exact service properties confirm the 5s watchdog, 3s abort/stop escalation, control-group killing, main-process notification, and no restart. Worker notification variables and their absence from the private bus child were inspected directly.

The six essential-process fault cases reconcile to failed, unavailable sessions. Each original worker/bus/compositor identity was gone, rejected desktop requests did not report success, and all seven real desktop generations retained fully decoded 1280×720 PNGs and their logs. Selected complete artifacts are under `health/<case>/`; `health/selected-artifacts.json` records their original generation/path and SHA-256. No fallback cleanup was needed.

These are bounded observations on this host, not realtime guarantees. Bus/compositor fault timing includes a deliberate 0.2s (SIGKILL) or 2.2s (SIGSTOP) wait before testing rejected admission, so those measurements are observation upper bounds. Kernel restrictions denied the compositor's `/proc/exe` and `/proc/environ` reads; its identity was verified using comm, parent PID, start ticks, and exact cgroup instead, and its notification-environment isolation was not directly observed here. The starting fixture deliberately omits desktop probes and establishes watchdog behavior only. Production readiness and release qualification remain false; #35 owns replacement of the provisional adapters. #21's autonomous terminal runtime-cleanup hook is outside this evidence: status performs the current post-quiescence reconciliation.

## Rebuilt kdotool qualification

Source commit: `bb028ab144c6b2e0a201bf6f35f15fcfb1d67a89`. Every recorded installed Python module hash and the exact packaged query JavaScript match that commit.

Command: `.local/issue20-venv/bin/python -I evidence/issue-20/installed_query.py /home/josephwest/development/kde-agent/.local/issue20-query/final-bb028ab`. The non-editable installed package ran in its own systemd user service with private bus/Wayland endpoints; the test wrapper kept the native fixture's stdin open. No public desktop action implementation was added.

The rebuilt pinned kdotool binary `b7a300d5a2f0b95a21d71dca5757328382bb6dd887e4ac975fffb59e2351bd21` passed 24 populated structured queries. Each query observed the same sole native fixture UUID/PID, title/class, focus, 640x360 client geometry and in-bounds frame. Every helper was reaped and every unique script name was confirmed unloaded. Maximum query work was 0.020050s against 0.5s, and maximum script cleanup was 0.001068s against 1.5s. There were 8 distinct independent periodic live bus/compositor observations.

The natural first readiness query contained 0 windows; the qualification waited for the native fixture's presentation before its 24 populated queries. Separate final status reported ready. Generation `3aa0c528515a4d359b85c20e8ffd2c88` stopped in 0.123338s, and its exact cgroup was empty with all recorded owned lifetimes exited.

Selected evidence is in `evidence/issue-20/query/`: authoritative summary and build receipt, all 24 per-query receipts and snapshots, exact scripts, native/owner/worker/compositor/bus logs, and `selection.json` with source commit and copied-file hashes. Full local run artifacts remain at `.local/issue20-query/final-bb028ab`.

This qualifies the rebuilt query helper for the provisional provider; it is new issue 20 evidence and does not retroactively change historical M1 receipts or identify old-build timings as current. Production release qualification remains false and replacement remains issue 35. Two earlier local recorder iterations failed after queries completed (mutable health snapshot bookkeeping, then a missing required make_request argument); those historical recorder errors remain only under `.local/issue20-query/run1` and `run2`. The authoritative final run completed without errors.
