# Issue 21 installed cleanup qualification

Selected qualification is in [`review-fix-final/cleanup.json`](review-fix-final/cleanup.json), with
untouched artifact copies and SHA-256 inventory in
[`review-fix-final/selection.json`](review-fix-final/selection.json). All **21 cases passed** against
commit `01edfe40ddd05f19d7e784a058f55449b627cce8`; all 29 installed Python module
hashes exactly match that source. The recorded source worktree contained only
untracked planning notes. Host: systemd `261.3-1-arch`, Linux `7.2.4-arch1-2`.
The matching [unit/process suite report](review-tests.txt) records all 280 tests
passing in 20.372 seconds and the resolved test-fixture setup race.

The longest measured fault-to-empty interval was **7.801 seconds**, for a
permanently blocked release callback reaching watchdog termination. Manager stop
completed in 0.684 seconds, bus/KWin freeze in at most 5.125 seconds, and a worker
frozen with the record lock held in 4.882 seconds. Actual post hooks observed and
killed surviving ordinary descendants in several cases, including Manager stop.
Every tested path stayed below the approved 15-second bound; no fallback cleanup
was needed by the evidence runner.

Both earlier-failure regression cases retained failed terminal state and the
original first failure despite a later requested service timeout: 5.290 seconds
with normal records, and 5.318 seconds when the worker's early manifest update
failed. The uncertain-submission case froze the worker holding its generation
lock; Manager stop still reached cgroup emptiness in 0.367 seconds, reported
`records_preserved: false`, and the autonomous post hook repaired the submission
acknowledgment. These cases address the independent PR review findings.

Blocked ExecStopPost terminated the service and descendants in 3.222 seconds.
It left no autonomous complete receipt; separately recorded explicit
reconciliation then removed settings/sockets and wrote a complete terminal
receipt. This intentionally unavailable-recorder case does not demonstrate
autonomous artifact finalization. All other ordinary fault and stop cases do.
The measurements cover ordinarily killable processes and normal local storage;
they do not establish realtime bounds for uninterruptible kernel or filesystem
waits. Production input-release and application-window adapters remain issue 35.

The original 18-case qualification against `52bf8f5` remains unchanged in
[`final/`](final/cleanup.json) as earlier evidence. It is superseded by the
21-case selection above and did not cover the combined review regressions.

`installed_cleanup.py` runs isolated service generations using a non-editable
wheel and separate controller processes whose working directory is `/`:

```sh
.local/issue21-venv/bin/python -I evidence/issue-21/installed_cleanup.py \
  /absolute/new/output-directory /absolute/dependency-root
```

Append case names to select a focused rerun. The full matrix covers normal raw
stop, Manager stop, startup failure after ordinary descendants exist, failed
ExecStart, bus/KWin/worker kill and freeze, workers frozen while holding artifact
or generation locks, raw stop client disconnect after durable admission,
missing control sockets, frozen starting worker, blocked ExecStop and blocked
ExecStopPost, permanently blocked release callback, and stale finalizer replay
after a replacement generation is ready. The `prior-failure-stop` regression
injects an essential failure, waits for its durable failure receipt and a blocked
release callback, then requests Manager stop. It requires the pre-reconciliation
terminal state and first failure to retain the earlier failure even when the
requested service termination produces a raw timeout result.
The `prior-failure-stop-recordfail` variant additionally makes the worker's
failed-state manifest write raise an error, while preserving its real failure
receipt. The fresh finalizer must retain the earlier failure even though the
pre-stop manifest still says ready with no first failure.
The held-generation-lock case first marks service submission uncertain under the
installed generation lock. Stop must submit its service job without waiting to
write the acknowledgment, and the autonomous post hook must repair that missing
acknowledgment after terminating the frozen owner.

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

Fresh PR review subsequently reproduced an earlier essential failure being
misclassified as stopped when release blocked and Manager stop forced a timeout.
The untouched reproducer and receipts are retained in
[`history/pr54-prior-failure-stop`](history/pr54-prior-failure-stop/history.json).
The installed matrix now includes that exact ordering as `prior-failure-stop`;
the original 18-case run did not exercise this combined fault sequence.
