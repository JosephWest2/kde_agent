# Native Wayland fixture

`private_harness.build()` builds `wayland_fixture.c` with the generated stable
xdg-shell and presentation-time protocols, unstable text-input-v3, `wayland-client`, and `xkbcommon`.
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
`role`, or null when there is no known input surface.

The fixture binds `wl_seat` at version 9, or lower if the compositor offers less,
and every pointer event through version 9 has a receipt. `pointer_enter` and
`motion` record surface-local `x`/`y`. `button` records the pointer position at
the press. `pointer_frame` closes each group of events and records the current
`x`/`y`. The wheel receipts keep the protocol's numbers. `axis` has `axis` (0
vertical, 1 horizontal; positive is down or right) and the continuous `value`.
`axis_value120` has `value120`, ±120 per wheel notch, from version 8. `axis_discrete`
has `discrete` notches and is sent only to version 5–7 clients. `axis_source`
(0 wheel, 1 finger, 2 continuous, 3 wheel tilt), `axis_stop` and
`axis_relative_direction` (0 identical, 1 inverted; version 9) complete the set.
KWin 6.7.5 passes on an EIS discrete scroll as `axis_relative_direction`,
`axis_value120` and `axis` (±15 per notch) per axis, horizontal first, then one
`pointer_frame`. It sends no `axis_source` and, unless the sender stops the
scroll, no `axis_stop`. `close` precedes protocol
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

## Text input

`--text-input` gives the fixture one text field through `zwp_text_input_v3`, for
the input-method `type` tests ([INPUT.md](../docs/INPUT.md#non-ascii-text-the-input-method)).
Without it the fixture never binds text-input, so it is the "no text field" case:
KWin activates no input method for it.

- On `text_input_enter` (keyboard focus) the field is enabled and its state sent:
  `set_surrounding_text` with the last 4000 bytes of the field (starting on a
  UTF-8 boundary), cursor and anchor at the end, a content type and `commit`.
  `text_input_leave` disables it.
- `commit_string` is applied on the following `done`, and the state is sent again
  with change cause `input_method`. Key presses while the field is enabled also
  edit it (the key's text; Return and Tab insert `\n` and `\t`; BackSpace deletes
  one character) and send the state with cause `other`. Every key still has its
  usual `key` receipt.
- Receipts: `text_input_ready` (bound at start-up), `text_input_enter` and
  `text_input_leave` with the surface, `text_input_state` (`reason`, `commits`,
  `surrounding_bytes`, `field_bytes`), `text_input_commit_string` with the exact
  `text` and its `bytes`, `text_input_preedit_string`,
  `text_input_delete_surrounding_text`, and `text_input_done` with `serial`,
  `applied` (a commit was applied) and the field's whole `text` and `bytes`. These
  receipts hold the typed text on purpose: they are the test's evidence, in the
  application's own log.
