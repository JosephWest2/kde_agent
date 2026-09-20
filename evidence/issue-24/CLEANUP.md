# Targeting and waits: installed lifecycle regression

All **21 scenarios passed** against committed revision `7cfa228e99918130cf201b4d62275a159504b5b3`.
Product modules and packaged scripts are unchanged from `8919c37`. The installed
build came from a clean archive of that product revision; all **34 Python module
hashes and 1 JavaScript resource hash** match the selected committed source.
The [complete receipt](cleanup/cleanup.json) and [unaltered artifact inventory](cleanup/selection.json)
retain the observations. No fallback cleanup was needed.

The longest fault-to-empty/finalization interval was **7.613 seconds**
(`blocked-release`), within the original 15-second bound. The matrix covers
normal and manager stop, failed startup/exec, bus/compositor/worker death and
freezes, held locks, client disconnect, missing sockets, blocked hooks/release,
first-failure retention and stale-generation replay. Each case independently
observed whole-service cgroup emptiness and ordinary descendant absence.

The deliberately blocked finalizer remains a negative recording case: service
termination passed, while complete artifact finalization required the recorded
explicit reconciliation. Empty controller delegation, `supervisor` infrastructure,
application subgroups and exact `.control` lifecycle hooks are retained.

An earlier [failed attempt](cleanup-negative/cleanup.json) passed its first five
cases, then observed transient cgroup emptiness during `bus-freeze` before
ExecStopPost wrote `terminal.json`. Its original runner, log, observations and
fallback cleanup are preserved in [cleanup-negative/](cleanup-negative/selection.json).
The corrected runner requires final recording and cgroup emptiness within the
same original fault deadline. For the deliberately blocked finalizer it instead
requires the service to settle in an inactive/failed state before recording the
expected missing-finalizer limitation. The correction was independently reviewed;
no product code, deadline or acceptance assertion was relaxed. The final complete
run above passed with that correction.

Reproduce with an isolated, non-editable committed build:

```sh
/path/to/venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

This matrix exercises existing lifecycle fixture hooks. Separate native #24
receipts qualify focus/wait behavior and selected control responsiveness. They
also retain observed lifetime loop gaps exceeding the 100 ms target; this cleanup
matrix does not establish broad latency qualification. Readiness input/capture
and production shutdown hook replacement remain provisional under #35. Existing
normal local storage and ordinarily killable process assumptions still apply.
