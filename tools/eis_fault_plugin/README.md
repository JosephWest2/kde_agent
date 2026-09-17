# Issue #12 private EIS pause fault

This test-only KWin plugin calls the installed `KWin::EisDevice::setEnabled`
implementation through its exported `InputDevice` interface. It never injects
keys, synthesizes release events, accesses raw libeis objects, or clears KWin's
pressed-key state. It is not part of the Python runtime input adapter.

Build, without installing globally or loading a compositor:

```sh
python tools/build_eis_fault_plugin.py
```

The helper downloads checksum-pinned ECM 6.26.0 into the checkout's ignored
`.local/issue12-eis-fault/`, installs only that CMake dependency into a local
prefix, and uses the installed KWin CMake target. The plugin requires exactly
KWin 6.7.5 and must be rebuilt/retested after a KWin update. The generated
`build-receipt.json` records commands, dependencies, compiler, embedded metadata,
source/header/library hashes, binary hash and build outcome. Earlier receipts
are retained on subsequent builds. The metadata audit reads the binary's Qt
metadata without loading/instantiating the plugin.

Output: `.local/issue12-eis-fault/build/issue12_eis_fault.so`; plugin ID:
`issue12_eis_fault`. Metadata sets `EnabledByDefault: false`. Only an explicitly
enabled private harness generation may place this file under its own
`plugins/kwin/plugins/`, pass its own plugin root in KWin's `QT_PLUGIN_PATH`, and
enable `issue12_eis_faultEnabled=true` in that generation's private kwinrc
`[Plugins]` group. Do not copy it into user or system plugin directories.

The factory fails closed unless `HARNESS_EIS_FAULT_PLUGIN=1`, a 32-character
lowercase hex `HARNESS_GENERATION`, the owner/mode-0700 `/tmp/kde-m1-*` runtime
and HOME, runtime `owner.json` generation, `XDG_CONFIG_HOME`, and the exact
owner-private bus socket/address match the harness. Registration failure also
prevents plugin creation. All control methods require the same generation and
an actual private D-Bus call.

Control uses service `org.kde.KWin`, path `/org/kde/KWin/Issue12EisFault`, interface
`org.kde.KWin.Issue12EisFault`:

| Method | D-Bus input | Output |
| --- | --- | --- |
| `Status` | `s` generation | `s` JSON |
| `Pause` | `ssu` generation, client name, milliseconds (1–250) | `s` JSON |
| `Resume` | `ss` generation, client name | `s` JSON |

The exact client selector is `issue12-<generation>-epoch-<positive decimal>`.
Pause requires exactly one enabled keyboard named `<client name> eis keyboard`
whose Qt class is exactly `KWin::EisDevice`. Resume accepts only the controller's
original unique D-Bus name and selector. The private bus connection must stay
alive to keep a requested pause active: controller disappearance resumes it.
The precise single-shot timer requests automatic resume after at most 250ms;
actual callback delay is subject to compositor scheduling and is recorded.

The plugin retains a `QPointer`, revalidates exact device identity before resume,
and resumes the same surviving device on explicit request, timer expiry,
controller loss or plugin unload. If destruction/removal prevents resume, it
records `ok:false`; it does not select a replacement device. A resumed device
does not establish that held keys are neutral. The Python probe must consume
real libei PAUSED/RESUMED, gate actions, and prove connection-reset recovery and
fixture release independently.

Each result is a compact JSON string, also written to KWin stderr. `ok:false`
with an explicit `event` names rejection/failure; successful responses include
generation, client/controller, ABI and monotonic timestamps. Active device
records additionally carry exact name/class/enabled state. Load/unload and
automatic resume records are available in that same stderr evidence stream.
