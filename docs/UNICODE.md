# Non-ASCII text entry (spike #84)

`type` sends printable ASCII, newline and tab as key events on a fixed US layout
([INPUT.md](INPUT.md)). This spike measured three ways to enter other text in the
private session: an input-method commit, a clipboard paste, and keymap tricks. It
used GTK3, GTK4 and Qt6 clients.

**Decision: go ahead with an input-method commit through KWin's
`zwp_input_method_v1`; follow-up [#102](https://github.com/JosephWest2/kde_agent/issues/102),
size medium.**
- **Why.** It delivered exact text, including combining marks, CJK, emoji, newline
  and tab, to every toolkit that implements text input. That covered GTK3, GTK4,
  Qt6, Dolphin (KF6) and Blender 5.2.
  - It took under 1ms (median) and needed no KWin configuration.
  - It is the only approach that tells `type` whether the text can land (an
    active context) and whether it did (`surrounding_text`).
- **Clipboard paste works too,** but it overwrites the clipboard and depends on
  each application's paste shortcut. It gives no confirmation, so it is not the
  path for `type`.
- **Keymap approaches are a no-go.** The keymap is fixed when KWin starts.
  Compose and GTK's ctrl+shift+u depend on the toolkit and on an environment
  override, and cover only part of Unicode.

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
- **Isolation.** Nothing touched the user's desktop, clipboard or configuration. The
  only configuration written was a `kxkbrc` in the private session's own
  `XDG_CONFIG_HOME` and in a nested compositor's directory under the session's
  `TMPDIR` (below).

## Results

| Approach | GTK4 (probe, gnome-text-editor) | GTK3 (probe) | Qt6 (probe, Dolphin) | Blender 5.2 | Fixture (no text input) | Latency |
| --- | --- | --- | --- | --- | --- | --- |
| Input-method commit (`zwp_input_method_v1`) | probe 20/20 exact; editor 5/5 saved files exact | 10/10 exact | probe 20/20 exact; Dolphin 3/3 file names exact | works (screenshot) | no context: commit refused, detectable | commit to text: median 0.3ms (GTK4), 0.7ms (Qt6), max 32ms; fresh connection active 1.3–5ms after it starts |
| Clipboard: `wl-copy` (ext-data-control) + `key ctrl+v` | probe 20/20 exact; editor 5/5 saved files exact | 10/10 exact | probe 20/20 exact; Dolphin 3/3 file names exact | not tried | n/a | `wl-copy` 20ms; text appears 107–112ms (median) after the `key` CLI starts, which includes CLI start-up and is about the same as any `key` call |
| GTK hex entry: ctrl+shift+u, hex digits, space | 0/5 by default; 5/5 with `GTK_IM_MODULE=gtk-im-context-simple` | 5/5 | 0/5 (no such feature) | n/a | n/a | about 0.5s per character through the CLI (3 requests) |
| Compose (`compose:ralt` in a private `kxkbrc` read at KWin start) | fails by default; works with `GTK_IM_MODULE=gtk-im-context-simple` | not tried | works (`é – ©`) | not tried | n/a | 3–4 strokes per character |
| Changing the keymap of a running KWin | not possible: `reloadConfig` and `kwriteconfig6 --notify` left the layout and the EIS keymap unchanged | | | | | |

Combining characters, emoji (outside the BMP) and CJK all arrived exactly by
commit and by paste. GTK4 counts were the same in the probe and in
gnome-text-editor, whose saved file (written through its Save dialog) matched byte
for byte. It held an input-method line, a pasted line and an ASCII line typed with
`type`.

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
- **No interference.** Key events from libei still reach the application
  unchanged. `ctrl+a`, `ctrl+v`, `type '\n'` and ASCII text all worked with the
  input method bound. No panel or other surface appears.
- **Every bound client gets the context.** Two clients bound at once both got
  `activate`, and both commits landed. An application under test that is itself
  an input method (ibus, fcitx, plasma-keyboard) would share the field with
  `type`.

**What the application sends back.**
- **GTK3, GTK4 and Qt6** send `surrounding_text` with the cursor right after each
  commit. In the probes, the committed text appeared before the cursor 0.4–14ms
  after the commit; gnome-text-editor reported the new length after 15ms. So
  `type` can confirm that the text landed, by checking that the text before the
  cursor ends with it.
- **Blender** sends empty surrounding text, so a commit there can only be
  reported as sent.
- **Newline and tab** in a commit were inserted as characters by both probes. They
  are not Return or Tab key events, so they don't press a default button or
  trigger an editor's auto-indent.

**`keysym` requests on the context** (an alternative to `commit_string`) delivered
`a`, `é` and `中` in both probes but dropped 🎉 (outside the BMP). `commit_string`
is the one to use.

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

**Security and isolation.** The client exists only in the private compositor. It
receives the focused field's surrounding text, which can be sensitive application
data. That text must not be written to artifacts. The real desktop is not
involved.

## 2. Clipboard paste

**Setting the clipboard.** `wl-copy` (wl-clipboard 2.3.0) inside the session set
the clipboard through `ext_data_control_manager_v1`, which the private KWin
advertises (there is no wlr data control). `WAYLAND_DEBUG` showed it creating no
surface, so it needed no focus: the target stayed focused, and `wl-paste` in the
session read the text back exactly.
`key ctrl+v` then pasted it into each target, with the results in the table above.

**Clipboard state.**
- **Only the private clipboard changes.** The clipboard belongs to the private
  KWin, reached through the session's own `WAYLAND_DISPLAY` and runtime
  directory. Clients of the private compositor cannot reach the user's
  compositor or its Klipper. The user's clipboard was not read or written.
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

**KWin reads the private `kxkbrc` at start-up only.**
- **Live changes did not work.** Writing `[Layout] LayoutList=de` with
  `Options=compose:ralt` into the private session's `XDG_CONFIG_HOME/kxkbrc`
  changed nothing. Neither the `org.kde.keyboard /Layouts reloadConfig` signal,
  nor `kwriteconfig6 --notify`, nor `org.kde.KWin.reconfigure` on the private bus
  changed the layout (`getLayoutsList` still said `us`) or the keymap a new EIS
  device received.
- **A file present at start-up did work.** A nested headless `kwin_wayland
  --virtual`, with its own `dbus-run-session` and config, started inside the
  private session with that `kxkbrc` already written. It used
  `pc_de_inet(evdev)_compose(ralt)` for both its layout and the EIS keymap, even
  with `XKB_DEFAULT_LAYOUT=us` in the environment. Live changes to that nested
  KWin were ignored too.
- **So a session could choose a layout or options only before KWin starts.** That
  would be a `desktop.py` change, and the choice would be fixed for the session.

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

**Conclusion.** The keymap is fixed for the session, the results differ by
toolkit, and coverage is partial. None of these can tell `type` whether the text
arrived.

## Comparison

| | Input-method commit | Clipboard paste | Keymap / compose / hex entry |
| --- | --- | --- | --- |
| Works in | GTK3, GTK4, Qt6, Blender (any text-input-v3/v2 client) | GTK3, GTK4, Qt6 text fields; each app's own shortcut | Compose: Qt6, and GTK only with the simple input context. Hex: GTK3, and GTK4 only with the simple input context |
| Doesn't work in | X11 clients, the fixture, non-text widgets | non-text widgets, apps whose paste shortcut isn't `ctrl+v` | Qt (hex), default GTK4, anything outside the compose table |
| Latency | under 1ms (median) per commit | one `key` request plus about 20ms | 3+ strokes per character |
| Detecting success | Before sending: an active context. After: `surrounding_text` ends with the text (GTK, Qt) | none | none |
| Side effects | none seen; shares the field with another input method if the app under test is one | overwrites the private clipboard | needs a session-wide layout chosen at start, or per-app environment |
| Isolation | private compositor only; surrounding text must not be logged | private clipboard only | private config only |
| Effort to productize | medium: a Wayland client in the worker, `type` routing, a fixture extension | medium: a data-control source in the worker plus a `paste` command | high for little coverage |
| How to verify | fixture `--text-input` (text-input-v3) logs exact commits; gnome-text-editor saved file; Dolphin rename | fixture that reads the selection on `ctrl+v`; saved file | fixture with a compose-aware keymap; not worth building |

## Decision

1. **Go ahead: input-method commit.** Follow-up
   [#102](https://github.com/JosephWest2/kde_agent/issues/102), size medium:
   - a Wayland input-method client owned by the worker;
   - `type` keeps the key path for text the US layout can type, and commits any
     other text whole;
   - the request is refused before anything is sent when no context is active;
   - the result says whether `surrounding_text` confirmed the text;
   - a `--text-input` mode for the fixture, plus smoke, failure-path and
     gnome-text-editor tests.
2. **Clipboard paste: not for `type`.** It stays possible as a separate `paste`
   command if a workflow needs it (parking lot, #87). It needs a data-control
   source owned by the worker.
3. **Keymap approaches: no-go.** `type` keeps the fixed US layout. A
   per-session layout option would need KWin to read `kxkbrc` before start-up,
   and nothing has asked for one.
