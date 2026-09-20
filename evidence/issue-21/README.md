# Issue 21 installed cleanup qualification

Selected qualification is in [`final/cleanup.json`](final/cleanup.json), with
untouched artifact copies and SHA-256 inventory in
[`final/selection.json`](final/selection.json). All **18 cases passed** against
commit `52bf8f543600baa29e224bb7484d3782e16507e9`; all 29 installed Python module
hashes exactly match that source. The recorded source worktree contained only
untracked planning notes. Host: systemd `261.3-1-arch`, Linux `7.2.4-arch1-2`.

The longest measured fault-to-empty interval was **7.718 seconds**, for a
permanently blocked release callback reaching watchdog termination. Manager stop
completed in 0.334 seconds, bus/KWin freeze in at most 5.253 seconds, and a worker
frozen with the record lock held in 4.868 seconds. Actual post hooks observed and
killed surviving ordinary descendants in several cases, including Manager stop.
Every tested path stayed below the approved 15-second bound; no fallback cleanup
was needed by the evidence runner.

Blocked ExecStopPost terminated the service and descendants in 3.195 seconds.
It left no autonomous complete receipt; separately recorded explicit
reconciliation then removed settings/sockets and wrote a complete terminal
receipt. This intentionally unavailable-recorder case does not demonstrate
autonomous artifact finalization. All other ordinary fault and stop cases do.
The measurements cover ordinarily killable processes and normal local storage;
they do not establish realtime bounds for uninterruptible kernel or filesystem
waits. Production input-release and application-window adapters remain issue 35.

`installed_cleanup.py` runs isolated service generations using a non-editable
wheel and separate controller processes whose working directory is `/`:

```sh
.local/issue21-venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

Append case names to select a focused rerun. The full matrix covers normal raw
stop, Manager stop, startup failure after ordinary descendants exist, failed
ExecStart, bus/KWin/worker kill and freeze, a worker frozen while holding the
artifact record lock, raw stop client disconnect after durable admission,
missing control sockets, frozen starting worker, blocked ExecStop and blocked
ExecStopPost, permanently blocked release callback, and stale finalizer replay
after a replacement generation is ready.

The fixture imports installed product modules without altering `sys.path`.
It supplies provisional native readiness and Python-only shutdown observers.
An ordinary child launches a grandchild that calls `setsid()` and ignores
SIGTERM. Neither process belongs to the worker's tracked child registry, so the
matrix exercises systemd cgroup cleanup beyond direct-child/process-group
termination. This is lifecycle infrastructure evidence; production tracked-input
release and application-window adapters remain issue 35.

Fault signals use pidfds with exact generation cgroup and process start-tick
checks. The runner observes external cgroup emptiness, descendant lifetime
absence, terminal receipt, manifest cleanup and settings/socket removal before
any toolkit reconciliation. For Manager startup failure and Manager stop, a
test subclass snapshots the real `_retire` entry before delegating to the
unmodified retirement method. It introduces no extra wait or cleanup action.
Native success cases retain and validate their readiness PNGs. Hook events prove
active action cancellation, cleanup, release and close attempts precede the
ordinary application's SIGTERM receipt.

The blocked-post negative case intentionally prevents the independent recorder
from running. It must still terminate the service and ordinary processes within
15 seconds, must not claim autonomous complete cleanup, and must reach complete
cleanup after a separately recorded explicit reconciliation. A blocked-stop
helper must still reach the functioning post hook and complete autonomously.

`cleanup.json` records exact source and installed module hashes, the source
commit/worktree state, effective service properties, sampled state transitions,
timings, process identities and durable artifact inventory. Development runs
with a dirty source tree are exploratory; final selected evidence must identify
the committed implementation it qualifies. Temporary runtime directories are
removed after the run; permanent artifact copies retain the observations.

The failed run at `7b096e6` is retained in `history/7b096e6`, including its
original cleanup receipt and blocked-post artifacts. It exposed a receipt
destination bug: the first outside reconciliation wrote its initial uncertain
state to `terminal.json`, then wrote completion to `reconciliation.json`. The
fix at `52bf8f5` chooses the receipt destination once per finalizer invocation.
This historical run is not selected qualification evidence.
