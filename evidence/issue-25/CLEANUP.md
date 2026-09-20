# Graceful close: installed lifecycle regression

All **21 scenarios passed** against revision `b927445507d0bfa11519e45de2afbf3f016263b2`.
Its production modules and resources are unchanged from reviewed source
`9584243e6ab7fef57c9b263f4cef440dc9e8e4de`. The isolated non-editable build came
from a clean archive of that product revision. All **35 Python module hashes and
1 packaged JavaScript resource hash** match the selected source exactly.
The [complete receipt](cleanup/cleanup.json) and [unaltered artifact inventory](cleanup/selection.json)
retain observations and provenance. No fallback cleanup was needed.

The longest fault-to-empty/finalization interval was **7.556 seconds**
(`blocked-release`), within the original 15-second limit. Coverage includes
normal/manager stop, failed startup/exec, bus/compositor/worker death and freezes,
held locks, disconnect, missing sockets, blocked hooks/release, prior-failure
preservation and stale-generation replay. Every case independently observed
whole-service cgroup emptiness and ordinary descendant absence.

The deliberately blocked finalizer remains a negative recording case: service
termination passed, while complete artifact finalization required the recorded
explicit reconciliation. Other cases wait for both final recording and cgroup
emptiness within the original fault deadline; transient empty containment before
ExecStopPost starts is insufficient. Empty-controller delegation, `supervisor`
infrastructure, application subgroups and exact `.control` hooks are retained.

Reproduce with an isolated committed installation:

```sh
/path/to/venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

This matrix exercises existing lifecycle fixture hooks. The separate native #25
matrix qualifies the production explicit close operation and the reusable close
hook in a controlled owner. Production shutdown selection, release ordering and
owner pumping remain under #35. These receipts do not qualify broad response
latency; prior lifetime gaps beyond the unchanged 100 ms target remain documented.
Normal local storage and ordinarily killable process assumptions still apply.
