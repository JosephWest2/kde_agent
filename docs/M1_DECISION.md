# M1 platform adapter decision record

The proposed integration retains pinned kdotool for windows/focus, one Python
ctypes/GLib owner for libei input and cancellation, and an individual process
for each ScreenShot2 capture. The user approved the capture process boundary
and failing/stopping the owned private session when aborted server cleanup is
unconfirmed. These decisions preserve the existing private headless desktop,
generation ownership and service cleanup boundary.

**GO for M1 feasibility and dependent production design**, with mandatory
session stop after unconfirmed capture cleanup and continued gating of uncertain
input. The [#13 evidence](../evidence/issue-13/README.md) completes the adapter
matrix: eleven selected generations, 32 complete PNGs, 1,080 exact pixel checks
across thirty captures, responsive cancellation/control and observed cleanup.
Six deliberately aborted captures remain failed operations followed by private
session stop; their expected failure is not counted as screenshot success.
The known InvalidScreen rejection separately verifies safe recovery. The
[summary](../evidence/issue-13/summary.json) and [run index](../evidence/issue-13/runs.json)
preserve all selected samples, actual source hashes and outcomes.

This record concerns feasibility for subsequent production design. Application
support, production session readiness, a client CLI/protocol, generic application
acknowledgment and twenty production workflow runs remain separate requirements.

## Adapter evidence and proposed boundaries

| Adapter | Evidence and reproduction | Decision and practical limit |
| --- | --- | --- |
| Dependencies (#9) | [Environment and build evidence](../evidence/issue-9/README.md), [setup](SETUP.md) | Keep the locked unpatched kdotool source and distribution native bindings. A dependency inventory alone establishes no desktop support. |
| Private desktop (#10) | [Harness evidence](../evidence/issue-10/README.md), [commands](PRIVATE_HARNESS.md) | Keep private D-Bus/Wayland, one 1280x720 scale-1 output, native fixture and owned systemd service. Independent cgroup/runtime observation covers descendants and crash cleanup. `foundation_ready` remains narrower than REQ-009. |
| Windows/focus (#11) | [Window transport and timings](../evidence/issue-11/README.md), [commands](PRIVATE_HARNESS.md#structured-windowfocus-feasibility-probe-11) | Keep unchanged pinned kdotool; observe its worker asynchronously from GLib. Validate metadata and actual active UUID; successful activation exit alone proves no focus. |
| Input (#12) | [Lifecycle evidence](../evidence/issue-12/README.md), [ownership correction](../evidence/issue-12/fd-ownership-fix/README.md), [commands](LIBEI_PROBE.md) | Keep the audited 24-symbol ctypes surface and one GLib owner. Resumed-device gating, explicit release frames, bounded cancellation and uncertainty/reset remain mandatory. |
| Capture/integration (#13) | [Probe commands and contract](CAPTURE_PROBE.md), [capture evidence](../evidence/issue-13/README.md) | Keep one killable child per request owning bus, pipe and image work. Actual raw pixels, EOF and timely complete PNG publication passed; stopping the session after unconfirmed aborted-server cleanup is mandatory. |

## Timing evidence and bounds

Observed historical maxima below belong to their recorded source versions and
machine; they are not real-time guarantees. Do not attribute pre-fix #12 runs
to the corrected implementation or use them as failed-native-setup coverage.

| Phase | Proposed/enforced bound | Existing observation |
| --- | --- | --- |
| Private foundation startup | Shared 30s | #10 exercises the real startup deadline; full adapter readiness was not implemented there. |
| Window query work | 500ms | #11: 300 sequential full calls, max 27.760ms; 90 paced calls, max 34.635ms. |
| Query cleanup | Separate shared 1.5s | #11 longest cleanup phase 666.634ms after the finite slow-script fault. |
| Focus checks | 100ms between starts, one in flight, no catch-up bursts | #11 paced start intervals 100.020–100.282ms. |
| Verified focus | 2s total work | #11 actual successful transitions 22.376–61.557ms; no-op activation correctly times out. |
| Input cancellation dispatch | 100ms | #12 original nine cancel/EOF samples: max 1.040ms; #13 two stalled-capture samples: max 0.859731ms. |
| Fixture-observed release | 500ms under recorded conditions | #12 original samples: max 3.880ms; #13 two samples: max 2.609346ms; distinct from dispatch. |
| Connection/lifecycle and complete reset | 3s each; reset uses one shared budget | #12 original 12 replacements through neutral verification: max 8.379ms. |
| Finite input hold | At most 2s | #12 independent worker-timeout sample uses 150ms. |
| GLib responsiveness | 5ms timer, 100ms maximum-gap acceptance target | #12 original 24 runs: max gap 5.655ms; #13 eleven runs: max 6.294332ms. |
| Capture through accepted publication | 3s | #13 32 samples: max 126.157575ms. |
| Local capture abort cleanup | Separate 1s | #13 seven samples, including safe rejection: max 5.343563ms. |
| Independent control response during stalled capture | Measured under the enclosing scenario budget | #13 two samples: max 50.395019ms. |
| Complete owned-service shutdown | 15s | #10 selected fault max 3.264s; #12 original max 225ms, post-fix max 218ms; #13 eleven runs max 241ms. |
| Failed capture through session cleanup | Up to 19s = 3s + 1s + 15s | #13 six failed-session samples: max 3.318368s; no reusable-session recovery claim. |
| Aggregate foundation plus adapter sequence | Compared with proposed shared 30s startup budget | #13 three samples: max 1.764696s, conservatively including ten captures and acknowledged input. |

The [#13 summary](../evidence/issue-13/summary.json) records timeout detection up
to 3.003762s because the supervisor polls on a 5ms timer. Its acceptance check
still rejects every success at or after 3s; detection latency does not widen the
success deadline.

The existing per-probe 60s work limit, independent 95s service lifetime and
enclosing harness bounds remain in force. Split scenarios into new private
generations instead of widening a failed limit. The observed aggregate sequence
supports retaining the proposed 30s production startup budget for design, but
the after-foundation query/input/capture measurement does not implement the
REQ-009 readiness gate or rename the existing `foundation_ready` phase.

## Ownership and failure conditions retained from review

The libei binding resolves `/usr/lib/libei.so.1` and permits only x86_64 with
audited SHA256
`93897fc311319920c1c25e9422db62ebe8324d54c5e0c3d4a9f15a0a6cac2501`.
Unknown binaries fail before loading or obtaining an EIS FD. For this exact
libei 1.6.0, failed initial epoll ADD leaves the supplied FD caller-owned:
close it immediately on the negative result, before logging, context unref or
possible integer reuse. Successful setup transfers ownership exclusively to
libei. Never probe and close an old FD number after context destruction.

The corrected baseline is merged #12 commit
`ddacc3b3775bf8a5f1d5640692b4b334d498307b`. Its separate correction evidence has
35 passing tests, a renewed compiler audit and three post-fix private runs.
Eight actual negative setup iterations check immediate closure, stable FD count
and survival of reused/unrelated descriptors; a nonblocking socketpair checks
successful ownership transfer. The original 31-test/24-run evidence remains
historical and unchanged.

KWin 6.7.5 retains pressed keys across EIS pause/resume. A RESUMED event therefore
does not clear uncertainty after a held pause. The probe cancels, gates input,
explicitly replaces the connection, and requires neutral fixture keys/modifiers
plus a fresh resumed device before another action. Failed reset remains
unavailable; interrupted requests are never replayed. Neutral-state verification
depends on this instrumented fixture and cannot promise acknowledgment from
arbitrary production applications. Focus checks are observational, not atomic
targeting; a release may reach the newly focused window.

The #12 C++ pause plugin only toggles the selected generation/epoch's actual
EisDevice; it does not fabricate releases or clear KWin's key ledger. It is a
test dependency requiring a rebuild for every KWin release, not a runtime input
backend. The existing compiled signature audit remains a test dependency too.

For capture, format 6 means premultiplied native-endian Qt pixels; the fixed
contract is 1280x720, stride 5120, scale 1, exact requested screen, 3,686,400 bytes
and full EOF. The child closes its original and UnixFd-wrapper writer copies
after message queueing. It decodes and fully validates the PNG before publication;
the parent's 3s deadline includes acceptance of that published file.

A per-capture process is justified because [Python threads](https://docs.python.org/3/library/threading.html)
cannot be forcibly stopped through their API; a Future timeout does not terminate
an in-progress native codec or file operation. A directly executed child supplies
an owned kill/reap boundary without sharing the GLib/libei context. Exact KWin
[render source](https://github.com/KDE/kwin/blob/ab7df7ccb7c6af20f4b279cd6220f7cd3d2267d7/src/plugins/screenshot/screenshot.cpp)
and [D-Bus/writer source](https://github.com/KDE/kwin/blob/ab7df7ccb7c6af20f4b279cd6220f7cd3d2267d7/src/plugins/screenshot/screenshotdbusinterface2.cpp)
establish synchronous fresh offscreen rendering, metadata before pipe writing,
and the writer's 60s per-poll wait. Those native waits do not enforce the proposed
capture deadline.

A killed child proves local closure, not completion of compositor render/writer
work. Actual read-only server-FD inspection returned PermissionError. Broken-pipe
diagnostics, a responsive compositor or a later successful
capture cannot alone prove earlier server cleanup. Unconfirmed failures make
the private session unavailable and require observed service termination under
the separate 15s bound. A known InvalidScreen rejection before capture is the
narrow recoverable exception. Capture and image work never own the parent's
libei connection or postpone its independent cancellation dispatch.

The real 750ms query-stall test also preserves a fundamental limit: GLib can
dispatch release while KWin is busy, but application receipts wait for KWin.
Synchronous small diagnostic/control filesystem operations remain measured
normal-storage feasibility behavior, not guarantees under a hung filesystem.

## Exact dependency baseline

The [#9 inventory](../evidence/issue-9/environment.json), #12 audit/plugin receipts
and each run manifest provide complete native/Python/Cargo provenance. The
recorded Arch x86_64 baseline is:

| Component | Recorded version/pin |
| --- | --- |
| KWin / Plasma workspace | 6.7.5-1; KWin release commit `ab7df7ccb7c6af20f4b279cd6220f7cd3d2267d7` |
| Qt / KF6 CoreAddons | 6.11.2-3 / 6.30.0-1 |
| systemd / D-Bus | 261.3-1 / 1.16.2-1 |
| Wayland / wayland-protocols / libxkbcommon | 1.26.0-1 / 1.49-1 / 1.13.2-1 |
| Python / PyGObject / dbus-python / Pillow | 3.14.7 / 3.56.3 / 1.4.0 / 12.3.0, distribution bindings |
| GLib / libffi / libei | 2.88.3 / 3.8.0 / 1.6.0 |
| libjpeg-turbo / zlib packages | 3.2.0-2 / 1:1.3.2-3 |
| kdotool | 0.3.0, `be03ce90c09350898556436bac74ed35fe928617`, no patches |
| rustc / cargo | 1.95.0 (`59807616e`) / 1.95.0 (`f2d3ce0bd`) |
| GCC | 16.2.1+r23+gd564253eb6c8-1 |
| Pause-plugin local ECM | 6.26.0, checksum-pinned build input |

kdotool Cargo.lock SHA256 is
`d6beea15d1a9254586d71ac1c5c55d088d9dc3c9a8e980c6af7c2d8ee8f25edc`;
the selected binary SHA256 is
`62e7ee53096d933ec29e8e5d439b895590f851a40f0dcd87b87db9a6dc4749de`.
The unchanged pause plugin SHA256 is
`451fab0a1f219f7aabb7f81fff84743be9f8847d2a54db73026403b9d99de168`,
with IID `org.kde.kwin.PluginFactoryInterface6.7.5`.
These identify evaluated artifacts, not arbitrary rebuilds or a broad support
range. Changed native libraries require renewed audits and the dependency-change
reruns in [SETUP.md](SETUP.md#when-dependencies-change).

The completed feasibility evidence supports these adapter boundaries on the
recorded machine. Dependent production work must preserve the audited ownership
rules, input uncertainty gate, complete capture acceptance deadline and mandatory
session-stop policy. Generic application reset semantics, production readiness,
client behavior and the production support matrix remain unimplemented work;
the M1 decision does not certify them.
