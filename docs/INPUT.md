# Keyboard and pointer input

## Public `key` and `type`

```sh
agent-desktop --json key  --window REF ctrl+shift+t [--hold 0.05]
agent-desktop --json type --window REF 'Hello, World!' [--timeout 30]
agent-desktop --json type --window REF 'héllo ✓ 中文 🎉' [--method auto|keys|input-method]
```

**Focus first.** Both commands query the window before the first stroke and
require it to exist and be focused. Otherwise they fail with `target_lost` (reason
`focus_lost`) and send nothing. Use `focus` first. They also fail with
`target_lost`, reason `compositor_surface_open`, while a KWin surface such as the
window menu is open, even though the window is still active: that menu takes the
keyboard and its accelerators would act on the window. Close it with a screen
click outside it ([row kinds](WINDOWS.md#row-kinds-windows-popups-and-compositor-surfaces)).
Both refusals before the first stroke send nothing (outcome `not_started`). If
either is found by a recheck instead, it is reported as below.

**Focus rechecks.** While a hold or a sequence is still being sent, the window is
queried again every 250ms. If it is gone or no longer focused, everything held is
released at once and the request fails with `target_lost` and the progress so far
(`strokes_sent`, `strokes_total`, `key_held`, `focus_rechecks`). Detection is not
instantaneous: the next query starts 250ms after the previous one finishes, and a
query takes about 0.1s (at most 0.5s), so typically 0.25–0.35s and at worst about
0.75s of input, including the release itself, can reach whatever took focus. A
recheck still running when the last stroke is sent is waited for, so a late loss
is reported even though every stroke was sent. No recheck is started when less
than 250ms of input remains at the pace achieved so far, so `key` with the default
hold and short `type` text never recheck. Successful results
include `focus_rechecks`.

**Key names.** Chords are names joined by `+`, case-insensitive, pressed in order
and released in reverse, with up to 8 distinct keys. Each name is one physical key:

| Group | Names (aliases) |
| --- | --- |
| Letters, digits | `a`–`z`, `0`–`9` (`A` means the same physical key as `a`) |
| Modifiers | `ctrl` (`control`, `ctrl_l`), `ctrl_r`, `shift` (`shift_l`), `shift_r`, `alt` (`alt_l`), `alt_r` (`altgr`), `super` (`super_l`, `meta`, `win`), `super_r` |
| Editing | `return` (`enter`), `escape` (`esc`), `tab`, `backspace`, `delete` (`del`), `insert`, `space`, `menu` |
| Navigation | `up`, `down`, `left`, `right`, `home`, `end`, `page_up` (`prior`), `page_down` (`next`) |
| Function | `f1`–`f12`, `print`, `pause`, `caps_lock`, `num_lock`, `scroll_lock` |
| Keypad | `kp_0`–`kp_9`, `kp_enter`, `kp_add`, `kp_subtract`, `kp_multiply`, `kp_divide`, `kp_decimal` |
| Punctuation | `minus`, `equal`, `bracketleft`, `bracketright`, `semicolon`, `apostrophe`, `grave`, `backslash`, `comma`, `period`, `slash`, or the unshifted character itself (`-`, `/`, …) |

Shifted symbols are not key names: `ctrl+!` is rejected with a hint to use
`ctrl+shift+1`. `+` cannot appear inside a name; use `shift+equal`. Games can hold
keys such as `w` or `shift+w` with `--hold` (at most 2s).

**Text.** `type` picks a method for the whole text (`--method`, default `auto`):
text made only of printable ASCII, space, newline and tab goes as keys, exactly as
before; text with any other character goes whole as one input-method commit
([below](#non-ascii-text-the-input-method)). `--method keys` always uses keys and
rejects any other character before anything is sent, with its `index` and
`codepoint` (`unsupported_input`, non-ASCII lookalikes such as the Kelvin sign
included). `--method input-method` commits even ASCII text.

Keys map printable ASCII, space, newline (Return) and tab (Tab) to the
US layout, adding Shift where needed. If `key caps_lock` has turned Caps Lock on,
`type` inverts Shift for letters so the text still comes out as written; only this
toolkit sends input to the private desktop, so the worker tracks that state. Each character is one
press and one release, about 10ms apart, so roughly 170 characters fit the default
3s and about 1900 fit the 30s maximum. Text whose estimate (15ms per character,
plus 0.25s for a final focus recheck when it takes longer than 250ms) does not fit
the remaining time fails with `timeout`, phase `budget`, and sends nothing.

### Non-ASCII text: the input method

Text the US layout cannot type is sent through KWin's `zwp_input_method_v1`: the
worker is the private session's input method and commits the whole text as one
`commit_string` to the focused text field. No key events are sent, so a newline or
tab in that text is inserted as a character, never as Return or Tab: it can't
activate a default button or move focus. Combining characters, emoji and any other
Unicode go through unchanged (UTF-8). The result has `method: input_method`,
`characters` (code points), `bytes` (UTF-8), `confirmed` and `confirmation_reason`;
key results have `method: keys`.

- **Limit.** One commit carries at most 4000 UTF-8 bytes (text-input's limit;
  4001 would break the connection with nothing delivered). Longer text is refused
  before anything is sent: `invalid_arguments`, reason `text_too_long`, with
  `bytes` and `limit_bytes` (4000). Split it into several `type` requests; there
  is no automatic chunking. Empty text with `--method input-method` is
  `invalid_arguments`, reason `empty_text`. The limit doesn't apply to keys.
- **Preconditions.** The same focus and compositor-surface checks as keys, then
  the focused window must have an active text field: KWin activates the input
  method for a Wayland client with text-input enabled. If none is active within
  200ms (by when KWin's activation was received, not when the worker got to it)
  the request fails before anything is recorded or sent: `unsupported_input`
  (exit 5), reason `text_input_unavailable`, outcome `not_started`. That covers
  windows without a text field (the fixture without `--text-input`), a focused
  widget that isn't a text field, and X11 (XWayland) clients, which can't receive
  it. If the private KWin doesn't offer the input method at all (see `health`
  below), the request fails with `input_unavailable`, reason
  `input_method_unavailable`. The client is acquired once per request: a
  connection that fails during the request fails it, and the next request reopens it.
- **Same field.** If the field became active after the focus check started (for
  example, the window was only just focused), the focus is checked again
  (`focus_rechecks`). The field found active must still be the active one when the
  commit is written: if another field took over (a dialog opened, focus moved),
  the request fails with `target_lost` (exit 6), reason `context_changed`, and
  nothing is sent.
- **One effect.** The commit is a single message. Once any of it may have been
  written, failure, timeout or cancellation report outcome `unknown` with
  `partial_result` (`method`, `bytes`, `commit_sent`), and nothing is retried
  automatically: check the application before sending it again. Cancellation
  before the message was written withdraws it. As for keys, the request is
  refused with `input_uncertain` while an earlier release is unconfirmed.
- **`confirmed`.** After the commit the worker waits up to 250ms for the field to
  report its text (`surrounding_text`); reports received later never count. `confirmed: true` only if the field's last
  report before the commit (newer than the field's activation) and a later report
  from the same field, with no deactivation in between, differ by exactly the
  commit: the selection (anchor to cursor, in bytes) replaced by the text and the
  cursor right after it. Otherwise `confirmed: false` with `confirmation_reason`:

  | Reason | Meaning |
  | --- | --- |
  | `no_surrounding_text` | The field reports empty text (Blender does). |
  | `no_fresh_snapshot` | The field hadn't reported its text since it was activated, so there is nothing to compare with. |
  | `truncated` | A report is near the 4000-byte cap, so it may be a window into a longer text. |
  | `context_changed` | The field was deactivated or replaced (focus moved) before a matching report. |
  | `mismatch` | The field's text changed in some other way, or the toolkit reported a different window of it. |
  | `timeout` | No report arrived within the wait. |

  `confirmed: false` is not a failure and does not mean nothing was typed: the
  commit was delivered (`dispatched: true`). Check the application (screenshot,
  title, saved file) and do not resend blindly. GTK reports only part of a
  multi-line text (lines around the cursor, and just the current line after a
  commit), so in a multi-line GTK editor `mismatch` is common even when the text
  arrived; in a single-line field it is usually confirmed.
- **Health.** `session status` reports `health.input_method`: `state` `passed`
  (bound), `unavailable` (KWin doesn't advertise `zwp_input_method_v1`; `reason`
  `not_advertised`) or `failed` (connect or set-up error), and `advertised`. Its
  absence never fails the session; only non-ASCII `type` needs it. `doctor` runs
  no compositor, so it reports `input_method: not_tested`. KWin 6.7.5 advertises
  it in the private session without configuration. If a KWin build ever needs one,
  the untested fallback is a private `kwinrc` with `[Wayland] InputMethod=` set
  before KWin starts.
- **One input method.** The worker binds when the session starts and keeps that
  connection: KWin sends a field's text only when the application next updates it,
  never replayed to a later client, so a per-request connection would rarely have
  anything to confirm against. A lost connection is reopened by the next
  non-ASCII `type` (its confirmation then usually reports `no_fresh_snapshot`).
  Running another input method (an IME such as fcitx5 or ibus) inside the private
  session is unsupported: every bound input method gets the field's context, a
  commit clears any preedit text (KWin 6.7.5), and an application under test that
  is itself an input method gives undefined results. Preedit, compose and paste
  are out of scope.
- **Privacy.** The worker keeps only the field's latest report, in memory. Neither
  that text nor the typed text is written to request records, events or logs:
  results and records carry lengths and whether they matched.

**Release guarantees.** The worker owns every hold. Client disconnect, Ctrl-C,
timeout, cancellation and `session stop` release held keys immediately on the owner
thread, and shutdown releases again as a backstop (`shutdown.json` stage `release`).
If a release cannot be confirmed, later `key`/`type` fail with `input_uncertain`
and `session stop` still works. Losing the input device fails the session.

**Recovery is `session stop` then `session start`** (about a second). There is no
`input reset`: the only paths to an unconfirmed release (device pause, removal or
disconnect) already fail the session, and a replacement connection cannot release
keys for the old one, because KWin ignores releases for keys another connection
pressed. Dropping a connection while keys are held is also unsafe: KWin 6.7.5
crashes (SIGSEGV) when an EIS client disconnects while holding keys on a focused
surface (3 of 3 attempts; releasing first was safe). The worker therefore always
releases before it closes the connection, and stop does the same.

**Results.** `dispatched: true` means the events reached the compositor for the
focused window, not that the application handled them. Check with a screenshot or
window query. Results also include the window, the query artifact used for the
focus check, timing and either `codes` and `hold` (key) or `method` and
`characters` (type; keys add `strokes`, the input method `bytes`, `confirmed` and
`confirmation_reason`).

## Public `click`

```sh
agent-desktop --json click --window REF --x 107 --y 23 [--button left|right|middle] [--count 1-3]
agent-desktop --json click --x 640 --y 360    # screen coordinates, no window checks
```

**Coordinates.** With `--window`, `x`/`y` are integer pixels from the top-left
of the window's **client area**, the same space as `screenshot --window`, so a
pixel read from a window screenshot can be clicked directly when the window is
fully on screen (see [Screenshots](CLI.md#screenshots) for clipped windows). A GTK header bar is
client content (gnome-text-editor's "New Tab" button is at about 107,23). A KWin
title bar, which Qt/KDE apps get, is not. The point must be inside the client area
(`invalid_arguments`, reason `outside_window`) and on screen (`outside_screen`). It
is mapped to the screen from the geometry of the same window query that checks
focus. Without `--window`, `x`/`y` are screen coordinates (0–1279, 0–719) and
nothing about windows or focus is checked: whatever is at that point gets the click.

**Focus first.** With `--window`, the window must be active, as for `key`. Use
`focus` first. A click there can't land on a window that is covering the target,
except for windows KWin keeps above the active one. While KWin's window menu (a
`compositor` row) is open, `click --window` fails with reason
`compositor_surface_open`, as `key` does.

**Clicks.** The pointer moves to the point once, then each click is a 20ms press
and release, with 60ms between the clicks of a double or triple click (well
inside toolkit double-click times). The pointer stays there afterwards, so a
later screenshot may show hover effects or a tooltip. A tooltip is listed by
`windows` as a `popup` row of the app; `--app` selection ignores it.

**Release and results.** Buttons are in the same release ledger as keys, with the
same guarantees and `input_uncertain` behavior. Results give `x`, `y`,
`screen_x`, `screen_y`, `button`, `count` and, with `--window`, `window`, `client`
and the query artifact. `dispatched: true` means the events reached the
compositor, not that the application acted on them.

## Public `move`

```sh
agent-desktop --json move --window REF --x 107 --y 23
agent-desktop --json move --x 640 --y 360    # screen coordinates, no window checks
```

Moves the pointer to one point and presses nothing: hover. The coordinates and
every check are the same as for `click`. With `--window`, the point is in client
coordinates and must be inside the client area and on screen, and the window must
be active and free of KWin surfaces (`target_lost`, reason `focus_lost` or
`compositor_surface_open`; outcome `not_started`). Hover needs focus for the same
reason as a click: pointer events go to whatever surface is under the pointer, and
only the active window is on top, so this is what makes sure the hover lands on the
window you named. Without `--window`, nothing about windows is checked.

The pointer stays at the point, `screen_x`, `screen_y` in the result, until the
next `click`, `move` or `scroll`. A new session's pointer starts at the screen
center (640, 360). Toolkits show hover highlights at once and tooltips after their
own delay (often 0.5–1s), so wait before taking the screenshot. A tooltip is listed
by `windows` as a `popup` row of the app. A move to the point the pointer is already
at reaches the application as an empty pointer frame with no motion, so it does not
restart a tooltip delay; move away and back for that.

The result has click's fields without `button` and `count`: `window`, `focused`,
`dispatched`, `query_artifact`, `client`, `x`, `y`, `screen_x`, `screen_y`,
`focus_rechecks` and timing. A failure after the motion was attempted has outcome
`unknown` and `pointer_moved` in its context. The motion opens a libei emulation
sequence that is closed before the result, so nothing stays in progress.

## Public `scroll`

```sh
agent-desktop --json scroll --window REF --x 350 --y 300 --dy 3     # 3 wheel notches down
agent-desktop --json scroll --window REF --x 350 --y 300 --dy -3    # 3 notches up
agent-desktop --json scroll --x 640 --y 360 --dx 2                  # screen point, 2 notches right
```

**Steps and signs.** `--dy` and `--dx` are mouse-wheel notches: integers from -50
to 50, default 0, and not both 0 (`invalid_arguments`, reason `zero_scroll`).
Positive `--dy` scrolls down, toward the end of a document, so the content moves up.
Negative `--dy` scrolls up. Positive `--dx` scrolls right and negative `--dx` left.
This is a wheel without "natural scrolling", and Wayland's own sign. It was checked
live: `--dy 3` moved gnome-text-editor's text up and `--dy -3` brought it back,
and the fixture receives positive values for down and right. How far a notch goes
is up to the application. GTK 4 scrolls page height^(2/3) pixels, about 60px or
2.5 lines in a 520px-tall gnome-text-editor. Other toolkits use their own amounts.

**Where it goes.** The pointer first moves to the point, with the same coordinate
and window rules as `click` and `move`, because a wheel scrolls whatever is under
the pointer, not the focused widget. Then one notch goes out every 20ms, a brisk
wheel spin. A diagonal scroll steps both axes together until the shorter one is
done, so `--dx 1 --dy 3` sends (1, 1), (0, 1) and (0, 1).

**What is sent.** Each step is one libei discrete scroll of 120 units per notch,
followed by a frame. KWin passes it on as one wheel event of `wl_pointer.axis_value120`
±120, `axis` ±15 and `frame`, with no `axis_source`; horizontal comes first when
there are both. Clients bound below wl_seat version 8 get `axis_discrete` ±1 instead
of value120. No scroll stop is ever sent. A physical wheel sends none, and KWin
would deliver it as `axis_stop`, which toolkits treat as the end of a touchpad
fling. Nothing is held between steps, so an interrupted scroll leaves nothing in
progress. The emulation sequence the motion opened is closed after the last step,
and at once on any failure, Ctrl-C, timeout or `session stop`. The shutdown backstop
closes it too.

**Focus rechecks and progress.** As for `type`, a `--window` scroll queries the
window again every 250ms while steps remain. A lost focus or an opened KWin surface
stops it between steps with `target_lost`, outcome `unknown`, and the progress so
far: `steps_sent`, `steps_total`, `dx_sent`, `dy_sent`, `pointer_moved` and
`focus_rechecks`. Any other failure after the first emission reports the same fields.
`steps_sent` counts the steps that were sent, which the failure test checks against
the fixture's receipts. Detection has the same delay as for typing: typically
0.25–0.35s of steps (10–15), at worst about 0.75s, can reach whatever is under the
pointer after focus moves. In the failure test, 11 steps reached the window that
took focus, which KWin had raised under the pointer.
Ctrl-C gives the CLI's own `cancelled` result, which carries no progress.

**Budget.** The estimate is 30ms per step plus the motion, so 50 steps (about 1.3s
to send) fit the default and maximum 3s. A scroll that does not fit fails with
`timeout`, phase `budget`, and sends nothing. For longer scrolls, send several
requests.

**Results.** Click's fields without `button` and `count`, plus `dx`, `dy` and
`steps`, the number of wheel events: the larger of |dx| and |dy|. The pointer stays
at `screen_x`, `screen_y`. `dispatched: true` means the steps reached the compositor.
Scrolling over something that does not scroll, or over no surface at all in screen
coordinates, still succeeds, so check with a screenshot.

## Public `drag` and `--modifiers`

```sh
agent-desktop --json drag --window REF --from 140,190 --to 280,120 [--button left|middle|right] [--duration 0-2000] [--modifiers ctrl,shift,alt]
agent-desktop --json click --window REF --x 10 --y 20 --modifiers ctrl          # ctrl+click
agent-desktop --json scroll --window REF --x 350 --y 300 --dy -3 --modifiers ctrl # ctrl+wheel
```

**Order.** The pointer moves to `--from` (or the click or scroll point) with
nothing held. Then the modifiers go down in the order given, 20ms before the first
button press or wheel step; then the button, the motion and the release; then the
modifiers come up in reverse order 20ms later. A multi-click holds the modifiers
across all its clicks. The fixture's receipts in the smoke test check this order
and the XKB modifier mask on every pointer event.

**Drag motion.** One absolute motion every 10ms along a straight line, with
fractional positions, ending exactly at `--to`: `ceil(duration / 10ms)` steps,
at most 200 and at most the larger of the x and y distance in pixels, at least
one. A step that falls behind is sent as soon as the owner can, never skipped,
so an application that paints along the path gets every point. The button comes
up 20ms after the last motion, at `--to`. While the button is down, Wayland's
implicit grab sends the motion to the surface where the press landed even if
the pointer leaves it. `--duration` is 0–2000ms (default 300); 0 is a single
motion. Both ends must be in the client area and on screen (`outside_window`,
`outside_screen`, with `field`), and they must differ (`zero_drag`). `drag`
always needs `--window`, as do `--modifiers` on `click` and `scroll`
(`modifiers_need_window`), so focus is checked while anything is held.

**Focus rechecks, interruption and progress.** As for `scroll`, the window is
queried every 250ms while the drag runs. Focus loss, a KWin surface, Ctrl-C,
timeout or `session stop` stops the motion between steps and releases the button
first, then the modifiers, in the same owner turn. A failure after the first
emission reports `pointer_moved`, `button_pressed`, `steps_sent`, `steps_total`,
`button_released` and `focus_rechecks`. If the pointer device is paused or
removed mid-drag, the button's release cannot be confirmed: the session fails,
the modifiers are still released on the keyboard, and input stays
`input_uncertain` until the session restarts. The failure test interrupts a 2s
`drag --modifiers ctrl,shift` all three ways and checks that the fixture saw the
button, shift and ctrl come up and no motion after.

**Budget.** A drag's estimate is its duration plus the focus checks and gaps
(about 0.3s more); the 2000ms maximum fits the 3s timeout. A drag that does not fit
fails with `timeout`, phase `budget`, before anything is sent.

**Results.** `from`, `to`, `screen_from`, `screen_to`, `screen_x`/`screen_y` (the
end, where the pointer stays), `button`, `duration` in ms, `steps`, `modifiers`
and the window fields of `click`. `click` and `scroll` add `modifiers` when given.

**Modifiers and `key`.** Modifiers are `ctrl`, `shift` and `alt` (the left keys),
distinct, in any order. `move` takes none, since nothing is pressed. `key` takes
none either: its chord (`ctrl+shift+a`) already presses the modifiers first and
releases them last.

# Private input connection

The worker owns one persistent `input_connection.Input` on its GLib thread. Its
asynchronous EIS negotiation uses the explicitly created private D-Bus connection;
that bus remains retained for the connection lifetime. Host endpoint discovery
and fallback are absent. It asks KWin for keyboard and pointer devices (EIS
request flags 3) and binds the keyboard, absolute-pointer and button
capabilities, plus scroll when the seat offers it. KWin then offers a keyboard
device and a separate absolute device (absolute motion, button, scroll) with one
region per output; relative-pointer and touch devices are never bound or
referenced. The startup gate requires CONNECT and one resumed keyboard, within
the existing shared startup deadline and a three-second input limit. The pointer
device is checked when `click`, `move`, `scroll` or `drag` uses it (`input_unavailable` if
it is missing, or for `scroll` if it lacks the scroll capability). Public `key`,
`type`, `click`, `move`, `scroll` and `drag` use this same connection (above). It is never replaced within a session; see recovery above.

The limited ctypes declarations in `libei_binding.py` accept any x86_64 libei
1.x (soname `libei.so.1`) that exports every declared symbol. 1.6.0 is the tested
version, and `doctor` warns on others. The M1 compiler audit checked this table
against the installed headers, including void dispatch and variadic promoted enums,
and `tests/test_libei_probe.py` repeats that check for the current table, including
`ei_device_scroll_discrete(device, int32_t, int32_t)`, when the headers are installed.
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
most 32 distinct evdev codes (BTN_LEFT/RIGHT/MIDDLE for the pointer device)
before emission and records attempted presses before native calls. Absolute
motion must fall inside one of the pointer device's regions. When a press, motion
or wheel step starts, what is held must be exactly what its caller says it holds,
and otherwise nothing: pointer input may name `modifiers`, the keyboard batch of
ctrl, shift and alt it pressed earlier, in press order, and a motion may also name
the one `button` its drag is holding. Anything else held, anything held on a lost
device, or any uncertainty refuses with `input_unavailable`, so input still never
stacks on a press it does not own; only pointer presses may name modifiers, and a
wheel step never runs with a button down. A wheel step is one
notch (-1, 0 or 1) per axis, sent as `ei_device_scroll_discrete` with 120 units
per notch and a frame; it holds nothing. It is internal: the public
`key`/`type`/`click`/`move`/`scroll`/`drag` tasks enforce finite holds and focus checks. Release uses explicit release events and a frame,
and a motion or wheel step with nothing held is closed with `stop_emulating`.
Release goes through the pointer device before the keyboard, so a button always
comes up before the modifiers it was pressed under. If one device's release fails,
input becomes uncertain and the other device is still released before the failure
is raised. `release(kind)` releases one device kind only, which a modifier click
uses between its clicks.

Held state becomes uncertain after lifecycle loss or an emission failure.
RESUMED does not clear uncertainty, and disposal preserves uncertain held
history. Nothing clears that gate; a new session does. The adapter does not
replay input or claim generic application acknowledgment. The known KWin pause key-ledger behavior remains covered by the
[M1 decision](M1_DECISION.md).

Runtime input capability loss fails and stops the worker's owned session.
Callback/protocol errors remain sticky and are checked before ordinary work and
worker heartbeats.

The [issue #27 evidence](https://github.com/JosephWest2/kde_agent/blob/d1efe95b/evidence/issue-27/README.md) records installed production
async negotiation and real compositor lifecycle faults. The test harness uses
the existing strictly private pause plugin and its `issue12` name selector;
production uses the `agent-desktop` prefix. Fault control is not a product API.

Canceling pending negotiation guarantees local callback/FD cleanup, not confirmed
server context destruction while the originating bus stays open. Startup failure
and worker stop close that bus. Recovery from unknown negotiation completion on
a reused bus does not arise: the bus is never reused for a second negotiation.
