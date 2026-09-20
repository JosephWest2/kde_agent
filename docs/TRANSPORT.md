# Generation-bound worker transport

The packaged foreground worker establishes private routing and protocol validation.
It does **not** establish a desktop, a ready session, production health, successful
input, capture or lifecycle cleanup. `session start` and `doctor` remain locally
unsupported. Established-session commands can contact this worker and receive
`unsupported_operation` with verified session identity. If there is no routing
record they return `session_not_found`; an unreachable recorded worker returns
`session_unavailable`. Real lifecycle/adapters follow in M3 onward.

## Worker and ownership

The internal supervisor entrypoint is:

```sh
python -m agent_desktop.worker --session NAME --generation GENERATION
```

Run with the distribution Python/PyGObject selected in the M1 decision, or an
installation that exposes those system bindings. CLI parsing, help, version and
the client do not import gi. The entrypoint stays in the foreground and never
launches an adapter or service. SIGTERM/SIGINT stops this transport process and
removes its owned endpoint; it does not pretend to implement session stop.
Worker stdout/stderr are separate from the socket; only framed replies cross it.
Durable worker logs and artifact records are #17 work.

The root is absolute `XDG_RUNTIME_DIR`, or `/run/user/<uid>` only when that variable
is absent. Missing roots are not created and there is no `/tmp` fallback. Roots
must be owned by the caller, have no group/other permissions and have no symlink
ancestors. Toolkit directories must also be real owner-only directories:

```text
$XDG_RUNTIME_DIR/agent-desktop/current/NAME.json
$XDG_RUNTIME_DIR/agent-desktop/current/NAME.lock
$XDG_RUNTIME_DIR/agent-desktop/g/GENERATION/control.sock
```

Directories are 0700; sockets, routing and lock files are 0600. Both socket peers
check Linux SO_PEERCRED for the current UID. This is access control between users,
not a sandbox against another process running as the same trusted user. Unsafe
existing paths are rejected, not chmodded or deleted. Overlong Unix socket paths
fail rather than being truncated. Bind does not unlink an existing endpoint.

A generation directory is claimed by exclusive creation and retained after worker
exit. A second worker cannot reuse the token, under the same or another name,
even after graceful shutdown. The supervisor must create a fresh generation for
each lifetime. Runtime cleanup removes only the recorded socket inode and removes
the name pointer only if it still names that generation. A bounded nonblocking
per-name lock protects publication and conditional cleanup; lock files remain.
The pointer is atomically published only after listen succeeds. It is routing
metadata, not a durable manifest or proof of desktop health. Crash residue and
conflicting pointers are rejected; M3 supplies authoritative service/lifecycle
reconciliation, including claim retirement.

The client resolves a name once, checks any expected or handle-derived generation,
and pins the immutable endpoint. Name-only lookup means the current generation at
that resolution; it does not protect against restart. No reconnect/re-resolution
occurs after failure. Before a correlated worker reply, the public actual
generation remains null; candidate identities are labeled in error context.

## Framing and schemas

One connection carries one request and one reply. Each frame is a four-byte
unsigned big-endian length followed by that many UTF-8 JSON bytes. Both payloads
are limited to 1 MiB and 32 nested containers. Zero/oversized lengths, invalid UTF-8,
duplicate keys, trailing bytes, non-object roots, nonfinite numbers (including
exponent overflow), incompatible versions and malformed field types are rejected.
Before dispatch, one nonblocking one-byte peek rejects already queued trailing
data, including at receive-chunk boundaries. There is no pipelining; additional
bytes never dispatch another operation. A peer
can send bytes after an already valid request has executed; their later arrival
does not undo that execution. Normal clients keep both socket directions open:
EOF/HUP means client departure, not an end-of-frame marker.

Requests have exactly these fields:

```json
{"schema_version":1,"request_id":"11111111111111111111111111111111","operation":"session.status","session":"default","expected_generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","arguments":{},"timeout_seconds":3.0,"caller_cwd":"/absolute/caller"}
```

For transmission, `expected_generation` is always the concrete resolved generation,
including name-only calls. The optional original caller expectation stays local.
Timeouts are JSON numbers, handles decoded objects, coordinates integers and
explicit launch environments objects. No argparse reconstruction or coercion of
CLI-like wire strings occurs. Shared operation validators then check arguments,
finite operation limits, handle formats and conflicting generations. `doctor` and
`session.start` are local-only and rejected on the socket. Every admitted worker
request must match its immutable name/generation immediately before dispatch.
Duplicate active IDs are rejected and cannot unregister the original request.
IDs are correlation identities, not a durable replay cache or idempotency promise.

Replies use the existing [CLI envelope](CLI.md#requests-results-and-errors).
The client validates schema, request ID, operation, actual name/generation,
success/error consistency and error shape before accepting one. Malformed or
uncorrelated replies cannot become success. A valid partial error preserves
available handles and paths. Unknown schema/type errors use `protocol_error`;
well-shaped semantic argument errors retain `invalid_arguments` or
`generation_mismatch`. Public diagnostics never echo raw offending arguments,
environments, JSON bytes or arbitrary exception messages.

## Bounds, lost responses and the queue handoff

These transport allowances are provisional bounds tested with local process
doubles, separately from the operation work budget:

| Phase | Bound |
| --- | --- |
| Client connect | 1s |
| Entire request send / server frame receive | 1s each |
| Entire server response write | 1s |
| Simultaneous accepted connections / listen backlog | 32 each |
| Read/write work per GLib callback | one chunk, at most 64 KiB |
| Accept work per GLib callback | at most 8 peers |
| Deadline observation timer | 10ms |

Each phase uses an absolute monotonic deadline; trickled bytes do not reset it.
Client response waiting is bounded by work timeout + 16s cleanup reserve + 1s
response allowance after sending. The reserve accommodates possible capture abort
(1s) and owned-service cleanup (15s); it never enlarges the success/work budget.
#16 refines per-operation cleanup and supplies worker-enforced task cancellation.
Synchronous small routing-file operations assume responsive local runtime storage;
these are not hard real-time guarantees under a hung filesystem.

Pre-send failures report `not_started`. Once bytes may have reached the worker, a
lost/truncated/invalid response reports `completion_unknown`, outcome `unknown`.
SIGINT closes the socket and emits one `cancelled` envelope, with unknown effects
if sending had begun. Launch and input are never retried. A complete valid worker
error may establish a more precise not-started or partial outcome.

The server fully validates/encodes a reply before writing any header. Oversized or
unencodable results after dispatch produce a small correlated `completion_unknown`
error with unknown effects, retaining safely representable application/window
handles when available. They never become `not_started`, and raw oversized data
or encoding exceptions are not echoed. Failure to deliver that fallback is a lost
response, also unknown to the client.

The GLib owner performs nonblocking socket I/O. Internal handlers receive an
Admission with monotonic admitted_at/deadline, complete(result/error), disconnected
state and an on_disconnect callback. They must return promptly; this is not a
permission for blocking adapter calls. Completion cannot accept late success; a late structured result is retained as
the timeout's partial result so available handles and recovery paths survive.
Oversized or unencodable late results use the same bounded safe-handle fallback.
Disconnect leaves the active ID owned until terminal completion and notifies the
corresponding unfinished admission once. #16 adds the serialized ordinary queue,
priority control admission, cancellable task stepping and cleanup continuation.
The connection cap alone is not a guarantee of responsive cancellation under
saturation. No 100ms production cancellation claim is made by this issue.

## Verification boundary

`python -m unittest discover -s tests -p 'test_*.py'` includes real fresh GLib worker
and separate CLI processes, with a private temporary XDG runtime. It exercises
malformed frames/requests, generation mismatch/replacement, private ownership,
exclusive generation reuse, slow peers, duplicate IDs, disconnect/SIGINT and
post-effect lost/oversized/unencodable responses. The fixture's successful status
explicitly reports `state: transport_test` and `desktop_ready: false`.

The fixture handler is injected through internal Python code in a tests-only
launcher. There is no packaged fake-handler flag, environment switch, import-path
option or protocol activation command. Installed-wheel smoke checks separately
verify the packaged worker's explicit unsupported response and verified identity.
Tests do not touch the personal desktop, create a private KWin or claim adapter
support. Missing gi causes integration failure, not silently skipped coverage.
