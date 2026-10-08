# Accessibility tree (spike #85)

Today an agent finds controls by looking at screenshots and clicking pixel
coordinates. This spike measured whether the AT-SPI accessibility tree can be used
instead, inside the private session: start a private accessibility bus, turn
accessibility on in GTK3, GTK4 and Qt6, find controls by role and name, read
their text and value, and press them with an accessibility action.

**Decision: go, as an opt-in session feature; follow-up
[#104](https://github.com/JosephWest2/kde_agent/issues/104), size large.**
- **Why.**
  - A private bus started in about a quarter of a second (221–310ms, 5 starts)
    and left nothing behind on `session stop` in any of the 7 sessions.
  - Turning accessibility on made no difference to time to first window that
    the 100ms polling could see (5 runs per app, each way).
  - Finds took 14–28ms on gnome-text-editor (85 nodes, client walk) and
    29–51ms on GIMP (4,759 nodes, through the Collection interface).
  - Actions pressed buttons in GTK3, GTK4 and Qt6 probes, 10/10 each, without
    focus. In gnome-text-editor a Save As done entirely through AT-SPI (set the
    file name, invoke Save) wrote the file byte for byte (1/1). In GIMP,
    File > New... ran from a closed menu (1/1).
- **Limits.** These are why it is opt-in and why actions come first and
  coordinates second:
  - Blender has no accessibility tree at all.
  - Many controls have no name (GIMP's toolbox buttons) or the wrong one.
  - Accessible extents only map to `click` coordinates per toolkit, and GTK4's
    header bars were several pixels off.
  - A full GIMP tree is 1.76 MB, more than one 1 MiB response.
  - An action is a request, not a confirmation: GTK4 clicks 250ms later and Qt
    100ms later, and one Qt action returned true and did nothing.
  - The private bus is not a security boundary. Any process of the same user
    that can reach the socket can read the applications' text and invoke their
    actions directly, without the CLI's gates and records (section 1).
- **Command shape (proposed):** `ui tree`, `ui find`, `ui invoke`, `ui text`
  and `ui set-text` (below).

## Measurements, defined before the runs

| Measurement | How |
| --- | --- |
| Start-up cost | Time from spawning `at-spi-bus-launcher` to `org.a11y.Bus` answering on the private bus, then from spawning `at-spi2-registryd` to `org.a11y.atspi.Registry` being owned. Per app: time from spawn to the first KWin window (`wait --for window`, which polls every 100ms or more), with and without the bus, alternating, 5 runs each, after one discarded warm-up per app. With the bus: time from spawn to the app being a registry child with at least one toplevel (polled every 5ms with plain Gio). Resident memory of the bus processes. |
| Tree size | Nodes and bytes of JSON (role, name, states, interfaces, extents, up to 200 characters of text, value, up to 8 action names per node) for a full walk of each app, plus depth limits 3 and 6. |
| Query latency | Full dump, and find by role and name: a client-side walk, and a server-side `Collection.GetMatches` on role, then a name match. Each 3 times in a fresh helper process; `elapsed` is the query, `total` adds the helper start (about 40ms: Python and `gi` import). |
| Missing or wrong nodes | Each tree compared with a window screenshot. For three probe apps, toolkit-reported widget geometry (Qt `mapTo`, GTK4 `compute_bounds`, GTK3 `translate_coordinates`) compared with the accessible extents. |
| Cleanup | After `session stop`: no units, no processes in the generation's cgroup, the private runtime tree (which holds the accessibility bus socket) removed, and the host's own accessibility processes unchanged. |

## Method

- **Environment.**
  - KWin 6.7.5, the CLI's own private session (`session start`) and Linux 7.2.6.
  - at-spi2-core 2.60.7: `/usr/lib/at-spi-bus-launcher` and
    `/usr/lib/at-spi2-registryd`, the `Atspi-2.0` typelib (libatspi 2.60.7) and
    dbus-broker as the launcher's bus. pyatspi isn't installed and wasn't needed;
    PyGObject's `Atspi` was enough.
- **Applications.**
  - gnome-text-editor 50.1 (GTK 4.22.5) and GIMP 3.2.6 (GTK 3.24.52).
  - Dolphin (Qt 6.11.2, KF6). No Qt6 text editor is installed.
  - Blender 5.2.2.
  - Three probes, each the same small window in GTK3, GTK4 (PyGObject) and Qt6
    (PySide6 6.11.2): an entry, a check box, a slider, a combo box, a label, a
    text view and Cancel/Save buttons. Each wrote its own widget geometry and a
    timestamped log of button clicks.
- **The helpers ran inside the session.** As in #84, a Python sidecar was the
  session's one launched application. Its children were in the app's cgroup and
  had the private environment:
  - the bus launcher, the registry daemon and the applications under test;
  - a query helper (PyGObject `Atspi`), one fresh process per query.

  The sidecar changed the environment only by name: it unset `NO_AT_BRIDGE` for
  GTK3, and set or unset the Qt variables for the enablement matrix. A product
  version would put the bus under the worker instead (below).
- **The prototypes stayed out of the repository.** They were the sidecar, the
  probe apps, the query helper, a run script and a socket and environment audit,
  about 900 lines of Python in all.
- **Sessions.** The spike ran 7 sessions, all of which became ready: one
  exploratory, three scripted runs (N=1, N=2 and N=5 cycles), one enablement
  matrix, one for the frozen-app, popup, duplicate-title and Unicode checks, and
  one after review for the combined on and off environments.
  Each ended with `session stop`. One more attempt was refused before start
  with `prerequisite_missing`, because the script ran outside the repository and
  the CLI resolved its dependency root from the working directory.

## 1. A private accessibility bus

**What there is today.**
- The private `dbus-daemon` config (`desktop.py`) has no `<servicedir>`, so
  nothing on the private bus is activatable, `org.a11y.Bus` included.
- At the time of the spike, applications got `NO_AT_BRIDGE=1`,
  `QT_ACCESSIBILITY=0` and `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0`, and
  `AT_SPI_BUS_ADDRESS` is stripped (`environment.py`,
  [ARTIFACTS.md](ARTIFACTS.md)). There is no `DISPLAY`, so the X root-window
  property `AT_SPI_BUS` doesn't apply.
- So in practice **accessibility was off because no bus could be found**, not
  because of those variables (section 2). #107 has since changed the off
  values to the ones section 2 recommends: `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`
  removed and protected, `GTK_A11Y=none` added, `NO_AT_BRIDGE=1` and
  `QT_ACCESSIBILITY=0` kept.
- A libatspi client in the session with no bus doesn't fail cleanly: it logs
  `Couldn't connect to accessibility bus` and aborts (SIGABRT). A helper must
  check for `org.a11y.Bus` first, or treat that crash as "no bus".

**Starting one.**
- `/usr/lib/at-spi-bus-launcher --launch-immediately --a11y=1`, started with the
  private environment:
  - claims `org.a11y.Bus` on the private session bus;
  - starts `dbus-broker-launch` with `/usr/share/defaults/at-spi2/accessibility.conf`;
  - listens on `$XDG_RUNTIME_DIR/at-spi/bus`, which is
    `/run/user/1000/agent-desktop/g/GEN/desktop/at-spi/bus`, 84 bytes and under
    the 108-byte socket limit.
- `org.a11y.Bus.GetAddress` returned that address, `org.a11y.Status IsEnabled`
  was true and `ScreenReaderEnabled` false.
- **The registry has to be started explicitly.** dbus-broker activates services
  only through systemd. Its launcher's systemd connection opened the private
  session bus, the one `DBUS_SESSION_BUS_ADDRESS` names: it showed up there as a
  peer, and no `systemd1` exists on it. So activating `org.a11y.atspi.Registry`
  failed with `Could not activate remote peer 'org.a11y.atspi.Registry': unit
  failed`, and the host's systemd listed no new unit. Running
  `/usr/lib/at-spi2-registryd` (without `--use-gnome-session`) right after the
  bus is up fixes it. Clients that ask before then get the activation error.
- **Start-up cost** (5 cycles, one session):
  - launcher spawn to bus answering: 77–116ms;
  - registryd spawn to registry owned: 95–134ms;
  - launcher spawn to registry ready, including the probes: 221–310ms.
- **Footprint:** resident memory was 7.4 MB (launcher), 6.0 MB (registryd),
  3.8 MB (`dbus-broker-launch`) and 2.8 MB (`dbus-broker`): about 20 MB.

**Ownership and cleanup.**
- In the spike the bus processes were the sidecar's children, so they were in
  the launched app's cgroup.
- Killing the registry (SIGTERM) and then the launcher (it exits 0) removed the
  launcher's children and `org.a11y.Bus` (4 of 4 cycles). That took 479–527ms,
  including a fixed 0.3s wait and a status check.
- `session stop` cleaned up every time (7 sessions):
  - no `agent-desktop*` units;
  - no process left in the generation's cgroup;
  - the private `desktop/` tree removed, `at-spi/bus` socket included;
  - the host's at-spi processes the same before and after.
- A product version should make the launcher and registry desktop children
  owned by the worker, next to `dbus-daemon`: started after the bus probe and
  before any launch, and covered by the same cgroup teardown.

**The user's accessibility bus was never used.** The evidence, measured in the
N=2 and N=5 runs, plus the manual sessions where noted:
- **Environment.** Every process in the session cgroup was checked by variable
  name (values never printed): no `AT_SPI_BUS_ADDRESS`, no `DISPLAY`, and
  `DBUS_SESSION_BUS_ADDRESS` inside the generation's runtime tree. The worker
  has no session-bus variable at all.
- **Private addresses.** The private `org.a11y.Bus` gave the private socket.
  Every application under test showed up in the private registry. The peers on
  the private accessibility bus were all session processes.
- **Host bus untouched.** The host's accessibility broker had 23 open file
  descriptors before, during and after each scripted session.
- **Not checked at socket level.** The kernel's `unix_diag` peer information
  isn't available here (`ss` reports peer 0), so socket peers couldn't be listed
  directly.
- **Separation, not a sandbox.** A program in the session that deliberately
  connected to `/run/user/1000/at-spi/bus_0` would not be stopped
  ([ARTIFACTS.md](ARTIFACTS.md)).
- **The reverse direction is open too.** The private bus uses
  `accessibility.conf`: `EXTERNAL` authentication, which admits the bus's own
  user (and root), and a policy that allows sending to any destination. So any
  other process of the same user that can reach
  `$XDG_RUNTIME_DIR/at-spi/bus` (or ask the private session bus for
  `org.a11y.Bus`) can read every registered application's text and call its
  actions, `EditableText` included, without going through the CLI. The CLI's
  durable intents, deadlines and records don't apply to it. This comes from the
  configuration and the socket's location; no outside connection was attempted.
  The private bus keeps ordinary discovery apart (host tools and screen readers
  don't find it), but it is **not a confidentiality or mutation boundary**. The
  private session bus and Wayland socket are just as reachable today;
  accessibility adds reading text and pressing widgets with one D-Bus call. Opting in should
  say so.

**KWin joins the bus too.**
- The private KWin is a Qt program, and at the time of the spike its
  environment had `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0` (section 2; removed
  since by #107). It connected to the private
  accessibility bus as soon as one existed. It logged `Error in contacting
  registry` to the user journal (the exact trigger wasn't isolated).
- It never appeared as a registry application.
- Sessions stayed healthy, window queries included. Whether KWin's
  accessibility bridge can stall the compositor was not measured.

## 2. Turning accessibility on

The enablement matrix: each probe in one session, with `IsEnabled` false
(`--a11y=0`) and then true (`--a11y=1`). Registered means a registry child with
a toplevel within 2.5s; 1 sample per cell.

| Toolkit | Environment | `IsEnabled` false | `IsEnabled` true |
| --- | --- | --- | --- |
| Qt 6.11 | private default (`QT_ACCESSIBILITY=0`, `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0`) | registered | registered |
| Qt 6.11 | both unset | not registered | registered |
| Qt 6.11 | `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` | registered | registered |
| GTK 4.22 | private default (`NO_AT_BRIDGE=1`) | registered | registered |
| GTK 4.22 | `GTK_A11Y=none` | not registered | not registered |
| GTK 3.24 | private default (`NO_AT_BRIDGE=1`) | not registered | not registered |
| GTK 3.24 | `NO_AT_BRIDGE` unset | registered | registered |

- **Qt** follows `IsEnabled` unless `QT_LINUX_ACCESSIBILITY_ALWAYS_ON` is
  **set**, and any value turns it on, `0` included. Qt 6.11's libQt6Gui doesn't
  mention `QT_ACCESSIBILITY` at all (`strings`).
- **GTK4** (the AT-SPI backend; see below) ignores both `IsEnabled` and
  `NO_AT_BRIDGE`. It registers whenever a bus exists; only `GTK_A11Y=none`
  stops it.
- **Which GTK4 backend was measured.** GTK4 picks its accessibility backend
  with `GTK_A11Y`: `atspi`, `accesskit` (when built), `test` or `none`. This
  host's gtk4 4.22.5 (Arch package `1:4.22.5-1`) was built without AccessKit:
  the library's `GTK_A11Y` help text lists `accesskit - Disabled during GTK
  build` and has no enabled variant, and there are no `accesskit_` symbols
  (`strings`). Running with `GTK_A11Y=help` printed nothing, because Gtk.init
  without a window doesn't reach backend selection. So every GTK4 result here
  is for the **AT-SPI backend** only. AccessKit, in builds that include it,
  is reported to follow `IsEnabled` (from the review of this spike; not checked
  here), so its behaviour may differ. The policy below
  therefore sets `GTK_A11Y=atspi` explicitly when on, rather than relying on
  the default.
- **GTK3**'s ATK bridge ignores `IsEnabled` and is controlled by `NO_AT_BRIDGE`.
- **So the spike-time "disabled" variables didn't disable Qt6 or GTK4.** They
  were harmless only because no bus existed. The off state should instead
  (done in #107):
  - remove `QT_LINUX_ACCESSIBILITY_ALWAYS_ON` (not set it to `0`);
  - add `GTK_A11Y=none`;
  - keep `NO_AT_BRIDGE=1`.
- **The on state the follow-up should use:**
  - the launcher with `--a11y=0`, so `IsEnabled` is false;
  - applications get `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` and
    `GTK_A11Y=atspi`, with no `QT_ACCESSIBILITY` and no `NO_AT_BRIDGE`;
  - KWin keeps the off values, so it stays off the bus.
- **Both combinations, run as a whole** (one session after the review, with the
  launcher at `--a11y=0` and a registry running; 1 sample per cell):
  - on: the Qt, GTK4 and GTK3 probes all registered, and a find for `button`
    `Save` matched once in each;
  - off (Qt variables removed, `GTK_A11Y=none`, `NO_AT_BRIDGE=1`): none of the
    three registered, although a bus existed.

  So `GTK_A11Y=none` is the right disable setting for the AT-SPI-only build
  measured here. `none` selects GTK's no-op backend whatever is built, so it
  should disable an AccessKit build too, but that wasn't measured.
- **Not measured.** Setting `IsEnabled` at run time through
  `org.freedesktop.DBus.Properties.Set` was tried once. The property read back
  false, and the test was confounded anyway (that Qt probe had `ALWAYS_ON=0`).
- **Start-up cost per app**, time to the first KWin window in ms (N=5 each way,
  alternating, 100ms polling resolution):

  | App | Off: median (min–max) | On: median (min–max) | On: registered in the registry |
  | --- | --- | --- | --- |
  | gnome-text-editor (GTK4) | 183 (154–196) | 192 (168–204) | 127 (122–134) |
  | GIMP 3 (GTK3) | 5,044 (4,940–5,138) | 5,040 (5,037–5,116) | 4,990 (4,977–5,052) |
  | Dolphin (Qt6) | 194 (187–207) | 197 (190–213) | 100 (96–107) |

  No difference shows at this resolution. The registry sees each application
  before or about when the window poll does. Blender never registered: it has
  no AT-SPI support, so the tree is empty while KWin lists its window.

## 3. Bounded queries

- **The helper.** Queries ran in a fresh helper process: Python, PyGObject
  `Atspi`, the session's environment. Each was bounded by
  `Atspi.set_timeout(500–1000ms)` per D-Bus call, plus limits on depth, nodes
  and time that the walk checks between calls.
- **libatspi is synchronous.** Its calls block, so it must never run on the
  worker's GLib owner thread. A helper process the worker supervises like
  kdotool, with a hard kill at the deadline, is the right shape.
- **Collection support.** `Collection.GetMatches` matches on the application's
  side in one call. Qt and GTK3 support it; **GTK4 doesn't** (the app root has
  no Collection), so GTK4 needs a client walk.

Full trees and finds from the N=5 run, 3 queries each, in ms. `elapsed` is the
query only; `total` adds the helper start (about 40ms). The N=2 run gave
similar ranges (GIMP full dump 1,639–1,844ms).

| App | Nodes | JSON bytes | Full dump elapsed | Find by walk elapsed | Find by Collection elapsed | Depth 3 / 6 |
| --- | --- | --- | --- | --- | --- | --- |
| gnome-text-editor, main window | 85 | 23,088 | 29–122 | 14–28 | not supported | 4 / 7 nodes |
| gnome-text-editor, with the Save dialog open | 336 | 102,551 | 175–236 | | | |
| GIMP 3, main window | 4,759 | 1,758,451 | 1,688–1,823 | 330–380 | 29–51 (719 `menu item` hits, then a name match) | 19 / 852 nodes |
| Dolphin | 637 | 161,835 | 258–351 | 81–181 | 2–4 (27 `button` hits) | 37 / 205 nodes |
| Probes (GTK4 / GTK3 / Qt6) | 27 / 22 / 24 | 8,188 / 6,010 / 6,656 | 27 / 4 / 23 | | | |

- **Limits work.**
  - A 100ms deadline stopped at 76 nodes (gnome-text-editor, 102ms), 508 (GIMP,
    101ms) and 213 (Dolphin, 101ms).
  - `--max-nodes 200` stopped at 200.
  - Depth limits are cheap, but not useful on their own: controls sit deep
    under unnamed panels. gnome-text-editor's header buttons are 17–18 levels
    down and its tree is 25 deep; GIMP's is 26 and Dolphin's 13.
- **Size.**
  - A full GIMP tree, 1.76 MB, doesn't fit one 1 MiB response. `ui tree` should
    write the walked tree to an artifact file and return counts plus a bounded,
    filtered list. The artifact is only the full tree when the walk was
    complete; its metadata says which (see "Proposed commands").
  - Only 251 of GIMP's 4,759 nodes are `showing`; 2,997 are table cells and 719
    are menu items of closed menus.
- **A frozen application** (SIGSTOP on the Qt probe, 500ms per-call timeout):
  - listing all applications took 1.5s, about three timeouts' worth, and
    returned the frozen app with no name and no children;
  - a dump of it returned after about 1s with one error and one node, with a
    2s or a 10s deadline alike (it gave up on the first failed call);
  - queries to the other applications were unaffected (6ms);
  - after SIGCONT it answered normally.

  So per-call timeouts compound, and the helper needs an overall deadline that
  the worker enforces by killing it.
- **Reading text and values works.**
  - `Text.GetText` read gnome-text-editor's buffer exactly after `type`
    (`hello accessibility\nsecond line`, 31 characters), its file-name field,
    and labels.
  - `Value` gave slider and spin-button values with their ranges: 42 of 0–100
    in all three probes; GIMP's width 1920 of 1–524288.
- **Setting text works too.**
  - In gnome-text-editor's Save dialog, `EditableText.SetTextContents` set the
    name field to a path, read back exactly.
  - As a side note for #102: the same call set `héllo ✓ 中文 🎉 é` (with a
    combining accent) in all three probes' entries, read back exactly through
    AT-SPI; 1 sample each, not checked against the app's own state.

## 4. Mapping accessible objects to window refs

Each registry application has a pid (`GetConnectionUnixProcessID`, or
`Accessible.get_process_id`), and its children are the toplevels: role `frame`
or `dialog`, and the accessible name is the title.

| Key | Result | Reliability |
| --- | --- | --- |
| pid | Matched KWin's `pid` for every app tested; a popup row keeps the owner's pid | Good, as a first filter. KWin pids are lookup keys, not authority ([WINDOWS.md](WINDOWS.md)); the worker's existing association check still applies |
| Title (toplevel name equals KWin caption) | Matched every window and dialog: editor, `Save a File`, `Create a New Image`, Dolphin, probes. The name follows title changes | Ambiguous with duplicate titles: two gnome-text-editor windows, same pid, both `New Document (Draft) - Text Editor`, same 700×520 size |
| `active` state | Exactly the toplevel KWin reported active had the `active` state | Settles duplicates only for the active window. Focusing the target first, then matching the active toplevel, works, at the cost of a focus change |
| Size | GTK4 and Qt toplevel extents equal the KWin client size. A GTK3 client-side-decorated dialog's equals KWin's `bufferGeometry` (it includes the shadow) | Can't tell apart same-sized windows. Positions are all (0,0) on Wayland, so they give nothing |
| Popups | The Qt combo popup was a KWin `popup` row (pid of the app). In AT-SPI its items were under the combo box in the main window's subtree, not a separate toplevel. A GTK4 popover was not checked | Map popup content through the parent window |

**Rule for the follow-up:**
- take the app's toplevels with the window's pid;
- keep those whose name equals the title;
- if more than one is left, keep the one in the `active` state if W is active;
- otherwise fail with `target_ambiguous` (exit 6), with the bounded candidate
  toplevels in its context and `focus` named as the way out.

Never guess by order.

## 5. Actions versus coordinates

**Actions.**

| Probe | `doAction` success | Click effect after `doAction` returned |
| --- | --- | --- |
| GTK3 `click` | 10/10 | synchronous: the handler ran inside the call |
| GTK4 `click` | 10/10 | 250ms later (GTK's press animation) |
| Qt6 `Press` | 10/10 | about 100ms later (95–106ms; `animateClick`) |

- **No focus needed.** Actions worked on windows that weren't focused, and none
  of them moved focus.
- **Real applications.**
  - gnome-text-editor's Save dialog: set the name, invoke `Save`; the file was
    written byte for byte (1/1).
  - GIMP: invoking the closed `File > New...` menu item opened `Create a New
    Image` (1/1).
  - Qt's combo `ShowMenu` opened the popup (1/1).
- **An action is not a confirmation.**
  - `doAction` returns before the effect, and a `true` return doesn't mean
    anything happened. On a Qt combo popup item, `Toggle` returned true and
    selected nothing (1/1).
  - Dolphin's `Open Menu` and its file items expose no action at all.
  - So `ui invoke` reports `sent`, not success. The agent observes the result
    (`wait --for title`, `windows`, `ui text`), as for input.

**Extents.**
- `Component.GetExtents` was read in SCREEN and WINDOW coordinates, then
  checked against toolkit geometry (probes) and screenshots (applications).

| Toolkit | SCREEN | WINDOW | Matches `click --window` coordinates? |
| --- | --- | --- | --- |
| Qt 6.11 | same as WINDOW (window-relative; Wayland has no global position) | the client area | Yes: 7 of 8 probe widgets exact; the check box's width is 93 against the widget's 450 (Qt reports the box and text part). Dolphin's toolbar looked right but wasn't measured |
| GTK 4.22 | x, y always 0 | the client area | Probe: 4 of 8 widgets exact, 4 off by 1px. In gnome-text-editor's libadwaita header bar the buttons were about 6px left of and 5px above where the screenshot draws them (estimated from glyph positions in one screenshot); the text area was right |
| GTK 3.24 | same as WINDOW | the **surface buffer**, including a client-side shadow | KWin decorations (the probe): 8 of 8 exact. GIMP's client-side dialog: offset by KWin's `clientGeometry - bufferGeometry` = (26, 23), which matched the screenshot |

- **Clicks at accessible centres hit.** In each probe, clicking the centre of
  the accessible extents of `Cancel` with `click --window` pressed it 5/5. (The
  first attempt, without `focus`, was refused with `target_lost`, as for any
  click.)
- **Wrong extents.**
  - GTK3 hidden menus report x, y = −2147483648 and size 1×1.
  - GTK 4.22 reported a width of −1627507280 and height 32725 for 2 panels in
    gnome-text-editor's main window, and 6 in the window with the Save dialog.
  - GTK4 marks nearly everything `showing`: 335 of 336 nodes, including path-bar
    buttons scrolled past the dialog's edge.
  - Qt's hidden combo list reports items 707px wide at (0,0).
- **The conversion** is therefore per toolkit:
  - Qt and GTK4: WINDOW extents are client coordinates;
  - GTK3: subtract KWin's buffer inset, which the window query would need to
    return (`bufferGeometry` is available in KWin scripting; a probe script read
    it through kdotool);
  - discard values that are clearly invalid.

  Report bounds as approximate, prefer actions, and point agents to a window
  screenshot to confirm before clicking.

## Missing or wrong nodes, compared with what is visible

| App | Visible but missing or unnamed | Present but wrong |
| --- | --- | --- |
| gnome-text-editor | No visible control was missing: Open, New Tab, Document Properties, Main Menu, Minimize, Maximize, Close and the text view are present and named. 71 of 85 nodes are unnamed, 68 of them `panel`/`grouping` layout nodes | Header bar extents about (6, 5)px off; 2 panels with garbage sizes. In the Save dialog the file list had 7 file rows; whether rows outside the visible area appear (GTK4 list views create widgets only for visible rows) wasn't checked. The Size and Type cells of folders are named with the folder's full path |
| GIMP 3 | All 42 showing buttons in the main window have empty names (16 toolbox tools, the dock buttons); spin buttons are unnamed but have values | Closed menus are fully present (719 items; useful for invoking); hidden combo menus at −2147483648. In client-side dialogs every extent is offset by the shadow |
| Dolphin | `Open Menu` and the file items have no action | Hidden search-option items (`Plain Text`, `Glob Pattern`, ...) are reported `showing` at (0,0) |
| Probes | In the GTK probes the entry, slider and combo box have no name: the probe didn't link the labels to them, which Qt's `setAccessibleName` did. Names depend on how each app is written | Qt and GTK3 name the combo box after its current item (`alpha`); in Qt that replaces its accessible name `Mode`. Qt's `Toggle` on a popup item did nothing |
| Blender | Everything: no AT-SPI | — |

## Proposed commands

All of them are opt-in: `session start --accessibility`, or a per-launch
`--accessibility`. Without it, `ui` fails with `unsupported_operation`, reason
`accessibility_off`. Every `ui` command takes `--window W` (a `window`-kind row;
popups are reached through their parent), maps W to one toplevel (section 4)
and runs one bounded helper.

```sh
agent-desktop --json ui find   --window W --role button --name Save [--name-regex RE] [--showing] [--limit 20]
agent-desktop --json ui tree   --window W [--depth 8] [--max-nodes 10000] [--showing]   # walked tree goes to an artifact file
agent-desktop --json ui invoke --window W --role button --name Save [--index 0] [--action click]
agent-desktop --json ui text   --window W --role text [--name Name] [--max-chars 4096]
agent-desktop --json ui set-text --window W --role text --name Name TEXT                  # EditableText, if the follow-up keeps it
```

- **Roles** are libatspi's role names: `button`, `toggle button`, `menu item`,
  `text`, `check box`, `combo box` and so on. Qt and GTK both report
  `push button` as `button`.
- **One request finds and acts.** `ui invoke` finds and invokes in one helper
  run. It fails with `target_not_found` if nothing matches, and with
  `target_ambiguous` (exit 6, the existing code; at most 20 candidate rows in
  its context, with a `candidates_total` count) if several do and `--index`
  isn't given. There are no node handles kept across requests, because paths
  change as the tree changes.
- **Completeness.** Every `ui` result carries `walk`:
  `{"complete": bool, "visited": N, "limit": "max_nodes" | "depth" | "deadline"
  | "output_bytes" | "call_errors" | null, "call_errors": N}`. A walk is complete
  only if no limit stopped it and no call failed; a Collection match is complete
  when its single call succeeded.
  - `ui find` and `ui tree` may return incomplete results, marked as such. A
    `ui tree` artifact is called the walked tree, never the full tree, unless
    `complete` is true.
  - **Mutations fail closed.** `ui invoke` and `ui set-text` act only on a
    unique match from a complete resolution. From an incomplete one they claim
    neither `target_not_found` nor a unique match: they fail before dispatch
    (`outcome: not_started`) with a reason such as `resolution_incomplete`,
    with the walk metadata in context. Narrow `--role` (Collection, where the
    app has it) or raise the limits.
  - The default 10,000 nodes covers every tree measured (GIMP's 4,759 is the
    largest) within the deadline: GIMP's full walk took 1.7–1.8s. A lower
    `--max-nodes` is allowed, with the consequences above.

- **Rows** look like this:

```json
{"path": [0, 3, 1], "role": "button", "name": "Save", "states": ["enabled", "focusable", "showing"],
 "actions": ["click"], "text": null, "value": null,
 "bounds": {"x": 429, "y": 342, "width": 68, "height": 34, "coordinates": "client", "exact": false},
 "toolkit": "GTK 4.22.5"}
```

  - `bounds` is in `click --window` coordinates. It is null, with a reason, when
    the toolkit's value is invalid or can't be converted. `exact` is never true
    for GTK4.
  - `text` is truncated to `--max-chars`, with the full `chars` count given.
- **`ui invoke`'s result** has `action`, `sent: true` and the matched row. It
  doesn't wait for the effect.
- **Mutations are gated like input.** `ui invoke` and `ui set-text` are effects
  under [ARTIFACTS.md](ARTIFACTS.md)'s "before effects" rule: admission and
  start records are not enough. The helper runs in two phases:
  1. **Resolve** (no effect): map the window, walk or match, and report the
     unique target (path, role, name, pid, toplevel) and the action or text
     length to the worker, then wait.
  2. The worker queues a **mutation intent** record (outcome uncertain, naming
     the window, target, action or text length, never the text itself) and
     waits, without blocking the owner thread (as input does today), until
     that record and every earlier record of the request are durable.
  3. The worker then rechecks: enough of the work deadline is left for one
     call, the request isn't cancelled, and W is still the same window in the
     same generation. Only then does it **authorize** the helper, over a pipe,
     to make the one mutating call (`DoAction` or `SetTextContents`).
  4. The helper re-resolves the target by path and checks role and name before
     the call. A mismatch fails as `target_lost`, still before dispatch.
- **Uncertain completion.**
  - Any failure before the worker authorizes the call is
    `outcome: not_started`: resolution errors, a timeout, a helper crash, a
    cancelled request or a failed recheck.
  - Once authorized, the helper reports `dispatching` and then makes the call.
    If it then times out, crashes, is killed or the reply is lost, the result is
    `completion_unknown` (exit 11) with `outcome: unknown`. `partial_result`
    keeps the window, target row, action (or text length) and whether the reply
    arrived. A `false` reply is `outcome: unknown` too, because the
    application may still have acted.
  - Nothing is retried automatically ([CLI.md](CLI.md)'s rule for launch and
    input applies). The docs tell agents to check the application instead.
- **Deadlines.** The **work deadline** (default 2s) bounds resolution and the
  mutating call. Authorization is refused when less than one per-call timeout
  (500ms) of it is left. A separate **reap allowance** (default 0.5s) after
  the work deadline is only for killing and reaping the helper: the worker
  closes the authorization pipe at the work deadline, so no mutation can start
  during cleanup. Other defaults: 500ms per D-Bus call, depth 32, 10,000 nodes,
  and 512 KiB of rows in the response.
- **Data handling.** Text read from applications is application data, like
  screenshots: it is returned to the caller, and the follow-up decides whether
  request records keep it.

## Decision

1. **Go:** an opt-in private accessibility bus with `ui find`, `ui tree`,
   `ui invoke` and `ui text`; follow-up
   [#104](https://github.com/JosephWest2/kde_agent/issues/104), size
   large. It can split into a read-only part and an invoke and set-text part.
2. **Not a security boundary.** Opting in exposes every registered
   application's text and actions to any process of the same user that reaches
   the private bus, outside the CLI's gates (section 1). The opt-in flag's
   documentation must say so.
3. **Actions first, coordinates second.** Bounds are returned as approximate
   hints with a per-toolkit conversion. GTK3 needs KWin's buffer geometry added
   to the window query.
4. **Fix the off state anyway.** The spike-time
   `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0` turned Qt accessibility on whenever a
   bus exists, and GTK4 ignores `NO_AT_BRIDGE`. Fixed by #107.
5. **Scope.** [REQUIREMENTS.md](../REQUIREMENTS.md) lists accessibility
   automation as outside the initial scope, and
   [ARCHITECTURE.md](../ARCHITECTURE.md) calls the tree deliberately absent.
   Going ahead changes both; #104 includes that, and it needs the owner's
   approval like any scope change.
6. **No-go for Blender and custom-rendered apps.** They keep using screenshots
   and coordinates, which [REQ-017](../REQUIREMENTS.md) already requires to work
   without accessibility.
