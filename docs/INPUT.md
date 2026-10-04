# Keyboard input

## Public `key` and `type`

```sh
agent-desktop --json key  --window REF ctrl+shift+t [--hold 0.05]
agent-desktop --json type --window REF 'Hello, World!' [--timeout 30]
```

**Focus first.** Both commands query the window once and require it to exist and
be focused. Otherwise they fail with `target_lost` (reason `focus_lost`) and send
nothing. Use `focus` first. Focus is not rechecked mid-sequence (#67).

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
press and one release, about 10ms apart, so roughly 200 characters fit the default
3s and about 2000 fit the 30s maximum. Text whose estimate (15ms per character)
does not fit the remaining time fails with `timeout`, phase `budget`, and sends nothing.

**Release guarantees.** The worker owns every hold. Client disconnect, Ctrl-C,
timeout, cancellation and `session stop` release held keys immediately on the owner
thread, and shutdown releases again as a backstop (`shutdown.json` stage `release`).
If a release cannot be confirmed, later `key`/`type` fail with `input_uncertain`
and `session stop` still works. Losing the input device fails the session.

**Results.** `dispatched: true` means the events reached the compositor for the
focused window, not that the application handled them. Check with a screenshot or
window query. Results also include the window, the query artifact used for the
focus check, timing and either `codes` and `hold` (key) or `characters` (type).

# Private input connection

The worker owns one persistent `input_connection.Input` on its GLib thread. Its
asynchronous EIS negotiation uses the explicitly created private D-Bus connection;
that bus remains retained for the connection lifetime. Host endpoint discovery
and fallback are absent. The startup gate requires CONNECT and one resumed
keyboard, within the existing shared startup deadline and a three-second input
limit. Public `key` and `type` use this same connection (above); click and reset
are #66 and #67.

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
are checked before ordinary work and worker heartbeats.

The [issue #27 evidence](https://github.com/JosephWest2/kde_agent/blob/d1efe95b/evidence/issue-27/README.md) records installed production
async negotiation and real compositor lifecycle faults. The test harness uses
the existing strictly private pause plugin and its `issue12` name selector;
production uses the `agent-desktop` prefix. Fault control is not a product API.

Canceling pending negotiation guarantees local callback/FD cleanup, not confirmed
server context destruction while the originating bus stays open. Startup failure
and worker stop close that bus. Recovery from unknown negotiation completion on
a reused bus is deliberately left to the public reset policy in issue #31.
