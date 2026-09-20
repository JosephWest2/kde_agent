# Issue 21 installed cleanup qualification

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
