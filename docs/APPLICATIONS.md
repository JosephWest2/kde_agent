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
root exit code and discovered window references. `--wait-window` waits passively
for one or more current associated windows under the original launch deadline;
all current candidates are returned without selecting a target. `application_active` (exit 7) includes the current
handle when application lifetime observation or cleanup remains pending.

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
`cgroup.events` populated=0 observation after settled launch/root reaping,
deferred identity publication, and incremental settlement of every retained pidfd
releases the active slot. A forced observation cannot bypass this gate. A live
retained member outside the application subtree is ownership uncertainty; it
cannot be discarded to declare completion. Missing/malformed/unreadable data
cannot prove emptiness.
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
never extends the request deadline. Launch-only request cleanup allows 0.2 seconds, with the
registry retaining unfinished reaping. After exec acknowledgment, `--wait-window`
stops applying the helper handshake clock and shares the original work deadline.
An owned window query instead uses the shared 1.5-second cleanup reserve.
Timeout/cancellation/query failure retains the app, process identity, logs and
confirmed window references; an authorized application is never killed to complete
request cleanup. Confirmed references can survive a later outer request failure;
pending observations remain diagnostic uncertainty. Normal root/cgroup liveness checks occur
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

Graceful `close` observes this same owned lifetime after requesting normal closure
of one selected window. A closed window or reaped root does not establish complete
application exit. Confirmation/refusal can return timeout with the application
still owned and its other windows discoverable. The result's `process_state` is a
constant-size cached aggregate from existing cgroup observation, not a complete
process list or signal authority. `remaining_processes: null` and
`enumeration: unavailable` are deliberate; even 4096 retained identities do not
turn close reporting into an additional process walk. See [window close semantics](WINDOWS.md#graceful-selected-window-close).

## Explicit termination

`kill --app REF` is a serialized application operation that needs no windows.
It pins that generation and exact application owner and submits signals only
through retained, live pidfds after checking birth identity and exact/subtree
membership. Historical records never grant signal authority. Repeating kill for
a completed application succeeds with `already_exited: true` and zero signals,
even when a different application is active.

The original 5-second default / 15-second maximum includes queue time. At
resolution time `t0`, with remaining time `R` and original deadline `D`, TERM ends
at `t0 + min(1 second, R/3)`, all signals end at `D - min(250 ms, R/5)`, and the
remaining time is observation only. Each retained lifetime gets at most one
TERM and one KILL attempt per explicit request. New descendants join the current
phase, including KILL without a renewed TERM grace. Registry discovery,
retained-handle settlement, verification, dispatch and progress publication
share the existing 2 ms scheduling allowance; at most 16 retained candidates are
visited per turn. The allowance does not preempt filesystem calls or persistence.

Intent and phase entry are persisted before dispatch. A successful signal syscall
means submission, not acknowledgment or exit. Success requires positive settled
whole-application completion in time. `exit_status`/`root_returncode` describe only
the original child; descendant codes remain null. Cancellation, deadline expiry,
disconnect and stop synchronously revoke this request's further signals. Kill
cleanup does not wait or escalate; Registry keeps ownership for later wait or a
new explicit kill. Independently justified service failure/stop cleanup has its
own authority and does not count as application-kill success.

`kill_state` retains fixed cutoffs, phase, attempt/submission counts and at most
64 verified birth-identity observations with their actual timestamps. Samples
are observations, not an exact current process count; `enumeration_incomplete`
stays true until positive completion. `sample_truncated` means the sample reached
its capacity and may omit members. Unavailable samples are null, never an invented
empty list. Detected live foreign identities are reported separately as ownership
uncertainty. Complete success reports `remaining_processes: []`. Request records
retain this typed bounded summary alongside application/process/log references.

Pidfds prevent PID-reuse retargeting. Membership observations do not atomically
prevent hostile same-user migration after the last check; an unobserved process
deliberately moved away before acquisition is outside cooperative containment.
No raw PID, process-group, `cgroup.kill`, or systemd app-kill fallback is used.
Launch, wait and close never construct termination authority. Production shutdown
integration, input/capture and representative third-party application qualification
remain separate issue #35 work.
