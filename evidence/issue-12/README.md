# Issue #12 — libei binding and real lifecycle evidence

**Pass for the M1 feasibility scope:** all eight scenarios passed in three fresh
private desktops each. Every service cgroup was observed empty after cleanup.
The original compiler audit and 31 unit/regression tests passed. Fresh PR review
then found a failed-setup FD ownership error; the [scoped correction and new
35-test/three-run evidence](fd-ownership-fix/README.md) are recorded separately.
The original 24-run files and hashes remain unchanged. This is not the eventual
20-run production workflow or an application support claim.

Reproduce using [docs/LIBEI_PROBE.md](../../docs/LIBEI_PROBE.md). No host desktop,
permission override, global package installation, viewer, XWayland, ydotool,
kwin-mcp or MCP component was used. The test-only pause plugin was explicitly
approved and loaded only into the three private pause generations.

## Acceptance and samples

| Check | Evidence | Outcome |
| --- | --- | --- |
| All 24 ctypes symbols, return/argument types, ABI sizes and constants | [audit.json](audit.json), [compiler assertions](libei-audit.c) | Pass against installed libei 1.6.0 headers; ei_dispatch is void. |
| Actual resumed gating, evdev W and Shift+A, fixture releases and visual change | `runs/input-{1,2,3}/` | Pass; uppercase A, real Wayland receipts, neutral modifiers and separate presentation receipts. |
| Cancellation during holds/modifiers, separate client cancel/disconnect, worker timeout | `runs/cancel-{1,2,3}/` | Pass; explicit key releases acknowledged before normal hold end. |
| Actual idle pause/resume plus held pause, no emission while unavailable | `runs/pause-{1,2,3}/` | Pass; real PAUSED/RESUMED events from stock KWin/libeis, not initial internal paused state. |
| Removal/rebind and server disconnect | `runs/removal-{1,2,3}/`, `runs/disconnect-{1,2,3}/` | Pass; actual lifecycle events, no further scheduled presses, acknowledged recovery. |
| Failed reset stays unavailable | `runs/reset-failure-{1,2,3}/` | Pass; explicit later reset succeeds without replay. |
| Focus loss and asynchronous query boundary | `runs/focus-loss-{1,2,3}/`, `runs/slow-query-{1,2,3}/` | Pass; new focused fixture receives release; original reenters neutral. GLib responds during a real 750ms KWin query stall. |
| Resource/safety regression tests | [unit-tests.log](unit-tests.log), [builder checks](plugin-build-runner-checks.json) | 31 tests pass; clean build environment, timeout/output cap and compiler-group cleanup checks pass. |

[summary.json](summary.json) contains all numeric samples.
[runs.json](runs.json) lists generation identities and hashes of the checked-in
raw files. Each run includes original fixture receipts, libei/action timeline,
probe results, initial focus snapshot, D-Bus introspection, compositor diagnostics,
manifest, ownership/finalizer and controller cleanup result. Full per-query
artifacts remain at the durable manifest path printed by each controller result;
#11's checked-in evidence separately audits that unchanged window transport.

## Measurements

Observed maxima on the recorded Arch/KDE machine, not real-time guarantees:

| Measurement | Samples | Maximum |
| --- | ---: | ---: |
| Client cancel/EOF request to release dispatch | 9 | 1.040ms |
| Client cancel/EOF request to actual fixture release | 9 | 3.880ms |
| GLib heartbeat gap | 24 runs | 5.655ms |
| Explicit connection replacement through neutral-state verification | 12 | 8.379ms |
| Held pause through reset's fixture-neutral observation | 3 | 305.985ms |
| Requested 250ms plugin pause through automatic resume | 6 | 250.927ms |
| Focus-loss detection measured from action start | 3 | 126.552ms |
| Stalled-query timeout detection from query start | 3 | 505.056ms |
| Complete private-service shutdown | 24 | 225ms |

The held-pause measurement includes a deliberate 300ms observation interval to
prove resumed-but-uncertain input stays gated; it is not reset's processing cost.
Focus loss is induced just after the acknowledged press, so its action-start
measurement is a conservative combined setup/detection interval. The 500ms
query deadline is checked on a 5ms GLib timer; the measured detection is within
one timer tick plus scheduling variation. The query child additionally performs
#11's separately bounded cleanup. The 750ms compositor stall delays delivery;
no synchronous child wait blocks the input loop.

Proposed enforced bounds remain 100ms cancellation dispatch, 500ms fixture
release acknowledgment, 3s connection/lifecycle/shared reset, 2s finite hold,
100ms focus-query cadence with 500ms query work budget, and existing 15s harness
cleanup. These conservative bounds were not widened to accommodate a failed run.

## KWin pause behavior and successful recovery

KWin 6.7.5 leaves its pressed-key ledger held after an EIS pause. In all three
held-pause samples, the fixture still had Shift+W down after automatic RESUMED.
The adapter correctly retained uncertainty and blocked new input. Replacing the
private EIS connection made stock KWin release both actual keys and send neutral
modifiers. Only then did the adapter permit the next acknowledged W action.
[Extracted original release receipts](pause-reset-releases.json) show this ordering
for every pause sample. The raw `ready` timeline row denotes resumed device
negotiation; the separate reset gate remains closed until neutral receipts are
consumed. It is not session/action readiness by itself.

The plugin only invokes existing `InputDevice::setEnabled`; it never emits a key,
fakes an event, or calls KWin's release helper. Thus it does not conceal this
behavior. No KWin patch is claimed. Explicit uncertainty followed by a verified
private connection reset satisfies this probe's REQ-029/031 recovery contract;
continuing input merely because RESUMED arrived would be incorrect.

## Versions, source audit and maintainability decision

Tested: Arch `kwin 6.7.5-1`, `libei 1.6.0-1`, Python 3.14.7, GCC
`16.2.1+r23+gd564253eb6c8-1`, Qt `6.11.2-3`, KF6 CoreAddons `6.30.0-1`, and pinned
kdotool `be03ce90c09350898556436bac74ed35fe928617`. Exact Python version appears in
summary.json; [#9 baseline](../issue-9/README.md), run manifests and plugin receipt
record resolved dependencies. Native bindings remain the distribution packages.

- [KWin v6.7.5](https://github.com/KDE/kwin/tree/ab7df7ccb7c6af20f4b279cd6220f7cd3d2267d7), commit `ab7df7ccb7c6af20f4b279cd6220f7cd3d2267d7`: `src/plugins/eis/eisbackend.cpp` supplies private connect/disconnect; `eisdevice.cpp:88–91` supplies pause/resume. Only creation enables EIS devices in stock source. The touchpad toggle excludes them. `src/backends/libinput/device.h:78` and `device.cpp:476` expose enabled only for libinput devices; EIS has no such D-Bus property.
- [libei 1.6.0](https://gitlab.freedesktop.org/libinput/libei/-/tree/8a46bf3d4b6af7f25a61be53386cf618218114ef), commit `8a46bf3d4b6af7f25a61be53386cf618218114ef`: `libei.c:988–1005` establishes immediate dispatch during FD setup; O_NONBLOCK is required. Correction from fresh review: `util-sources.c:91–112,189–199` leaves the supplied FD open after failed epoll add; the original claim that failure transferred ownership was wrong. The pinned implementation now gets immediate caller-side close on that negative path; successful transfer remains library-owned. `libei-device.c:306–311` is the actual PAUSED-event path, unlike initial ADDED/internal paused state or removal.
- Installed libei.h SHA256 `8aabc649cbf009bec552e45d855ec8be28ef416e8f62450ab453c64a2fd9c847` equals the exact release's source header. Loaded `/usr/lib/libei.so.1.6.0` SHA256 is recorded in audit.json.
- [Plugin build receipt](plugin-build-receipt.json): local ECM 6.26.0 archive checksum, exact compiler/link commands, installed KWin headers and library hashes, plugin metadata and resolved native dependencies. Final plugin SHA256 is `451fab0a1f219f7aabb7f81fff84743be9f8847d2a54db73026403b9d99de168`. Embedded IID is exactly `org.kde.kwin.PluginFactoryInterface6.7.5`; EnabledByDefault is false. Rebuild for every compositor release.

**Decision: retain Python ctypes + GLib.** This limited surface is manageable with
explicit declarations, a compiler header audit, one event-loop owner and real
lifecycle tests. A compiled runtime boundary would not solve compositor-held
state on pause. The C audit and C++ plugin are test-only dependencies; neither
replaces the Python architecture. The plugin is the smallest identified real
pause mechanism, avoiding a full KWin patch/build or a fake substitute server.
Physical-key breadth, text/pointer mapping, production worker/client protocol and
generic reset semantics still belong to subsequent milestones.

## Development failures retained

No unexpected failure occurred in the final 24-run matrix. Two earlier local
implementation failures are preserved under [development-failure](development-failure/):
initial plugin CMake configuration lacked explicit Qt6 Widgets discovery required
by the installed KWin target; adding that declared dependency fixed configuration.
An initial focus-loss run reached its assertions then raised NameError because a
removal-test branch was misplaced during editing; moving it back to its scenario
fixed the issue. That failed private service still cleaned its cgroup within
234ms. Neither failure was omitted or counted as a passing sample.
