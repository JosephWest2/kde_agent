# M1 libei compatibility probe (#12)

`tools/libei_probe.py` proves the project-owned Python/libei sender on the existing
private KWin harness. It is feasibility tooling, not the production session CLI.
Actual Wayland fixture receipts distinguish application acknowledgment from input
dispatch; presentation receipts are separate. There is no screenshot, full text
mapping, pointer implementation, application support or twenty-run support claim.

## Reproduce

Use the #9 [dependency setup](SETUP.md) and pinned kdotool artifact. All generated
builds and runs stay under ignored `.local/`; nothing is installed globally.
Substitute the absolute checkout directory below:

```sh
/usr/bin/python -I tools/libei_binding.py --output .local/issue12-audit
/usr/bin/python -I tools/build_eis_fault_plugin.py
/usr/bin/python -m unittest discover -s tests -v

/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tools/libei_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool input
/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tools/libei_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool cancel
/usr/bin/python -I /checkout/tools/private_harness.py run \
  --eis-fault-plugin /checkout/.local/issue12-eis-fault/build/issue12_eis_fault.so -- \
  /usr/bin/python -I /checkout/tools/libei_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool pause
```

Use the same command without `--eis-fault-plugin` for `removal`, `disconnect`,
`reset-failure`, `focus-loss` and `slow-query`. The pause option copies exactly the
local plugin into this generation's private runtime and enables its ID only in
that generation's kwinrc. Only KWin receives the additional plugin path and
opt-in environment; applications and probe children retain the normal clean
environment. No EIS permission override is needed on tested KWin 6.7.5.

Each run returns the harness JSON result and manifest path. Inspect
`libei-probe.json`, `libei-timeline.jsonl`, original `fixture-events.jsonl`,
`eis-introspection.xml`, `kdotool-probe.json`, compositor/probe diagnostics and
cleanup records. The full per-query evidence remains in that durable run folder.
A success requires observed empty service cgroup and no input uncertainty left
at the end. A failed operation is preserved as failure, not silently retried.

The standalone compiler audit uses a constructed PATH/locale environment and
kills/reaps its own command group on timeout. The plugin builder adds private
HOME/XDG/TMP, a bounded output cap and the same group cleanup. It fetches only
checksum-pinned ECM 6.26.0 into its local prefix, then uses the installed exact
KWin CMake dependency contract. Its receipt records source/header/library hashes,
compiler commands, dependency versions, resolved libraries and embedded plugin
metadata. Rebuild and rerun after dependency changes, especially each KWin release.

## What the real scenarios establish

| Scenario | Actual operation and required observation |
| --- | --- |
| `input` | Reject a partly invalid chord before any native call; W and Shift+A presses/releases, uppercase A, neutral modifiers and presented input state. |
| `cancel` | After acknowledged presses, a separate process requests cancellation or disconnects its channel; W and Shift+A release receipts and timestamps. A worker timer independently cancels a finite hold. |
| `pause` | Reject wrong generation; plugin toggles the real EisDevice off/on. Consume PAUSED, reject emission, consume RESUMED and acknowledge a new key. Repeat with Shift+W held; preserve uncertainty across auto-resume, replace connection and observe neutral state before new input. |
| `removal` | Unbind keyboard on the real seat while held, consume DEVICE_REMOVED, stop emission, reset. Also unbind/rebind an idle device on the same connection and require a newly resumed keyboard and receipts. |
| `disconnect` | Call the private EIS server's disconnect(cookie) while held, consume real DISCONNECT/removal, gate input, replace connection and acknowledge recovery. KWin remains alive. |
| `reset-failure` | A missing private EIS endpoint fails within its deadline; input stays unavailable. A subsequent explicit reset restores a fresh resumed connection and acknowledged input. |
| `focus-loss` | A second owned native fixture takes focus during a hold. A fresh query detects loss; the new focused fixture acknowledges release. Refocus the original and observe a neutral keyboard-enter before another acknowledged action. |
| `slow-query` | The fixed #11 query bridge runs a 750ms compositor-side stall. The Python GLib context remains responsive, detects its 500ms query deadline and dispatches release while the child is busy; the fixture acknowledges release once KWin resumes. |

Requested seat removal and server cookie disconnect are documented fault
procedures, not claims about physical device hotplug or every possible failure.
The plugin is necessary because the stock EIS sender interface does not expose
an enable toggle. The similarly named writable InputDevice D-Bus property belongs
to the libinput backend and cannot select EIS devices. Initial ADDED devices are
internally paused but that does not constitute a real PAUSED event.

The [fault plugin](../tools/eis_fault_plugin/README.md) selects exactly one
`KWin::EisDevice` with the current generation/connection-epoch keyboard name.
It invokes only the installed device's existing `setEnabled(false/true)` method.
It neither emits keys nor fabricates libei events or clears KWin's held-key ledger.
It retains a QPointer, resumes on a bounded 250ms timer/controller disappearance/
unload, and is never a production input dependency. Timer scheduling is measured,
not a hard real-time promise.

## Binding audit and ownership

`tools/libei_binding.py` declares 24 required symbols. Its generated C audit uses
compiler type compatibility assertions against installed libei headers for every
argument/result signature, enum value and ABI size. `ei_dispatch.restype` is
explicitly None (void); connection state comes from consumed events.

| Object or FD | Ownership rule |
| --- | --- |
| `ei_new_sender` | One owned context, released with ei_unref. |
| D-Bus UnixFd | `.take()` once transfers ownership to Python. Set O_NONBLOCK and non-inheritable before setup. Close locally only if preparation fails before transfer. |
| `ei_setup_backend_fd` | Consumes the socket even on its supported implementation's error path. Its initial internal dispatch can block without O_NONBLOCK. Context teardown closes it; Python must not close it again. |
| `ei_get_fd` | Borrowed poll FD; remove GLib sources before context destruction/FD reuse. Terminal DISCONNECT removes its watch immediately. |
| `ei_get_event` | Owned event, always unrefed in finally, including unknown event types. |
| Event seat/device getters | Borrowed objects; explicit ref before retention beyond the event, one unref on removal or disposal. |
| Originating private D-Bus connection | Retained throughout EIS ownership; KWin destroys its associated contexts when that caller disappears. |
| Retained fault-plugin InputDevice | Borrowed QPointer only; the plugin never deletes/owns the compositor device. |

The capability API is variadic. Only its fixed pointer argument is declared in
ctypes; capabilities and zero sentinel are explicitly promoted c_int values.
The installed 1.6.0 implementation reads enum/int values until zero, although its
header describes NULL termination. This is audited for the tested x86_64 SysV
ABI, not a cross-platform binding promise. Real bind/unbind/rebind tests exercise
it. All other symbols use explicit pointer, uint32, uint64, bool, enum/int or void
types. Pointer addresses are diagnostic values scoped to a connection epoch;
addresses can be reused after reset and are not persistent device identities.

## Cancellation, release and timing

One GLib context owns every libei call and state transition. Its FD callback
consumes events in bounded batches; 5ms timers service receipt reads, action
limits and focus polling. Synchronous #11 query logic runs in a persistent child,
with one request in flight; its waits never run in the libei callbacks. D-Bus
calls are asynchronous while the same GLib context continues processing input.
A private cancellation channel bypasses ordinary action sequencing. Every press
batch and release batch has an explicit libei frame; stop-emulating is never
used as a substitute for release.

| Phase | Enforced provisional limit |
| --- | --- |
| EIS connection or lifecycle wait | 3s |
| Complete connection replacement/neutral-state reset | Shared 3s |
| Key hold | At most 2s; timeout sample uses 150ms |
| Focus checks | 100ms between starts, no overlap; 500ms query work limit |
| Explicit focus operation | #11 2s work limit, separate bounded child cleanup |
| Cancellation request to release dispatch | 100ms acceptance bound |
| Cancellation request to fixture release | 500ms acceptance bound on recorded host |
| Plugin pause timer | At most 250ms requested interval; actual delay recorded |
| Harness cleanup | Existing 15s shared cleanup limit and 95s independent service lifetime |

Focus selection is checked, not atomic compositor targeting. Focus loss can route
the attempted release to a new active window, which the fault scenario records.
When KWin is stalled, the GLib loop can stop emission and queue release promptly
but cannot make the compositor deliver it instantly. The tested 750ms stall
finishes roughly 250ms after the 500ms detection limit. An indefinitely hung
compositor still requires bounded session cleanup; no fixture acknowledgment is
claimed without a real receipt.

**Observed correctness issue and mitigation:** stock KWin 6.7.5 does not release
its own pressed-key ledger on EIS pause. After pause/resume during Shift+W, the
fixture still has those keys held. The adapter cancels, reports release uncertainty,
rejects further input even after RESUMED, and requires explicit connection reset.
Destroying the private EIS context makes KWin release its held keys; the probe
observes neutral fixture state/modifiers and a fresh resumed device before
allowing another acknowledged action. The fault plugin does not repair or mask
this behavior. Reset is a deliberate recovery, never replay of the interrupted
request. Failed reset remains unavailable.

## Boundary decision

Keep the Python ctypes + GLib proposal for the next milestone: the limited
surface passes its compiler audit and real input/lifecycle tests with explicit
ownership and measured cancellation. A small compiled runtime binding is not
needed by this evidence; it also would not repair KWin's pause ledger behavior.
The C signature audit and C++ pause plugin are test dependencies only. No native
worker or replacement input backend is introduced.

The implementation remains a feasibility probe: it uses receipt-based
neutral-state checking available in the instrumented fixture. Production must
preserve the conservative uncertainty/reset contract without promising generic
application acknowledgment. Full mappings, pointer support, client protocol,
production interruption handling and session readiness remain later work.
See [recorded evidence and measurements](../evidence/issue-12/README.md).
