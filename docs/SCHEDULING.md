# Worker scheduling and cancellation

The scheduler supplies infrastructure for the later desktop adapters. The packaged
worker still returns `unsupported_operation` for every desktop command, including
reset/stop; unsupported controls do not cancel another request. Tests inject tasks
through an internal Python launcher only. Nothing here certifies desktop readiness,
input delivery, compositor cleanup or actual service shutdown.

## One owner, finite work

A single GLib owner admits up to 32 ordinary requests, including active work. All
session operations except `input.reset` and `session.stop` use that queue. Admission
captures one monotonic deadline after complete wire validation; queue delay counts.
A queued request expiring before execution has `timeout`, `outcome: not_started`,
and its task factory is never invoked. Factories construct tasks without effects;
their first step revalidates targets and starts work. Each step must return promptly.
The worker samples fresh time before constructing each task, again immediately
before every step (including after construction and other callbacks), and after
accepting its result. An observer consuming another request's remaining budget
cannot cause that request to emit using an earlier tick timestamp.
Success at or after the original deadline is rejected, including late structured
results containing application handles.

Tasks expose `step(now)`, `request_cancel(reason)` and `cleanup(now)`. A step returns
`None` while pending, or a result dictionary only when its completion/release work
is done. Waits use nonblocking observations or state checked on later steps. No
nested event loops, blocking process waits, sleeps, desktop calls or unbounded file
operations belong inside steps. Cleanup retains the ordinary execution slot until
confirmed or exhausted. Unconfirmed cleanup leaves the session/input unavailable
and calls the escalation seam; another queued action cannot take over that owner.

The 5ms GLib service turn bounds accepts to eight per endpoint and ready I/O to
eight connections per class, one 64KiB chunk each, rotating their order. Priority
traffic is serviced first, then ordinary I/O, expiry, child reaping and task steps.
A continuously ready control source therefore cannot suppress a separate timer:
all service classes run within the same bounded turn. There are at most 32 ordinary
and eight priority connections. Malformed traffic receives the same service quota.
These limits assume normally responsive local storage and bounded task callbacks,
not a hard real-time host or protection from unlimited hostile same-UID traffic.

## Correlated controls and caller departure

Each generation has a separate owner-private `priority.sock`, published alongside
`control.sock`. Reset and stop use priority; ordinary requests use control. Both
endpoints enforce name/generation and peer UID. Wrong endpoint or malformed controls
produce `protocol_error`; stale generation cannot affect another request.

SIGINT sends this internal control message to the already pinned priority endpoint,
with a bounded 100ms connect/send attempt, then closes the original socket. It does
not wait for control acknowledgment or re-resolve a name. A second SIGINT still
closes the original socket. There is no public cancel command or retry behavior:

```json
{"schema_version":1,"request_id":"11111111111111111111111111111111","operation":"request.cancel","session":"default","expected_generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","target_request_id":"22222222222222222222222222222222"}
```

The reply uses the usual envelope with `result.cancel_requested`. True means the
owning request accepted a signal, not that cleanup or input delivery is confirmed.
Unknown/completed targets return false without creating work. Request IDs are
correlation, not secret credentials. The worker accepts a complete priority frame
even if its sender immediately closes; incomplete controls never execute.

EOF/socket loss and worker timeout cancel only the matching unfinished ordinary
request. Queued cancellation prevents effects. Disconnect retains the request-ID
claim until terminal cleanup, and stale callbacks cannot cancel its successor.
CLI interruption returns one `cancelled` result and exit 130; after possible send,
its outcome is unknown. Cancellation cannot undo a click, launch or UI change.
Tasks record effects/available handles immediately; those partial results survive
cancellation, timeout and lost callers. No launch or input is replayed automatically.

The provisional target is cancellation dispatch and release **attempt** within
100ms of admitted valid control/detected EOF. End-to-end CLI delivery is measured
separately. M1's 500ms fixture-observed release target is distinct; neither target
proves arbitrary application acknowledgment. M5/M7 own renewed real-adapter timing
and failure validation. The process tests here use task doubles, not real input.

## Reset, stop and cleanup budgets

Supported task implementations gate input immediately on reset, signal the active
owner, wait for its cleanup, then reset under one admission-based budget <=3s.
Failure leaves input unavailable. Stop gates new work, cancels queued/active work
and owns its original <=15s deadline. Accepted lifecycle work survives caller loss;
repeat callers join with separate wait deadlines and cannot extend or shorten the
owner's deadline. Stop supersedes reset without running concurrent owner mutations. Superseded reset
cleanup is clamped to the first stop owner's remaining budget. Pending stop expiry
is enforced even while reset retains the lifecycle slot: the worker reports
timeout/uncertainty and fails closed without starting concurrent or late stop work.
A waiter timing out or disconnecting cannot reopen input or cancel safety cleanup.

Tasks declare a bounded cleanup reserve, at most 16s, covering their entire cleanup
sequence; resets/stops cannot extend their original admission deadline. Adapter
budgets remain query cleanup <=1.5s, local capture abort <=1s and owned shutdown
<=15s. If stop arrives during ordinary abort, the remaining abort is clamped to the
stop owner's original deadline. Repeated stops do not grant a second shutdown phase.
Future automatic capture-failure escalation must reuse an existing shutdown owner;
without one, its one <=15s phase follows the <=1s local abort. This infrastructure
provides the escalation callback, and a task-double regression demonstrates an
automatic abort callback creating or joining exactly one shutdown owner. #33 owns
real capture-abort routing and compositor uncertainty; #21 owns systemd/cgroup
shutdown integration and renewed shared-budget verification. No successful cleanup is fabricated here.

`Children` retains each directly spawned child independently of request lifetime.
`Popen.poll` is its only reaper; output uses private files or DEVNULL, never undrained
PIPEs or protocol stdout. Cancellation kills only the owned direct child and polling
continues after request timeout/terminalization until exit. It never blocks on an
uninterruptible child. Worker-process shutdown transfers any remaining reaping to
the system; killing a PID does not prove descendant or compositor cleanup. Later
owned-service boundaries provide those stronger guarantees.

## Record observation contract

The generic observer receives copied identity, phase, error code, effect outcome
and partial-result fields, never the original argv/environment/text. Observers are
non-reentrant, exception-contained and must be bounded. Ordinary admission record
failure prevents new effects. Safety cancellation/release happens before observation;
record failure does not skip required lifecycle cleanup or release the owner early.
Effects supplied by adapters must be a bounded safe projection, not arbitrary data.

`finalizing` means an outcome is being considered; it never commits durable success.
After that callback the scheduler and transport recheck deadline acceptance. The
transport validates/encodes the complete envelope and applies bounded serialization
fallback before setting `Admission.final_payload` and calling `on_terminal(payload)`.
That notification also happens for disconnected callers and contains the exact
accepted result, including timeout or `completion_unknown` conversion. The terminal
notification cannot veto or rewrite acceptance; its exception is contained. #17
must keep a pending/uncertain record if final publication fails, rather than write
a contradictory success before acceptance. Its durable implementation may use
bounded queues or measured small writes under the normal-storage assumption; no
hung-filesystem immunity or durable exactly-once execution is promised.

## Verification

Run `PYTHONWARNINGS=ignore python -m unittest discover -s tests -v`.
Deterministic tests cover deadline equality, serialization, queued expiry, factory
errors, observer failure/reentrancy, lifecycle join/supersession, release ordering,
retained handles and late reaping. Actual GLib process tests exercise SIGINT, socket
loss, timeout without client enforcement, 32 occupied ordinary slots, sustained
valid/malformed controls, stalled child output, and reset/stop after killing callers.
The older transport framing, late-result and installed CLI contracts remain covered.
Missing gi fails process tests instead of silently skipping evidence.
