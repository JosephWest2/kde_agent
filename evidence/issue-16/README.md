# Issue #16 infrastructure verification

The checked source hashes in [summary.json](summary.json) passed **119 tests** on
Python 3.14.7 / Arch Linux with distribution GLib/PyGObject. The suite ran in 9.702s.
These are task-double measurements with a real GLib worker, Unix sockets and
separate CLI processes, under normal local storage and ordinary host scheduling.
No private KWin, input adapter, compositor or desktop service was started.

| Observation | Samples | Maximum |
| --- | --- | --- |
| saturated_control_to_cancel | 1 | 5.029 ms |
| worker_timeout_detection | 1 | 2.398 ms |
| sigint_or_disconnect_to_cancel | 2 | 3.180 ms |
| flood_timeout_detection | 1 | 1.903 ms |

The cancellation samples include a worker with all 32 ordinary slots occupied,
CLI SIGINT and socket EOF. The flood test continuously sends valid and malformed
controls while a child stalls, produces 1MB output to DEVNULL, and is cancelled
and reaped. Expiry samples measure detection after the admission work deadline;
they do not widen success acceptance. These small sample counts establish the
infrastructure test result, not a universal timing guarantee or real input release.
M5/M7 retain real-adapter validation. Cancellation dispatch/release attempt targets
100ms; M1's 500ms fixture-observed actual release remains a separate concept.

[Installed smoke](installed-smoke.json) used an independently built/installed wheel
in a disposable venv exposing distribution PyGObject, with PYTHONPATH removed and
cwd outside the checkout. Two separate CLI invocations reached the packaged worker
and received unsupported results with its verified identity; stop stayed explicitly
unsupported. A tests-only Python launcher then injected tasks into the installed
worker. SIGINT cancelled its matching request, cleanup completed, and a subsequent
request succeeded. The installed cancellation observation was 4.260ms.

Reproduce the suite:

```sh
PYTHONWARNINGS=ignore python -m unittest discover -s tests -v
```

The process fixture is only an internal Python injection. No production CLI flag,
environment selector or wire command can enable it. The source hashes distinguish
this measured implementation from later changes; artifact paths in the smoke JSON
refer to the disposable test environment, not persistent user configuration.

The fresh-review corrections add regressions for factory/observer work crossing a
request deadline (including equality), and a short stop deadline while superseded
reset cleanup stalls. Emission uses fresh timestamps; reset cleanup and pending stop
share the original stop budget, with separate shorter/longer waiter results. An
automatic abort-escalation double also creates or joins one shutdown owner without
renewing its deadline. The metrics and source hashes above were refreshed after
these fixes; git history retains the pre-review evidence.
