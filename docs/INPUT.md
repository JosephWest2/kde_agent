# Keyboard and pointer input

## Public `key` and `type`

```sh
agent-desktop --json key  --window REF ctrl+shift+t [--hold 0.05]
agent-desktop --json type --window REF 'Hello, World!' [--timeout 30]
```

**Focus first.** Both commands query the window before the first stroke and
require it to exist and be focused. Otherwise they fail with `target_lost` (reason
`focus_lost`) and send nothing. Use `focus` first.

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

**Text.** `type` maps printable ASCII, space, newline (Return) and tab (Tab) to the
US layout, adding Shift where needed. Any other character rejects the whole request
before anything is sent, with its `index` and `codepoint` (non-ASCII lookalikes
such as the Kelvin sign included). If `key caps_lock` has turned Caps Lock on,
`type` inverts Shift for letters so the text still comes out as written; only this
toolkit sends input to the private desktop, so the worker tracks that state. Each character is one
press and one release, about 10ms apart, so roughly 170 characters fit the default
3s and about 1900 fit the 30s maximum. Text whose estimate (15ms per character,
plus 0.25s for a final focus recheck when it takes longer than 250ms) does not fit
the remaining time fails with `timeout`, phase `budget`, and sends nothing.

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
focus check, timing and either `codes` and `hold` (key) or `characters` (type).

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
except for windows KWin keeps above the active one.

**Clicks.** The pointer moves to the point once, then each click is a 20ms press
and release, with 60ms between the clicks of a double or triple click (well
inside toolkit double-click times). The pointer stays there afterwards, so a
later screenshot may show hover effects or a tooltip.

**Release and results.** Buttons are in the same release ledger as keys, with the
same guarantees and `input_uncertain` behavior. Results give `x`, `y`,
`screen_x`, `screen_y`, `button`, `count` and, with `--window`, `window`, `client`
and the query artifact. `dispatched: true` means the events reached the
compositor, not that the application acted on them.

# Private input connection

The worker owns one persistent `input_connection.Input` on its GLib thread. Its
asynchronous EIS negotiation uses the explicitly created private D-Bus connection;
that bus remains retained for the connection lifetime. Host endpoint discovery
and fallback are absent. It asks KWin for keyboard and pointer devices (EIS
request flags 3) and binds the keyboard, absolute-pointer and button
capabilities. KWin then offers a keyboard device and a separate absolute device
(absolute motion, button, scroll) with one region per output; relative-pointer
and touch devices are never bound or referenced. The startup gate requires CONNECT
and one resumed keyboard, within the existing shared startup deadline and a
three-second input limit. The pointer device is checked when `click` uses it
(`input_unavailable` if it is missing). Public `key`, `type` and `click` use this
same connection (above). It is never replaced within a session; see recovery above.

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
most 32 distinct evdev codes (BTN_LEFT/RIGHT/MIDDLE for the pointer device)
before emission and records attempted presses before native calls. Absolute
motion must fall inside one of the pointer device's regions, and nothing may be
held on any device when a press or motion starts. It is internal: the public
`key`/`type`/`click` tasks enforce finite holds and focus checks. Release uses explicit release events and a frame.

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
