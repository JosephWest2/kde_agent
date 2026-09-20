# Structured discovery: installed cleanup regression

All **21 scenarios passed** in a serialized run against revision `8e11df00b81f27f55cd7226e06e687d01b621b79`.
Product modules/scripts are unchanged from reviewed `7e4f8d6`; this revision also
includes the runner finalizer-observation correction. All 33 installed Python module hashes and 1 packaged JavaScript resource hash match source exactly.
The [complete receipt](cleanup-review/cleanup.json) and [unaltered artifact inventory](cleanup-review/selection.json)
retain the observations. No fallback cleanup was needed.

The longest fault-to-empty/observation interval was **7.707 seconds**
(`blocked-release`), below the existing 15-second shutdown bound. Coverage includes
normal/manager stop, failed startup/exec, bus/compositor/worker death and freezes,
held locks, disconnect, missing sockets, blocked hooks/release, earlier-failure
preservation, and stale-generation replay. Every case independently observed the
whole service cgroup empty and ordinary descendants absent.

Effective delegation retains empty controllers, `supervisor` infrastructure,
application subgroups and exact `.control` lifecycle hooks. The deliberately
blocked finalizer remains a negative recorder case: service termination passed,
while complete artifact finalization required the recorded explicit reconciliation.

The stale-generation check compares toolkit control/endpoint files, service
properties, and exact worker/descendant identities. KWin home preferences/cache
hashes remain in both snapshots as diagnostics; those files can change naturally
after readiness. An earlier run exposed two such newly created shortcuts files,
so the runner no longer incorrectly treats compositor home as immutable. A focused
stale-replay run passed before this complete rerun.

The runner rejects stale installed Python or packaged JavaScript. Builds were
installed non-editably from a clean archive of the committed source, avoiding
old deleted files in an existing build directory. Reproduce with the committed
build installed in an isolated venv:

```sh
/path/to/venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

This matrix uses existing lifecycle fixture hooks; the separate #23 native
matrix qualifies production discovery and query cleanup. Readiness input/capture
remain provisional under #35. These bounds retain the established normal local
storage and ordinarily killable process assumptions. Raw observations and paths
were copied without alteration.

The initial [e7d8827 matrix](cleanup/cleanup.json) is retained as historical evidence.
The selected review-corrected run above supersedes it. Two later negative
attempts are preserved under [cleanup-negative/](cleanup-negative/): a startup
query correctly timed out and reclaimed its session while other native suites
were running; concurrent load is a possible cause, not established attribution.
The next serialized run passed twenty cases but the replacement-stop harness
observed transient cgroup emptiness before ExecStopPost wrote terminal.json.
That negative receipt records fallback cleanup. The corrected harness waits for
both independent emptiness and complete terminal recording within the same
15-second deadline latched before stop dispatch, and records replacement stop
elapsed time. A focused stale replay and the final full matrix both passed.
No application code or deadline was weakened for either observation.
