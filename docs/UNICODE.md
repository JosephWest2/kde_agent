# Non-ASCII text entry (spike #84)

`type` sends printable ASCII, newline and tab as key events on a fixed US layout
([INPUT.md](INPUT.md)). This spike measured three ways to enter other text in the
private session: an input-method commit, a clipboard paste, and keymap tricks. It
used GTK3, GTK4 and Qt6 clients.

**Decision: go ahead with an input-method commit through KWin's
`zwp_input_method_v1`; follow-up [#102](https://github.com/JosephWest2/kde_agent/issues/102),
size medium.**
- **Why.** It delivered exact text, including combining marks, CJK, emoji, newline
  and tab, to every text-input client checked byte for byte: GTK3 and GTK4 (probes,
  gnome-text-editor), Qt6 (probe) and Dolphin (KF6). Blender 5.2 was checked only
  visually.
  - It took under 1ms (median) and needed no KWin configuration.
  - It is the only approach that tells `type` beforehand whether the text can
    land (an active context). It also gives some evidence afterwards
    (`surrounding_text`), but that evidence is an observation, not proof (below).
- **Clipboard paste works too,** but it overwrites the clipboard and depends on
  each application's paste shortcut. It gives no confirmation, so it is not the
  path for `type`.
- **Keymap approaches are a no-go.** A live layout change does reach KWin and its
  EIS keymap, but it replaces the input device the worker holds, which today fails
  the session. A layout covers only its own characters. Compose and GTK's
  ctrl+shift+u depend on the toolkit and on an environment override, and cover
  only part of Unicode.

## Method

- **Environment.**
  - KWin 6.7.5, the CLI's own private session (`session start`) and Linux 7.2.6.
  - gnome-text-editor 50.1 (GTK 4.22), Dolphin (Qt 6.11.2, KF6) and Blender 5.2.2.
  - Small probes, each a text view that appends every change, with a
    `CLOCK_MONOTONIC` timestamp, to a JSON-lines file:
    - PySide6 6.11.2 `QPlainTextEdit` (Qt6);
    - PyGObject `Gtk.TextView` on GTK 4.22 and GTK 3.
- **No Qt6 text editor is installed.** kate, kwrite and konsole are absent.
  Dolphin's inline rename (F2, a Qt `QLineEdit`) was used instead, checked by
  the file name on disk.
- **Helpers ran inside the session through `launch`, so they had its private
  environment.**
  - A session allows one launched application at a time, so each run launched a
    small Python sidecar. The sidecar started the target application and ran
    helper commands (wl-copy, the input-method client) for it.
  - A product implementation cannot use `launch` for this. It needs a client owned
    by the worker (see also #86).
- **The prototypes stayed out of the repository.**
  - The input-method client was about 150 lines of C, using `wayland-scanner` on
    `input-method-unstable-v1.xml`.
  - The keymap probes were Python with libei through ctypes and Gio.
- **The key strings were mixed.** For example `héllo wörld ✓ 中文 🎉 Ωμέγα — “quotes”
  é`, where the last character is `e` plus U+0301 (combining).
- **Scope of changes.** The helpers used only the private session's endpoints
  (its Wayland display, bus and clipboard). Nothing was done to the user's desktop,
  clipboard or configuration. The only configuration written was a `kxkbrc` in the
  private session's own `XDG_CONFIG_HOME` and in a nested compositor's directory
  under the session's `TMPDIR` (below).
- **Session starts.** The spike made 6 `session start` calls. 1 failed at start-up
  readiness: `Window query deadline expired` (component `window_query`), on the
  first, cold start. That was before any spike helper ran, and unrelated to the
  input method. A second session failed later, on purpose, when the live layout
  change below replaced its input device.

## Results

| Approach | GTK4 (probe, gnome-text-editor) | GTK3 (probe) | Qt6 (probe, Dolphin) | Blender 5.2 | Fixture (no text input) | Latency |
| --- | --- | --- | --- | --- | --- | --- |
| Input-method commit (`zwp_input_method_v1`) | probe 20/20 exact; editor 5/5 saved files exact | 10/10 exact | probe 20/20 exact; Dolphin 3/3 file names exact | 1/1, visual only (screenshot), not byte-verified | no context: commit refused, detectable | commit to text: median 0.3ms (GTK4), 0.7ms (Qt6), max 32ms; fresh connection active 1.3–5ms after it starts |
| Clipboard: `wl-copy` (ext-data-control) + `key ctrl+v` | probe 20/20 exact; editor 5/5 saved files exact | 10/10 exact | probe 20/20 exact; Dolphin 3/3 file names exact | not tried | n/a | `wl-copy` 20ms; text appears 107–112ms (median) after the `key` CLI starts, which includes CLI start-up and is about the same as any `key` call |
| GTK hex entry: ctrl+shift+u, hex digits, space | 0/5 by default; 5/5 with `GTK_IM_MODULE=gtk-im-context-simple` | 5/5 | 0/5 (no such feature) | n/a | n/a | about 0.5s per character through the CLI (3 requests) |
| Compose (`compose:ralt` in a private `kxkbrc`, in a nested KWin) | fails by default; works with `GTK_IM_MODULE=gtk-im-context-simple` | not tried | works (`é – ©`) | not tried | n/a | 3–4 strokes per character |
| Changing the keymap of a running KWin | a real KConfig change notification changes the layout and the EIS keymap; it also replaced the worker's input device, which failed the session | | | | | |

Combining characters, emoji (outside the BMP) and CJK all arrived exactly by
commit and by paste, wherever the result was checked byte for byte: in the probes'
text, saved files and file names. For GTK4 that was 20/20 in the probe and 5/5 in
gnome-text-editor. Each editor run saved a file through the Save dialog, and the
file matched byte for byte. Each held an input-method line, a pasted line and an
ASCII line typed with `type`. The Blender result is only what a screenshot shows.

## 1. Input-method commit

**What KWin offers.** The private KWin advertises:
- `zwp_input_method_v1` v1 and `zwp_input_panel_v1`;
- `zwp_text_input_manager_v2` and `zwp_text_input_manager_v3`.

It does not advertise `zwp_input_method_v2` (wlroots) or `zwp_virtual_keyboard_v1`,
so `wtype` cannot work. `org.kde.kwin.VirtualKeyboard` reports `available=false`
and `active=false`, and `kwinrc` has no `InputMethod`.

**Nothing has to be enabled.** Any client in the session can bind
`zwp_input_method_v1`. KWin sends it `activate` with a context as soon as the
focused surface enables text input, immediately on bind if a text field already has
focus. The client then calls `commit_string(serial, text)` with the serial from the
last `commit_state`; serial 0 before any `commit_state` was also accepted.
- **When toolkits activate.** GTK and Qt activate when an editable widget has
  focus. Blender activates only while a text field is being edited, and the
  context goes away when editing ends.
- **Key input still works.** Key events from libei still reach the application
  unchanged. `ctrl+a`, `ctrl+v`, `type '\n'` and ASCII text all worked with the
  input method bound, and no panel or other surface appeared. That is all that was
  measured, with no other input method present.
- **Every bound client gets the context.** Two clients bound at once both got
  `activate`, and both commits landed.
- **Another input method would be disturbed.** An application under test that is
  itself an input method (ibus, fcitx, plasma-keyboard) shares the field. KWin
  6.7.5's commit handling also clears preedit (this comes from the KWin source and
  was not measured here). A commit from `type` can therefore discard another input
  method's composition in progress. A user input method running at the same time
  in the private session is not supported.

**What the application sends back.**
- **GTK3, GTK4 and Qt6** send `surrounding_text` with the cursor right after each
  commit. In the probes, the committed text appeared before the cursor 0.4–14ms
  after the commit; gnome-text-editor reported the new length after 15ms.
- **Blender** sends empty surrounding text.
- **This is evidence, not proof.**
  - **Pre-existing text.** A check that the text before the cursor ends with the
    string can match text the field already had.
  - **No request id.** Nothing ties a `surrounding_text` update to a commit, so an
    unrelated edit or a focus change can produce or spoil a match.
  - **Optional and truncated.** Surrounding text is optional, and text-input-v3
    limits it to 4000 bytes.
  - **Byte offsets.** Cursor and anchor are byte offsets into UTF-8.

  `confirmed` must therefore be conservative. It is true only when a fresh snapshot
  from before the commit and one from after it, for the same context with no
  `deactivate` between them, differ by exactly the committed text at the selection
  or cursor. Otherwise it is false, with a reason. `confirmed: false` never means
  nothing was sent: once `commit_string` is flushed, the request has had an
  effect. A missing confirmation never authorizes a retry, since input is never
  retried automatically.
- **Newline and tab** in a commit were inserted as characters by both probes. They
  are not Return or Tab key events, so they don't press a default button or
  trigger an editor's auto-indent.

**`keysym` requests on the context** (an alternative to `commit_string`) delivered
`a`, `é` and `中` in both probes but dropped 🎉 (outside the BMP). `commit_string`
is the one to use.

**Size limit.** A Wayland message's size is a 16-bit field, and libwayland caps
messages at 4096 bytes. A 4,000-byte commit landed in both probes. At 4,084 bytes
the message was 4,104 bytes, and libwayland failed the connection (`message length
4104 exceeds 4096`): nothing landed, and the input method's connection was lost.
text-input-v3 also limits a commit to 4000 bytes. CLI requests can be up to 1 MiB,
so `type` needs a UTF-8 byte limit for input-method text, checked before anything
is sent, or bounded chunking with progress and cancellation.

**Failure modes.**
- **No context, so the text cannot be delivered.** This happens with no focused
  text field, a client without text input (the native fixture, X11 clients), or
  Blender outside a text field. `type` can check for this first and refuse
  without sending anything.
- **A KWin update could restrict the global** to the configured input method. The
  fallback is to name a helper in a private `kwinrc` `[Wayland] InputMethod=`
  before KWin starts, so that KWin launches it; this fallback was not tested.
  `doctor` should report whether the global is advertised.
- **Mixing keys and commits in one request** goes over two sockets (EIS and
  Wayland) with no ordering between them. The follow-up therefore sends text with
  non-ASCII characters as a single commit.
- **v1 has no destroy request** for `zwp_input_method_v1`. Unbinding means closing
  the Wayland connection.

**Data handling.** The client connects only to the private compositor. It
receives the focused field's surrounding text, which can be sensitive application
data, so that text must not be written to artifacts. This is desktop separation,
not a sandbox ([ARTIFACTS.md](ARTIFACTS.md)).

## 2. Clipboard paste

**Setting the clipboard.** `wl-copy` (wl-clipboard 2.3.0) inside the session set
the clipboard through `ext_data_control_manager_v1`, which the private KWin
advertises (there is no wlr data control). `WAYLAND_DEBUG` showed it creating no
surface, so it needed no focus: the target stayed focused, and `wl-paste` in the
session read the text back exactly.
`key ctrl+v` then pasted it into each target, with the results in the table above.

**Clipboard state.**
- **The helpers used the private clipboard.** `wl-copy` and `wl-paste` connected
  through the session's own `WAYLAND_DISPLAY` and runtime directory, so they set
  and read the private KWin's clipboard. The user's clipboard was not read or
  written. The private environment is desktop separation, not a sandbox
  ([ARTIFACTS.md](ARTIFACTS.md)). A program in the session that deliberately
  connected elsewhere is not prevented from doing so.
- **The private clipboard keeps the text afterwards.** `wl-copy`'s forked server
  owns the selection until something else replaces it (here, until the
  application was killed, since it was in the application's cgroup). Whatever the
  application under test had copied is overwritten. Restoring the previous
  contents would race with the application's asynchronous read of the paste.

**Semantics.**
- **The paste shortcut depends on the application:** `ctrl+v` in GTK, Qt and
  Blender text fields; `ctrl+shift+v` in terminals (none installed to test);
  something else, or nothing, elsewhere.
- **The paste needs the right widget focused,** and it isn't delivered as typing.
  Some fields filter or transform pasted text, and rich-text editors may prefer
  another MIME type (`wl-copy` offers only `text/plain` types).
- **`type` cannot tell whether a paste happened.** No feedback comes back from the
  application.

A product version would need a data-control source owned by the worker, because
a helper can't run beside the application under `launch`. It might make a
separate `paste` command, not a `type` method.

## 3. Keymap approaches

**What libei gets.** The EIS keyboard device's keymap is assigned by KWin. It is
`pc+us+inet(evdev)`, 487 keys, with no `Multi_key`, no dead keys and `<RALT>` as
`Alt_R`. A libei sender cannot replace it. The project's libei binding doesn't
read it at all, because `keymap.py` hard-codes US key codes.

**The private `kxkbrc` can set the layout, at start-up or live.**
- **First attempts did nothing.** These were:
  - writing `[Layout] LayoutList=de` with `Options=compose:ralt` straight into the
    private session's `XDG_CONFIG_HOME/kxkbrc`, then sending the `org.kde.keyboard
    /Layouts reloadConfig` signal and calling `org.kde.KWin.reconfigure`;
  - `kwriteconfig6 --notify` of values the file already held, which writes
    nothing and so sends no notification.

  None of them changed the layout (`getLayoutsList` still said `us`) or the keymap
  a new EIS device received.
- **A real KConfig change works live.** KWin 6.7.5 watches `kxkbrc` through
  `KConfigWatcher`. `kwriteconfig6 --notify` with changed values sends
  `org.kde.kconfig.notify` `ConfigChanged` on the private bus.
  - **In a nested headless KWin** (below), started with no `kxkbrc`, the layout
    became `de`. A new EIS device then received
    `pc_de_inet(evdev)_compose(ralt)` instead of `pc_us_inet(evdev)`.
  - **In the CLI's session** the same change replaced the EIS device the worker
    held. The session failed with `Resumed input capability was lost` (component
    `input_resumed`).
- **A file present at start-up works too.** A nested headless `kwin_wayland
  --virtual`, with its own `dbus-run-session` and config, started inside the
  private session with that `kxkbrc` already written. It used
  `pc_de_inet(evdev)_compose(ralt)` for both its layout and the EIS keymap, even
  with `XKB_DEFAULT_LAYOUT=us` in the environment.
- **What this would take.**
  - **Per session:** writing `kxkbrc` before KWin starts (a `desktop.py` change).
  - **Live:** the worker would have to survive and re-adopt a replaced input
    device, and `keymap.py` would have to read the device keymap instead of
    assuming US key codes.
  - **Either way,** only the characters of the chosen layout become typeable.

**Compose.**
- **Setup.** This used the nested KWin with `compose:ralt`, a probe as its only
  window, and libei strokes `RAlt ' e`, `RAlt - - .` and `RAlt o c`.
- **Qt6** produced `é – ©`, using the `en_US.UTF-8` compose table, which
  `compose.dir` maps to `C.UTF-8`.
- **GTK4's default Wayland input context** produced the raw keys (`'e--.oc`).
  `GTK_IM_MODULE=gtk-im-context-simple` made it compose correctly.
- **Compose covers only what the compose table lists.** That includes Latin
  accents and some symbols, but no CJK and almost no emoji.

**GTK hex entry (ctrl+shift+u, hex digits, space)** worked in GTK3 for `é 中 🎉 Ω
✓`. In GTK4 4.22 it worked only with `GTK_IM_MODULE=gtk-im-context-simple`. Qt has
no equivalent. It is GTK-only and costs about 0.5s per character through the CLI.
It also depends on an environment variable the agent sets at launch.

**Conclusion.** A layout change is possible, but each layout covers few
characters, and a live change disrupts the worker's input device. Compose and hex
entry differ by toolkit, and coverage is partial. None of these can tell `type`
whether the text arrived.

## Comparison

| | Input-method commit | Clipboard paste | Keymap / compose / hex entry |
| --- | --- | --- | --- |
| Works in | GTK3, GTK4, Qt6 (byte-verified); Blender (visual only) | GTK3, GTK4, Qt6 text fields; each app's own shortcut | Compose: Qt6, and GTK only with the simple input context. Hex: GTK3, and GTK4 only with the simple input context |
| Doesn't work in | X11 clients, the fixture, non-text widgets | non-text widgets, apps whose paste shortcut isn't `ctrl+v` | Qt (hex), default GTK4, anything outside the compose table |
| Latency | under 1ms (median) per commit | one `key` request plus about 20ms | 3+ strokes per character |
| Detecting success | Before sending: an active context. After: an observation only; a before/after `surrounding_text` diff (GTK, Qt), never proof that nothing was sent | none | none |
| Side effects | none seen without another input method; a commit can discard another input method's preedit | overwrites the private clipboard | a live layout change replaces the worker's input device; otherwise a layout chosen at start, or per-app environment |
| Data handling | private compositor endpoints; surrounding text must not be logged | private clipboard | private config |
| Limits | one commit at most 4000 bytes (libwayland message 4096) | none measured | layout coverage |
| Effort to productize | medium: a Wayland client in the worker, `type` routing, a fixture extension | medium: a data-control source in the worker plus a `paste` command | high for little coverage |
| How to verify | fixture `--text-input` (text-input-v3) logs exact commits; gnome-text-editor saved file; Dolphin rename | fixture that reads the selection on `ctrl+v`; saved file | fixture with a compose-aware keymap; not worth building |

## Decision

1. **Go ahead: input-method commit.** Follow-up
   [#102](https://github.com/JosephWest2/kde_agent/issues/102), size medium:
   - a Wayland input-method client owned by the worker;
   - `type` keeps the key path for text the US layout can type, and commits any
     other text whole, up to an upfront UTF-8 byte limit;
   - the request is refused before anything is sent when no context is active;
   - `confirmed` is a conservative observation from before and after snapshots,
     never a reason to retry;
   - a user input method running at the same time is not supported;
   - a `--text-input` mode for the fixture, plus smoke, failure-path and
     gnome-text-editor tests.
2. **Clipboard paste: not for `type`.** It stays possible as a separate `paste`
   command if a workflow needs it (parking lot, #87). It needs a data-control
   source owned by the worker.
3. **Keymap approaches: no-go for Unicode.** `type` keeps the fixed US layout. A
   per-session or live layout option is possible, but it would cover only that
   layout's characters. A live change also needs the worker to survive a
   replaced input device. Nothing has asked for one.
