# Private input connection

The worker owns one persistent `input_connection.Input` on its GLib thread. Its
asynchronous EIS negotiation uses the explicitly created private D-Bus connection;
that bus remains retained for the connection lifetime. Host endpoint discovery
and fallback are absent. The startup gate requires CONNECT and one resumed
keyboard, within the existing shared startup deadline and a three-second input
limit. No public key, type, click or reset operation is enabled by this stage.

The limited ctypes declarations in `libei_binding.py` accept any x86_64 libei
1.x (soname `libei.so.1`) that exports every declared symbol. 1.6.0 is the tested
version, and `doctor` warns on others. The M1 compiler audit checked this table
against the installed headers, including void dispatch and variadic promoted enums.
Gio owns original received descriptors. The duplicated descriptor is owned by
setup until it is handed to `ei_setup_backend_fd`. If that call fails, libei
1.6.0 leaves the descriptor caller-owned, but the API does not promise this.
Setup records the descriptor's (device, inode) before the call and closes it
afterwards only if the number still refers to that same file. Nothing else runs
on the single-threaded owner in between, so this never closes a descriptor libei
closed and reused. FD watches borrow libei's FD.
Events, retained seats/devices and the context have explicit reference ownership.
Disposal removes sources first, invalidates pending replies, and is idempotent.

Each dispatch turn drains at most 256 events. A continuation handles remaining
events, and emission stays blocked until the queue is observed empty. Temporary
backlog does not itself fail the session. Pause/removal/disconnection invalidates
state before notifying the action owner. Callbacks may release or dispose the
connection; stale callbacks cannot operate on replacement context state.
Connection epochs are process-unique, including fresh owner instances. Device
identity is monotonically allocated within the owner, independent of native
pointer reuse. Disposal cancels its pending private-bus request token; stale
FD replies cannot attach to a replacement. The numeric primitive validates a complete batch of at
most 32 distinct evdev codes before emission and records attempted presses
before native calls. It is internal: later action scheduling must enforce finite
holds and focus checks. Release uses explicit release events and a frame.

Held state becomes uncertain after lifecycle loss or an emission failure.
RESUMED does not clear uncertainty, and disposal preserves uncertain held
history. An explicit replacement/reset policy must establish when it can clear
that gate; the adapter does not replay input or claim generic application
acknowledgment. The known KWin pause key-ledger behavior remains covered by the
[M1 decision](M1_DECISION.md).

For this intermediate milestone, runtime input capability loss still fails and
stops the worker's owned session. Public recovery will switch atomically with
issue #31's reset implementation. Callback/protocol errors remain sticky and
are checked before ordinary work and worker heartbeats. Readiness moves to the
production input and capture adapters in #64.

The [issue #27 evidence](https://github.com/JosephWest2/kde_agent/blob/d1efe95b/evidence/issue-27/README.md) records installed production
async negotiation and real compositor lifecycle faults. The test harness uses
the existing strictly private pause plugin and its `issue12` name selector;
production uses the `agent-desktop` prefix. Fault control is not a product API.

Canceling pending negotiation guarantees local callback/FD cleanup, not confirmed
server context destruction while the originating bus stays open. Startup failure
and worker stop close that bus. Recovery from unknown negotiation completion on
a reused bus is deliberately left to the public reset policy in issue #31.
