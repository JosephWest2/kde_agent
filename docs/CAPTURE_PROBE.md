# M1 ScreenShot2 and responsive integration probe (#13)

`tools/capture_probe.py` exercises fresh whole-output screenshots on the existing
private KWin fixture. A separate process owns each capture's D-Bus connection,
raw pipe, Pillow decoding and PNG writing. The existing #12 GLib owner continues
to service libei, focus queries and the independent cancellation channel.
The user approved this process boundary and stopping the owned private session
when aborted server work cannot be proven reclaimed.

This is feasibility tooling. It does not implement a production session CLI,
application support, generic application acknowledgment, or the twenty-run
production acceptance requirement. The consolidated decision is in
[M1_DECISION.md](M1_DECISION.md).

## Reproduce

Use the [dependency setup](SETUP.md) and its unchanged pinned kdotool binary.
Substitute the absolute checkout directory below. Each invocation creates a fresh
generation under the existing service and lifetime limits.

```sh
/usr/bin/python -m unittest discover -s tests -v
/usr/bin/python -I /checkout/tools/private_harness.py run --screenshot -- \
  /usr/bin/python -I /checkout/tools/capture_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool functional
```

Replace `functional` with a scenario from the table below. Repeat successful
functional runs in three fresh generations for the repeated measurement matrix.
The screenshot capability is opt-in: the launcher constructs
`KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1` only in private KWin's environment.
It is not an inherited probe override or a host desktop setting. No fault plugin
is needed for capture scenarios. Reusing the separate #12 pause test still needs
its explicitly scoped [test plugin](../tools/eis_fault_plugin/README.md).

| Scenario | Observation |
| --- | --- |
| `functional` | Ten captures: distinct presented states, all fixture pixels, an acknowledged rendered input sequence, repeated capture and aggregate adapter timing. |
| `invalid-screen` | Exact D-Bus InvalidScreen rejection before a writer is accepted, followed by a successful fresh request. |
| `partial` | Close the reader after a positive partial byte count; no PNG success; fail the private session. |
| `stall-reply` | Stop the child after queueing the call, before it services the reply; enforce parent timeout and local cleanup. |
| `stall-pipe` | Stop the child with its reader retained after positive partial data; document payload versus actual pipe capacity. |
| `stall-encode` | Stop the child after complete raw capture and before PNG encoding; demonstrate the same kill boundary. |
| `responsive-pipe` | Hold acknowledged Shift+W, stall capture draining, then dispatch independent cancellation and observe actual fixture releases plus control status while capture remains unfinished. |
| `responsive-encode` | The same cancellation/control observations while the capture child is stopped before encoding. |
| `slow-query` | Reuse #12's real finite 750ms KWin script, its 500ms work deadline and separate cleanup, then perform capture. |

Injected capture-abort scenarios deliberately return a **failed** capture/probe,
including when cancellation and cleanup checks pass. Preserve that result and
verify the outside controller's empty/removed cgroup and removed runtime. Do not
turn the intended failure into a successful capture or reusable-session claim.
InvalidScreen is a narrowly identified rejection before capture work begins;
other uncertain failures require session stop.

## Raw pixels and publication

The private interface is `org.kde.KWin.ScreenShot2`, object path
`/org/kde/KWin/ScreenShot2`, version 5 on the tested release. `CaptureScreen`
receives the independently observed sole output name, an FD and explicit options:
`include-cursor=false`, `native-resolution=true`, `hide-caller-windows=false`.
The codec accepts only this metadata contract:

| Field | Required value |
| --- | --- |
| `type` | `raw` |
| `width`, `height` | 1280, 720 |
| `stride` | 5120 bytes, with no padding |
| `format` | Qt `Format_ARGB32_Premultiplied`, numeric 6 |
| `screen` | The independently observed requested output name |
| `scale` | 1 |

Typed D-Bus values are normalized without converting booleans to integers.
Invalid/missing types, dimensions, formats, scale or stride fail. Draining begins
before the asynchronous request and is capped at 8 MiB even before metadata
arrives. Completion requires valid metadata, exactly **3,686,400 bytes and EOF**.
The expected byte count or metadata reply alone cannot establish completion.

Qt words are premultiplied BGRA bytes on the tested little-endian machine. The
codec uses Pillow's `RGBa` mode with raw `BGRa` (or `aRGB` for big endian), converts
to straight RGBA, and explicitly normalizes zero alpha to transparent black.
KWin already exports top-down rows; the client does not flip again. Synthetic
tests cover both byte orders, channels, row orientation, semitransparent pixels
and zero alpha. Real fixture regions are opaque; their passing checks alone
would not establish alpha decoding.

For each freshness proof, the parent correlates the exact controlled fixture
state with its presentation receipt before submitting the capture. Current
`clientGeometry` supplies the sample origin. Four asymmetric quadrant colors
prove channels/orientation; all 32 black/white marker cells prove the state.
The captured PNG and per-sample expected/observed RGBA values accompany the
receipt. Input dispatch, actual fixture input receipt, presentation, request and
capture completion are distinct events. Request/completion monotonic timestamps
are never labeled as compositor exposure times. The virtual backend's missing
`sync_output` feedback is retained alongside the independently checked sole
output identity, as in #10.

The child creates an owner-private `image.partial`, encodes and closes it,
reopens and fully loads the PNG, checks its format/dimensions, then renames it to
the request's unique `image.png`. The parent accepts success only after observing
the correct generation/request/path, child completion and the published file
before its own deadline. A child reporting success too late fails; rejected
partial/final images are removed. The reported hash identifies the actual PNG.

## Ownership, deadlines and failure policy

The child owns the raw reader and its private bus; the input owner inherits no
raw-pipe endpoint. `dbus.UnixFd` duplicates the original writer, and message
construction takes another duplicate. After queueing the call, the child takes
and closes the wrapper's FD exactly once and closes the original writer. Neither
may keep EOF open. Per-pipe FD inventories check for retained local writers.
Successful exact data plus EOF establishes that writer copies have closed.

| Phase | Bound and meaning |
| --- | --- |
| Capture | 3s from admission through parent acceptance of the fully validated, published PNG, including child startup and exit observation. |
| Local abort cleanup | Separate 1s for kill/reap, local FD closure and artifact removal. |
| Failed private session | Existing 15s complete observed service shutdown when server cleanup is unconfirmed. |
| Worst full-timeout failure path | Up to 19s across capture, local abort and service stop, clamped to enclosing harness budgets. |
| Cancellation release dispatch | Existing independent 100ms acceptance bound. |
| Actual fixture release | Existing 500ms acceptance bound under recorded conditions. |
| GLib heartbeat | 5ms instrumentation; observed maximum scheduling gap target 100ms. |

Child termination is not server cancellation. KWin renders synchronously and
uses a thread-pool writer whose 60s per-poll wait is not this client's deadline.
A broken-pipe diagnostic precedes runnable destruction, and a subsequent
successful screenshot cannot prove an earlier server FD/image/task was reclaimed.
Read-only `/proc` server-FD inspection returned PermissionError on this target;
no permission bypass was used. On
unconfirmed cleanup, mark the session unavailable, reject more captures and
stop the owned service. Only its observed empty cgroup establishes full recovery.
Input cancellation retains its independent dispatch bound throughout.

## Acceptance deadline correction from fresh review

The [separate correction evidence](../evidence/issue-13/acceptance-deadline-fix/README.md)
fixes a stale acceptance timestamp taken before process/publication checks. The
supervisor now samples the clock after those checks and rejects expiry immediately
at acceptance. Two reproducing regressions and all **60 tests** pass. Three
focused corrected-source runs produce eleven complete PNGs, with maximum actual
acceptance 121.222ms, local cleanup 5.282ms, GLib gap 5.641ms and observed service
stop 161ms. Slow-capture cancellation dispatch is 0.579ms and fixture release
2.451ms. Timeout polling detection is 3.004051s; the strict success deadline stays
3s. The aggregate ten-capture/input sequence is 1.770s. The following original
matrix/tables remain historical pre-correction measurements with their original
source hashes; they were not silently replaced or attributed to the corrected code.

## Recorded evidence and measurements

The [#13 evidence](../evidence/issue-13/README.md),
[selected-run index](../evidence/issue-13/runs.json) and
[complete measurement summary](../evidence/issue-13/summary.json) record eleven
selected private generations, all with expected outcomes and observed cleanup.
Three functional generations produced thirty pixel-checked PNGs with 36 checks
each: **1,080 exact pixel assertions**. InvalidScreen recovery and the slow-query
scenario add two complete PNGs, for **32 accepted captures**. Six aborted-capture
runs preserve failed outcomes and stop their private sessions; the separate
known InvalidScreen rejection permits its successful follow-up capture.

| Measurement | Samples | Observed maximum |
| --- | ---: | ---: |
| Admission through accepted published PNG | 32 | 126.157575ms |
| Local abort cleanup, including safe rejection | 7 | 5.343563ms |
| GLib scheduling gap | 11 runs | 6.294332ms |
| External cancellation to explicit release dispatch | 2 | 0.859731ms |
| External cancellation to actual fixture release | 2 | 2.609346ms |
| Independent control status during stalled capture | 2 | 50.395019ms |
| Complete service shutdown | 11 | 241ms |
| Failed capture admission through observed empty cgroup | 6 | 3.318368s |
| Aggregate foundation plus adapter sequence | 3 | 1.764696s |

Timeout detection reached 3.003762s on the 5ms supervisor timer. The success
deadline remains strict: no result is accepted at or after 3s. The detection
measurement describes polling latency, not permission for late success. The
aggregate measurement conservatively includes all ten functional captures and
the input sequence; it leaves the harness's `foundation_ready` phase unchanged.
These are observations on the recorded environment, not hard real-time promises.

Each durable run records `capture-probe.json`, per-request `receipt.json`, PNGs,
child diagnostics, `screenshot-introspection.xml`, fixture events, libei/query
evidence, manifests and outside-controller cleanup. The request rows include
pipe capacity/inode, metadata, data/EOF stages, hashes, validation/publication,
acceptance and cleanup status. Retain failures and startup/outlier samples.
Local parser/codec/FD tests establish client defenses; they do not imply KWin
emitted malformed metadata. Aggregate adapter timing is a feasibility measurement
after `foundation_ready`; it does not implement the production REQ-009 readiness
gate or rename the harness's narrower foundation phase.
