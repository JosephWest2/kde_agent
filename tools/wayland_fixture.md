# Native Wayland fixture

`private_harness.build()` builds `wayland_fixture.c` with the generated stable
xdg-shell and presentation-time protocols, `wayland-client`, and `xkbcommon`.
The existing `tools/private_harness.py run` smoke exercises the default
stdin-driven primary surface. No AT-SPI integration is used.

The default remains a 640×360 primary toplevel titled `KDE Agent Native Fixture`
with app ID `org.kde_agent.fixture`. It requires the private Wayland and D-Bus
environment and a 32-character lowercase hexadecimal `HARNESS_GENERATION`.
`--autonomous` ignores stdin and permits the existing all-zero generation fallback
when the environment does not provide one. Existing `--window-delay-ms`,
`--exit-after-ms`, `--exit-code`, and sleeping `--descendant-ms` modes are retained.

For structured-window evidence, add any of:

| Option | Effect |
| --- | --- |
| `--sibling` | Independent 480×300 toplevel labeled `sibling` |
| `--dialog` | 320×180 toplevel labeled `dialog`, with its xdg parent set to `primary` |
| `--child-window-ms N` | One child process creates a 400×240 toplevel labeled `child`, then exits after N ms |
| `--title-mode normal\|empty\|omitted` | Set the normal title, explicitly set an empty title, or omit `set_title` |
| `--app-id-mode normal\|empty\|omitted` | Set the normal app ID, explicitly set an empty app ID, or omit `set_app_id` |

All surfaces share the chosen metadata policy; default title/app ID are
intentionally identical across the primary, sibling, dialog, and child. There
are three fixed surface slots in the parent and at most one window child. Slots
cannot be reused after closure. Each surface owns its configure state, rendering
state, presentation feedback, and independent shared-memory buffer.

For example, a public launch can pass these fixture arguments:

```text
--autonomous --sibling --dialog --child-window-ms 12000 --exit-after-ms 15000
```

The window child inherits the caller's cgroup and standard output, closes its
inherited Wayland descriptor, and execs the same binary to obtain a new Wayland
connection. Its internal `--child-surface` option requires autonomous timed mode
and prevents recursive child-window spawning. A `window_child_spawned` receipt
records the child PID. Parent and child sequence numbers are independent; key
receipts by `(fixture_pid, seq)`. The child can outlive the parent until its own
timer expires, as with the existing sleeping descendant mode. Native evidence
must independently read each PID's birth identity and cgroup while it is alive;
a printed PID alone is not association proof.

Stdin still accepts `state VALUE CONTROL_ID` for the primary and `close` for all
surfaces in that process. New commands are `open sibling`, `open dialog`, and
`close LABEL`, where LABEL identifies an open surface in that process. Opening a
dialog requires an open primary. Closing one surface leaves other surfaces
running; closing the last surface exits the process. Autonomous mode does not
read these commands.

`surface_created`, `configure`, `map`, `committed`, `presented`, `discarded`, and
`close` receipts include the fixture-local `surface` label. `surface_created`
records parent, requested dimensions, and metadata modes. `configure` records
the acknowledged serial and current client size. `map` means the first buffer
commit (`receipt: first_buffer_commit`); only `presented` proves compositor
presentation. Dimensions in configure/commit receipts describe content size,
not frame position or decoration size. `close` records `control`, `compositor`,
or `shutdown` as its source. Per-surface receipts also include `role`, normally
the surface label; a close-confirmation dialog has role `confirmation`.

For graceful-close evidence, select a deterministic compositor-close mode:

| Option | Effect |
| --- | --- |
| `--close-mode normal` | Default: destroy only the requested surface; exit when no surfaces remain |
| `--close-mode refuse` | Record every close callback and leave all surfaces alive |
| `--close-mode delay --close-delay-ms N` | Destroy the selected surface N ms after its first close callback; repeated callbacks do not restart the timer |
| `--close-mode confirmation` | First close callback creates a parented custom-rendered dialog in the reserved `dialog` slot; retain the selected surface until explicit dialog acknowledgment |

Non-normal modes require a positive `--exit-after-ms` later than the initial
window delay. `--close-delay-ms` must be positive in delay mode and is invalid
in other modes. Durations remain bounded by 86400000 ms. The autonomous expiry
is fixture cleanup, not a response to the close request. Set it beyond the
close observation interval when proving refusal or confirmation persistence.
Existing per-surface resize/destroy schedules remain independent cleanup
controls and should likewise be placed outside that interval.

Confirmation mode reserves the dialog slot, so it rejects `--dialog`, dialog
schedules, and the `open dialog` control command. The dialog is parented to the
selected surface, including a selected sibling. Its acknowledgment button is
the white rectangle at client coordinates `16 <= x < width - 16` and
`height - 56 <= y < height - 16` (default `16..303`, `124..163`). Only a subsequent
Wayland Return/keypad Enter **press while the dialog has keyboard focus**, or a
left-button **press inside that rectangle on the dialog**, acknowledges it.
Key releases, unrelated input, repeated parent close callbacks, and dialog close
callbacks cannot acknowledge it. Acknowledgment destroys the dialog and its
selected parent; another sibling remains alive. The fixed slot allows one
confirmation per fixture process; afterward any surviving sibling uses normal
close behavior. Stdin `close`/`close LABEL` bypass these modes for explicit test
cleanup, and cannot stand in for evidence of compositor close or dialog input.

Every receipt includes `monotonic_ns`, `fixture_pid`, and `seq`.
`close_requested` records each actual xdg_toplevel close callback, its surface,
mode, and increasing per-surface `request_count`. `close_refused`,
`close_delayed` (including the original absolute `due_ns`),
`confirmation_opened`, and `confirmation_pending` describe the response.
`confirmation_accepted` identifies its selected `target` and input `source`.
Key, button, motion, and axis receipts carry the actual input `surface` and
`role`, or null when there is no known input surface. `close` precedes protocol
object destruction; `destroy` follows it with the same source. Additional
destruction sources are `scheduled`, `delayed_compositor`, and `confirmation`.
`fixture_exit` records normal fixture-main completion after Wayland disconnect;
independent process observation must still establish actual process exit.

For example, use `--autonomous --close-mode confirmation --exit-after-ms 15000`
to prove a close timeout leaves a discoverable dialog, or
`--autonomous --close-mode delay --close-delay-ms 3000 --exit-after-ms 15000`
to exercise a late exit. Assert exactly one `close_requested` on the selected
surface and zero dialog close callbacks, input receipts, or
`confirmation_accepted` before any explicit follow-up input. Fixture receipts
alone do not establish the production application's observed lifetime.

With `--sibling --exit-after-ms N`, a normal close destroys the selected surface
while its sibling and root remain alive. With `--descendant-ms N`, closing the
last root-process surface exits that root while the existing detached sleeping
descendant remains alive until its own timer. Its existing `descendant_started`
and `descendant_exit` receipts, plus independent cgroup/process observations,
distinguish root exit from whole-application exit. Neither case needs a new
surface or process infrastructure path.

Empty and omitted metadata do not promise that KWin reports JSON null: record
its actual caption/class values, including defaults or empty strings. Native
Wayland also does not reliably force missing PID or client bounds; null decoder
coverage belongs in fixed synthetic tests. An unassociated evidence window can
use the same fixture binary launched as service infrastructure outside the
application subgroup; the fixture does not create or move cgroups.
