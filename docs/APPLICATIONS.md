# Application launch and ownership

`launch -- ARGV...` executes exactly one argument vector without adding a shell.
The caller normalizes cwd before transport. Slash-relative executable paths are
relative to that cwd; bare names use the final application PATH (including empty
segments). Explicit allowed environment overrides reach only the target exec.
The trusted isolated Python helper starts with clean defaults, so loader/Python
overrides cannot execute code before containment. Protected private endpoints
and settings cannot be overridden.

The result supplies a generation-qualified application handle, original process
birth identity, selected executable, stdout/stderr paths, lifetime state, observed
root exit code and an initially empty window list. `--wait-window` is rejected
before launch until issue #24. `application_active` (exit 7) includes the current
handle when the application or any ordinary descendant is still living.

## Kernel ownership and lifecycle

The session service delegates an empty controller list (`Delegate=`) and uses
`DelegateSubgroup=supervisor`. Worker, bus, compositor and adapter helpers live
under `supervisor`; systemd's stop hooks live under the exact `.control` subgroup.
Each application has a random `applications/<application-id>` cgroup below the
same service. No toolkit code writes `cgroup.subtree_control`. Host-inherited
controllers may exist; the toolkit requests none.

The helper enters its application group before replying ready. The worker retains
its pidfd, verifies birth identity and exact membership, and persists identity and
logs before recording an execution-authorized effect and releasing the gate.
All inherited group/config/gate descriptors close before exec. Configuration,
including environment, travels in a sealed anonymous memfd, never an artifact.
The exec-status pipe is close-on-exec. Missing exec-attempt receipt, signal death
at the handshake, protocol failure or timeout preserves a conservative outcome;
no launch is automatically retried. An execution-authorized record is deliberately
conservative and can exist when cancellation ultimately prevented execution.

Fork, double-fork, setsid, reparenting and original-process exit preserve inherited
cgroup membership. This is an ownership boundary for ordinary cooperative
applications, not a security sandbox against an application deliberately moving
itself out of the delegated service. Disk process records are diagnostics only;
they are never reopened as signaling authority. Live identities use pidfds and
rechecked `/proc` birth/membership observations. Popen remains the sole reaper of
the original child. Descendant exit codes are unknown unless directly observed.

Root exit and complete application exit differ. Only a positive recursive
`cgroup.events` populated=0 observation after settled launch/root reaping releases
the active slot. Missing/malformed/unreadable data cannot prove emptiness.
Cancellation, client disconnection and subsequent processing failure close request
protocol endpoints and retain application ownership and durable references. A
preauthorization helper may be aborted; an authorized application is not killed
by request cleanup. The registry observes lifetime after the request terminates.
Logs become complete only after the entire application set exits. Generation
shutdown remains responsible for terminating the complete service tree.

Stop live generations before upgrading the installed package. New hooks require
the exact delegated layout and intentionally reject old-layout generations. Live
code replacement needs a future versioned layout protocol; it is not supported.

## Supported work bounds

The helper gate deadline is the earlier of the request deadline and two seconds
after helper setup; exec acknowledgment is capped at one second after release and
never extends the request deadline. Request cleanup allows 0.2 seconds, with the
registry retaining unfinished reaping. Normal root/cgroup liveness checks occur
every 50 ms or synchronously before another launch.

Member scanning streams at most 4 KiB of input and 16 new member identities per
5 ms worker turn, yielding after directory/read boundaries, and stops scheduling
new observations after 2 ms. Changed snapshots coalesce at 50 ms except required
pre-release persistence. Limits are 4096 observed identities per application,
256 nested groups, depth eight and 64 KiB membership input per group per scan.
Overflow or incomplete observation fails the session closed while systemd retains
service-wide termination. No incomplete scan is called empty. The finalizer uses
the same finite traversal limits and its existing one-second survivor deadline.
The existing normal-local-storage assumption applies: an individual filesystem
operation is not advertised as preemptible or realtime bounded.

The native fixture preserves its stdin-driven defaults and adds `--autonomous`,
`--window-delay-ms N`, `--exit-after-ms N`, `--exit-code N`, and
`--descendant-ms N`. The last starts a double-forked setsid descendant with JSON
PID/start/exit receipts. These opt-in controls support automated qualification;
they do not widen the supported application compatibility claim.
