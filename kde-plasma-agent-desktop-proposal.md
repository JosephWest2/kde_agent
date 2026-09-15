# Proposal: `agent-desktop` KDE Plasma Backend

## Status

**Proposed**

## Summary

This document proposes a KDE Plasma / KWin implementation of a local desktop-control CLI for autonomous coding agents.

The tool should allow coding agents to:

1. launch graphical applications,
2. discover and identify application windows,
3. focus or activate windows,
4. capture screenshots,
5. inject basic keyboard and pointer input,
6. inspect window metadata,
7. gracefully close or force-stop applications,
8. and verify that the graphical application exited.

The intended use case is agentic development of games and native applications on Linux under KDE Plasma + Wayland.

The preferred architecture is to expose a **desktop-independent CLI** named:

```text
agent-desktop
```

with a KDE-specific backend:

```text
agent-desktop
    ↓
backend-kde
    ↓
KWin + XDG portals + PipeWire + libei/EIS
```

This keeps the public interface stable if support for other desktops is added later.

---

# 1. Motivation

Coding agents are already effective at:

- editing source code,
- compiling,
- running tests,
- inspecting logs,
- creating commits,
- and iterating on failures.

Native graphical applications introduce an additional need:

```text
edit code
   ↓
build
   ↓
launch application
   ↓
find its window
   ↓
observe graphical output
   ↓
send basic input
   ↓
observe result
   ↓
close application
```

On Wayland, arbitrary screenshot and input access is intentionally restricted.

KDE Plasma is a strong target because it provides several useful capabilities at once:

- standard XDG Desktop Portal support,
- PipeWire screen capture,
- RemoteDesktop portal support,
- KWin scripting and window metadata,
- persistent desktop-control permissions,
- explicit pre-authorization options,
- and a relatively rich compositor API.

The goal of `agent-desktop` is to hide these platform details behind a stable shell interface.

---

# 2. Goals

Primary goals:

- Support KDE Plasma running on Wayland.
- Launch graphical applications.
- Track launched processes.
- Discover application windows through KWin.
- Correlate windows with process IDs.
- Query window metadata.
- Focus or activate windows.
- Capture screenshots.
- Capture specific windows when practical.
- Inject basic keyboard input.
- Inject pointer movement and clicks.
- Support key hold/down/up operations for game testing.
- Gracefully close applications.
- Force-kill applications when necessary.
- Provide machine-readable JSON output.
- Work well from coding agents and shell scripts.
- Support non-interactive operation after explicit user authorization/pre-authorization.
- Use the KDE/Wayland security model rather than bypassing it.
- Remain independent from any specific coding-agent product.
- Support future MCP wrapping without redesigning the core.

---

# 3. Non-Goals

The initial implementation is not intended to be:

- a full GUI testing framework,
- a replacement for Playwright,
- a computer vision library,
- an OCR system,
- an accessibility testing framework,
- a remote desktop server,
- a screen recorder,
- a general-purpose window manager,
- a cross-platform abstraction from day one.

The first version should solve one problem well:

> **Allow an autonomous local coding agent to launch, inspect, minimally interact with, and terminate a graphical application on KDE Plasma Wayland.**

---

# 4. Target Environment

Initial target:

```text
OS:              Linux
Primary distro:  Arch Linux
Desktop:         KDE Plasma
Compositor:      KWin
Display server:  Wayland
Portal:          xdg-desktop-portal-kde
Capture:         ScreenCast portal + PipeWire
Input:           RemoteDesktop portal + EIS/libei
Window control:  KWin scripting / KWin APIs
```

The implementation should avoid Arch-specific assumptions where possible.

---

# 5. High-Level Architecture

```text
                      Coding Agent
                           │
                           │ shell commands
                           ▼
                    agent-desktop CLI
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
       Process Layer   Desktop Layer   Session Layer
            │              │              │
            │        ┌─────┼─────┐        │
            │        │     │     │        │
            ▼        ▼     ▼     ▼        ▼
          Linux     KWin  XDG  PipeWire  Runtime
        processes   API  Portals streams   state
                         │
                ┌────────┴────────┐
                ▼                 ▼
           ScreenCast        RemoteDesktop
              portal             portal
                │                 │
                ▼                 ▼
          screenshots       mouse/keyboard
```

---

# 6. Architectural Principle

The public CLI should not expose KDE-specific implementation details.

Good:

```bash
agent-desktop windows
agent-desktop screenshot --window win_01
agent-desktop focus --window win_01
agent-desktop key W --hold 1s
```

Avoid requiring agents to call:

```text
qdbus
kwin scripting internals
PipeWire commands
portal D-Bus methods
libei tooling
```

directly.

The KDE-specific backend should absorb all of that complexity.

---

# 7. Why KDE Plasma

KDE is attractive for autonomous desktop control because it gives the implementation access to both:

```text
standard desktop APIs
+
compositor-specific window APIs
```

The desired split is:

```text
capture/input     → XDG portals + PipeWire + libei
window metadata   → KWin
process lifecycle → Linux process APIs
```

This is preferable to trying to solve every operation through screenshots and coordinates.

---

# 8. Proposed CLI

Executable:

```text
agent-desktop
```

Suggested command tree:

```text
agent-desktop
├── session
│   ├── start
│   ├── status
│   └── stop
│
├── launch
├── processes
├── wait
├── close
├── kill
│
├── windows
├── window
│   ├── get
│   ├── focus
│   ├── close
│   └── bounds
│
├── screenshot
│
├── pointer
│   ├── move
│   ├── click
│   └── scroll
│
├── key
├── type
│
├── input
│   └── reset
│
└── doctor
```

---

# 9. Session Management

Wayland screen capture and input injection are permissioned capabilities.

The tool should expose this as an explicit desktop-control session.

## Start

```bash
agent-desktop session start
```

Options:

```bash
agent-desktop session start \
  --capture \
  --input
```

JSON:

```json
{
  "ok": true,
  "result": {
    "session_id": "sess_01",
    "capture": true,
    "input": true,
    "restored": true
  }
}
```

Requirements:

- Reuse a compatible active session where possible.
- Restore a previously authorized session where supported.
- Report whether capture/input capability is actually available.
- Never claim success before authorization has succeeded.

---

# 10. KDE Pre-Authorization

A major goal is unattended agent operation after deliberate user configuration.

KDE Plasma should support a setup where the trusted desktop-control application is explicitly authorized for remote desktop capability.

Conceptually:

```text
user explicitly authorizes agent-desktop
        ↓
KDE permission store
        ↓
future agent-desktop sessions
        ↓
capture/input available without interactive approval
```

The implementation should support:

```bash
agent-desktop doctor
```

detecting whether the required authorization exists.

The CLI itself should **not** silently modify permission policy.

A separate setup command may later be added:

```bash
agent-desktop setup
```

but it should always clearly explain any persistent permissions it changes.

---

# 11. Session Status

```bash
agent-desktop session status
```

Example:

```text
Session: active
Capture: available
Input:   available
Portal:  xdg-desktop-portal-kde
```

JSON:

```json
{
  "ok": true,
  "result": {
    "active": true,
    "capture": true,
    "input": true,
    "backend": "kde"
  }
}
```

---

# 12. Session Stop

```bash
agent-desktop session stop
```

Should:

- close active portal sessions,
- close PipeWire streams,
- release held input,
- remove transient runtime state,
- leave launched applications untouched unless explicitly requested.

---

# 13. Application Launch

```bash
agent-desktop launch ./target/debug/mygame
```

With arguments:

```bash
agent-desktop launch ./game -- --test-mode
```

Options:

```text
--cwd PATH
--env KEY=VALUE
--wait-window
--timeout DURATION
--stdout FILE
--stderr FILE
--json
```

Example:

```bash
agent-desktop launch \
  --cwd ~/src/game \
  --wait-window \
  --timeout 15s \
  ./target/debug/game \
  -- --test-mode
```

JSON:

```json
{
  "ok": true,
  "result": {
    "pid": 41220,
    "window_id": "win_01"
  }
}
```

---

# 14. Process Tracking

```bash
agent-desktop processes
```

The command should list processes launched or explicitly tracked by `agent-desktop`.

Example:

```text
ID       PID    STATE     COMMAND
game-1   41220  running   ./target/debug/game
```

This is not intended to replace `ps`.

---

# 15. Window Discovery

KWin should be the primary source of window metadata.

```bash
agent-desktop windows
```

Suggested fields:

```text
ID
TITLE
PID
APP_ID
WORKSPACE
OUTPUT
X
Y
WIDTH
HEIGHT
VISIBLE
ACTIVE
FULLSCREEN
```

Example:

```text
ID      PID    ACTIVE  TITLE
win_01  41220  yes     My Game
win_02  39811  no      Konsole
```

JSON:

```json
{
  "ok": true,
  "result": [
    {
      "id": "win_01",
      "title": "My Game",
      "pid": 41220,
      "app_id": "com.example.Game",
      "active": true,
      "visible": true,
      "fullscreen": false,
      "geometry": {
        "x": 100,
        "y": 60,
        "width": 1600,
        "height": 900
      }
    }
  ]
}
```

---

# 16. Window Identification

The tool should support targeting by:

```text
window ID
PID
application ID
exact title
```

Preferred priority:

```text
window ID > PID > app ID > title
```

Title matching should not be the primary mechanism because titles are often dynamic.

Examples:

```bash
agent-desktop window get --pid 41220
```

```bash
agent-desktop window get --app-id com.example.Game
```

---

# 17. Window Focus

```bash
agent-desktop window focus --window win_01
```

or:

```bash
agent-desktop window focus --pid 41220
```

Requirements:

- Use KWin activation mechanisms where possible.
- Confirm resulting active state.
- Return failure if focus cannot be confirmed.
- Avoid coordinate-based activation hacks.

---

# 18. Window Bounds

```bash
agent-desktop window bounds --window win_01
```

Output:

```text
x=100 y=60 width=1600 height=900
```

JSON:

```json
{
  "ok": true,
  "result": {
    "x": 100,
    "y": 60,
    "width": 1600,
    "height": 900
  }
}
```

Window bounds are important for:

- coordinate translation,
- screenshot cropping,
- visual verification,
- pointer targeting.

---

# 19. Waiting for Windows

```bash
agent-desktop wait --pid 41220 --until window-visible
```

Options:

```text
--timeout 15s
--until process-running
--until window-created
--until window-visible
--until active
--until exited
```

This avoids brittle sleeps in agent workflows.

---

# 20. Screenshot Capture

Screenshot support is a core capability.

## Desktop/Monitor Screenshot

```bash
agent-desktop screenshot \
  --output /tmp/screen.png
```

## Window Screenshot

Preferred interface:

```bash
agent-desktop screenshot \
  --window win_01 \
  --output /tmp/game.png
```

or:

```bash
agent-desktop screenshot \
  --pid 41220 \
  --output /tmp/game.png
```

JSON:

```json
{
  "ok": true,
  "result": {
    "path": "/tmp/game.png",
    "width": 1600,
    "height": 900,
    "window_id": "win_01"
  }
}
```

---

# 21. Screenshot Implementation

Preferred hierarchy:

```text
1. capture target directly through portal/source selection if available
2. capture selected monitor/surface through PipeWire
3. crop using trusted KWin geometry metadata
```

The CLI should clearly expose whether a screenshot was:

```text
native window capture
or
monitor capture + crop
```

in JSON metadata.

Example:

```json
{
  "capture_method": "monitor_crop"
}
```

---

# 22. Screenshot Output Requirements

- PNG support is mandatory.
- The command must return only after the file is fully written.
- File dimensions should be reported.
- Errors should distinguish:
  - permission failure,
  - no capture source,
  - target not found,
  - PipeWire failure,
  - encoding failure.

Optional future support:

```text
webp
raw RGBA
stdout binary mode
```

---

# 23. Keyboard Input

## Single Key

```bash
agent-desktop key Enter
```

Modifiers:

```bash
agent-desktop key Ctrl+S
agent-desktop key Alt+F4
agent-desktop key Shift+Tab
```

## Hold

Important for game testing:

```bash
agent-desktop key W --hold 1.5s
```

Semantics:

```text
keydown W
wait 1.5s
keyup W
```

## Explicit Down/Up

```bash
agent-desktop key down W
agent-desktop key up W
```

This allows combinations:

```bash
agent-desktop key down Shift
agent-desktop key down W
sleep 1
agent-desktop key up W
agent-desktop key up Shift
```

The tool must track held keys and attempt recovery on failure.

---

# 24. Text Entry

```bash
agent-desktop type "hello world"
```

Options:

```text
--interval 20ms
--literal
```

Text entry should be separate from raw keyboard events so the backend can select the most appropriate mechanism.

---

# 25. Pointer Input

## Move

```bash
agent-desktop pointer move 900 500
```

Coordinate systems should be explicit.

Default recommendation:

```text
global logical desktop coordinates
```

Window-relative coordinates:

```bash
agent-desktop pointer move \
  --window win_01 \
  400 300
```

This should translate:

```text
window-relative
→
global desktop coordinates
```

using KWin geometry.

---

# 26. Pointer Click

```bash
agent-desktop pointer click 900 500
```

Options:

```text
--button left
--button right
--button middle
--count 2
```

Window-relative:

```bash
agent-desktop pointer click \
  --window win_01 \
  400 300
```

---

# 27. Pointer Scroll

```bash
agent-desktop pointer scroll --vertical -3
```

Optional:

```bash
agent-desktop pointer scroll --horizontal 2
```

---

# 28. Input Reset

If an agent crashes while holding a key/button, recovery must be possible.

```bash
agent-desktop input reset
```

Should attempt to:

- release all tracked keys,
- release all tracked mouse buttons,
- clear local input state.

This command should be safe to run repeatedly.

---

# 29. Graceful Close

```bash
agent-desktop close --pid 41220
```

or:

```bash
agent-desktop window close --window win_01
```

Preferred order:

```text
request normal window close
↓
wait
↓
request normal process termination if needed
↓
return timeout if still alive
```

Option:

```bash
--timeout 5s
```

---

# 30. Force Kill

```bash
agent-desktop kill --pid 41220
```

This should be explicit and separate from graceful close.

The agent should use:

```text
close
```

first.

---

# 31. KWin Integration

The KDE backend should use KWin for operations that are compositor/window-manager concerns.

Target capabilities:

- enumerate windows,
- window title,
- application ID,
- PID,
- geometry,
- active state,
- fullscreen state,
- visibility,
- desktop/workspace,
- output/monitor association,
- focus/activation,
- close requests.

Implementation options may include:

```text
KWin scripting
KWin D-Bus interfaces
KWin-provided APIs
```

The exact mechanism should be chosen based on current API stability and security behavior.

The public CLI should remain independent of the chosen mechanism.

---

# 32. KWin Helper Architecture

If KWin scripting is required, the backend may install a small script/helper.

Conceptually:

```text
agent-desktop
      │
      ▼
backend-kde
      │
      ▼
local KWin integration
      │
      ├── enumerate windows
      ├── query geometry
      ├── query PID
      ├── activate
      └── close
```

Communication should remain local to the user session.

---

# 33. Portal Integration

Capture and input should prefer standard desktop portals:

```text
ScreenCast portal
RemoteDesktop portal
```

This provides:

- explicit user permission,
- standard Wayland integration,
- PipeWire capture,
- EIS/libei input,
- potential persistence/restore behavior.

The KDE backend should avoid bypassing portal security where a standard path exists.

---

# 34. Persistent Authorization

A major requirement for agent use is:

> after the user explicitly authorizes the trusted tool, routine agent tests should not require repeated GUI confirmation.

The backend should therefore support:

- portal restore tokens where available,
- KDE permission-store pre-authorization,
- stable application identity,
- diagnostic visibility into permission state.

`doctor` should report whether unattended operation is likely to succeed.

---

# 35. Stable Application Identity

Persistent permissions may depend on identifying the application consistently.

Therefore the tool should have a stable installed identity.

Possible packaging:

```text
/usr/bin/agent-desktop
```

and, if needed:

```text
desktop/application ID:
dev.agentdesktop.AgentDesktop
```

The permission model should not depend on random temporary paths.

---

# 36. `doctor`

```bash
agent-desktop doctor
```

This is essential for agent reliability.

Checks should include:

```text
KDE Plasma session
Wayland session
KWin available
user D-Bus session
xdg-desktop-portal
xdg-desktop-portal-kde
PipeWire
ScreenCast portal
RemoteDesktop portal
EIS/libei support
KWin window integration
persistent authorization status
runtime directory
control socket
```

Example:

```text
KDE Plasma                 OK
Wayland                    OK
KWin                       OK
D-Bus user session         OK
xdg-desktop-portal         OK
xdg-desktop-portal-kde     OK
PipeWire                   OK
ScreenCast                 OK
RemoteDesktop              OK
KWin window API            OK
Persistent authorization   OK
Desktop session            inactive
```

JSON mode should expose every check individually.

---

# 37. Machine-Readable Output

Every command should support:

```text
--json
```

Success:

```json
{
  "ok": true,
  "result": {}
}
```

Failure:

```json
{
  "ok": false,
  "error": {
    "code": "WINDOW_NOT_FOUND",
    "message": "No window matched PID 41220"
  }
}
```

Suggested error codes:

```text
INVALID_ARGUMENT
SESSION_UNAVAILABLE
PERMISSION_DENIED
PORTAL_UNAVAILABLE
PIPEWIRE_ERROR
WINDOW_NOT_FOUND
PROCESS_NOT_FOUND
INPUT_UNAVAILABLE
CAPTURE_UNAVAILABLE
TIMEOUT
UNSUPPORTED_OPERATION
INTERNAL_ERROR
```

---

# 38. Exit Codes

Suggested:

```text
0   success
1   generic failure
2   invalid arguments
3   permission unavailable
4   target not found
5   timeout
6   session unavailable
7   unsupported operation
8   capture/input backend failure
```

Agents should not need to parse prose.

---

# 39. Global Flags

Common flags:

```text
--json
--quiet
--verbose
--timeout DURATION
--session SESSION_ID
--backend kde
--help
--version
```

---

# 40. Configuration

The tool should work without a config file.

Optional location:

```text
~/.config/agent-desktop/config.toml
```

Example:

```toml
backend = "kde"
default_timeout = "10s"
screenshot_format = "png"

[session]
restore = true

[input]
release_on_exit = true
```

Project-local configuration may later be supported.

---

# 41. Runtime State

Transient state:

```text
$XDG_RUNTIME_DIR/agent-desktop/
```

Example:

```text
$XDG_RUNTIME_DIR/agent-desktop/
├── session.json
├── processes.json
├── input-state.json
└── control.sock
```

Persistent state:

```text
$XDG_STATE_HOME/agent-desktop/
```

Configuration:

```text
$XDG_CONFIG_HOME/agent-desktop/
```

No secrets should be stored.

---

# 42. Daemon / Session Helper

Portal and PipeWire sessions are stateful.

A lightweight per-user helper may therefore be useful:

```text
agent-desktop CLI
       │
       ▼
Unix socket
       │
       ▼
agent-desktop-session
       │
       ├── RemoteDesktop session
       ├── ScreenCast session
       ├── PipeWire stream
       └── input state
```

Possible socket:

```text
$XDG_RUNTIME_DIR/agent-desktop/control.sock
```

The helper should:

- run only for the current user,
- accept local Unix socket connections,
- terminate when no longer needed,
- not listen on TCP by default.

---

# 43. Security Model

Desktop automation is privileged.

Requirements:

- Respect KDE/Wayland security boundaries.
- Use portals for capture/input.
- Use KWin APIs only for documented window-management operations.
- Do not use compositor exploits.
- Do not expose remote control over TCP by default.
- Store local state with restrictive permissions.
- Keep persistent authorization explicit.
- Never claim a permission exists unless confirmed.
- Avoid capturing unrelated desktop content when a narrower target is available.

A later policy layer may support allowlists:

```toml
[policy]
allowed_processes = [
  "mygame",
  "test-app"
]
```

but this is not required for the MVP.

---

# 44. Agent Safety Behavior

Recommended agent rules:

1. Prefer targeting by PID/window ID.
2. Avoid interacting with unrelated windows.
3. Prefer application-native test APIs where available.
4. Use desktop screenshots only when needed.
5. Close applications gracefully.
6. Use `kill` only after graceful close fails.
7. Always release held keys/buttons.
8. Keep desktop interactions minimal.

---

# 45. Relationship to `game-cli`

For game development, this tool should complement rather than replace a game-native control CLI.

```text
game-cli
    │
    ├── scene setup
    ├── player/entity state
    ├── deterministic simulation
    ├── semantic actions
    ├── logs/events
    └── renderer-native screenshots

agent-desktop
    │
    ├── launch/focus window
    ├── OS-level screenshots
    ├── keyboard/mouse
    ├── fullscreen/window checks
    └── process/window lifecycle
```

Preferred hierarchy:

```text
unit tests
   ↓
integration tests
   ↓
game-cli
   ↓
renderer-native screenshot
   ↓
agent-desktop
```

Desktop automation should be the last layer, not the first.

---

# 46. Recommended Agent Workflow

Example graphical smoke test:

```bash
cargo test
cargo build
```

Launch:

```bash
RESULT="$(
  agent-desktop launch \
    --wait-window \
    --json \
    ./target/debug/mygame
)"
```

Extract PID/window ID:

```bash
PID="$(jq -r '.result.pid' <<<"$RESULT")"
WIN="$(jq -r '.result.window_id' <<<"$RESULT")"
```

Capture:

```bash
agent-desktop screenshot \
  --window "$WIN" \
  --output /tmp/start.png
```

Interact:

```bash
agent-desktop window focus --window "$WIN"
agent-desktop key W --hold 1s
```

Capture again:

```bash
agent-desktop screenshot \
  --window "$WIN" \
  --output /tmp/after-move.png
```

Close:

```bash
agent-desktop close \
  --pid "$PID" \
  --timeout 5s
```

If required:

```bash
agent-desktop kill --pid "$PID"
```

---

# 47. Agent Skill Integration

A coding-agent skill should teach the workflow.

Suggested:

```text
skills/
└── graphical-app-testing/
    ├── SKILL.md
    ├── references/
    │   └── agent-desktop.md
    └── scripts/
        └── smoke-test.sh
```

The skill should instruct agents to:

- run non-graphical tests first,
- prefer application-native test APIs,
- use `agent-desktop` only when graphical verification is required,
- identify targets by PID/window ID,
- capture before and after interactions,
- keep inputs minimal,
- always shut down launched applications.

---

# 48. Backend Abstraction

Even though KDE is the first target, the implementation should isolate desktop-specific behavior.

Suggested Rust structure:

```text
src/
├── cli/
├── process/
├── session/
├── capture/
├── input/
├── backend/
│   ├── mod.rs
│   └── kde/
│       ├── kwin.rs
│       ├── portal.rs
│       ├── pipewire.rs
│       └── permissions.rs
└── protocol/
```

Possible backend trait:

```text
DesktopBackend
├── list_windows()
├── get_window()
├── focus_window()
├── close_window()
├── screenshot()
├── send_key()
├── pointer_move()
└── pointer_click()
```

This leaves room for:

```text
backend-kde
backend-hyprland
backend-niri
backend-gnome
```

later.

---

# 49. Implementation Language

Recommended:

## Rust

Reasons:

- good D-Bus support,
- good PipeWire/libei integration options,
- reliable process management,
- static deployment,
- strong CLI ecosystem,
- good type-safe JSON output,
- suitable for a long-running session helper.

Suggested crate boundaries:

```text
agent-desktop-cli
agent-desktop-core
agent-desktop-backend-kde
```

or initially one crate with internal modules.

---

# 50. Testing Strategy

## Unit Tests

Cover:

- CLI parsing,
- target resolution,
- JSON output,
- error mapping,
- key parsing,
- coordinate conversion,
- timeout handling,
- process tracking.

## Integration Test Application

Create a small graphical test app that:

- opens a known window,
- displays text,
- responds to keyboard input,
- responds to pointer clicks,
- changes visible state,
- exits cleanly.

Example:

```text
Agent Desktop Test
-------------------------
Typed: hello

[ Click Me ]

Last key: W
Clicks: 2
-------------------------
```

This makes it possible to verify:

```text
launch
window discovery
focus
screenshot
type
key
pointer
close
```

without depending on the actual game.

---

# 51. MVP Scope

Recommended MVP commands:

```text
agent-desktop doctor

agent-desktop session start
agent-desktop session status
agent-desktop session stop

agent-desktop launch
agent-desktop wait
agent-desktop close
agent-desktop kill

agent-desktop windows
agent-desktop window get
agent-desktop window focus

agent-desktop screenshot

agent-desktop key
agent-desktop type
agent-desktop pointer move
agent-desktop pointer click

agent-desktop input reset
```

---

# 52. MVP Acceptance Criteria

The MVP is successful when, after required KDE authorization has been configured, an autonomous coding agent can:

```text
1. Verify desktop-control prerequisites.
2. Start/reuse a desktop-control session.
3. Launch a graphical test application.
4. Obtain its PID.
5. Discover its KWin window.
6. Focus that window.
7. Capture a screenshot.
8. Type text.
9. Press and hold a key.
10. Click a window-relative coordinate.
11. Capture a second screenshot.
12. Gracefully close the application.
13. Confirm that it exited.
14. Reset any remaining held input.
```

The complete flow must be executable through shell commands.

---

# 53. Phase 2

Add:

- scroll input,
- richer KWin metadata,
- workspace/output queries,
- fullscreen detection,
- target-specific screenshot improvements,
- persistent session restoration,
- permission diagnostics,
- multiple active application tracking,
- normalized coordinates,
- scenario runner.

---

# 54. Phase 3

Potential later capabilities:

- MCP wrapper,
- semantic accessibility-tree inspection,
- region screenshots,
- frame streaming,
- video capture,
- visual diff helpers,
- multiple backend support,
- KDE-specific virtual desktop management,
- dedicated agent policies,
- remote worker integration.

---

# 55. Open Questions

The prototype should answer:

1. Which KWin API is most stable for window enumeration and focus?
2. How reliably can PID-to-window correlation be maintained?
3. Can portal sessions be restored across login sessions?
4. How does KDE pre-authorization interact with restore tokens?
5. Can individual windows be captured directly through the portal backend?
6. If monitor capture + crop is required, how accurate are KWin logical coordinates relative to PipeWire frame coordinates under scaling?
7. How should mixed-DPI multi-monitor coordinates be represented?
8. Which libei/EIS path is most reliable for input injection?
9. Does input injection require the target to be focused?
10. What happens to held input when the session helper crashes?
11. Can all required functionality run without a permanent KWin script?
12. What identity does KDE use for persistent remote-desktop permissions?

These questions should be resolved experimentally before overbuilding the abstraction.

---

# 56. Recommended Initial Milestones

## Milestone 1 — KWin Window Control

Implement:

```text
doctor
windows
window get
window focus
```

Success criterion:

> The tool can reliably map a launched process to a KWin window and inspect/focus it.

## Milestone 2 — Capture

Implement:

```text
session start
session status
screenshot
```

Success criterion:

> An authorized tool can capture the desktop and a selected application target to PNG.

## Milestone 3 — Input

Implement:

```text
key
type
pointer move
pointer click
input reset
```

Success criterion:

> The tool can interact with the graphical integration test app without human input.

## Milestone 4 — Lifecycle

Implement:

```text
launch
wait
close
kill
```

Success criterion:

> An agent can run a complete launch → interact → verify → close lifecycle.

## Milestone 5 — Agent Integration

Create:

```text
skills/graphical-app-testing/SKILL.md
```

and a smoke-test script.

Success criterion:

> Codex CLI, Pi, or another coding agent can run a full graphical smoke test using only the documented interface.

---

# 57. Final Recommendation

Use KDE Plasma as the first desktop backend, but keep the public tool generic.

Preferred architecture:

```text
                     Coding Agent
                          │
                          ▼
                   agent-desktop
                          │
                 desktop-independent API
                          │
                          ▼
                    backend-kde
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
      KWin          XDG RemoteDesktop    ScreenCast
   window control       + libei          + PipeWire
```

KDE is especially suitable because it provides:

- standard Wayland portal APIs for capture/input,
- rich compositor-level window metadata,
- explicit window-management capabilities,
- and a path toward persistent/pre-authorized desktop control.

For game development, the intended stack becomes:

```text
                    Coding Agent
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
      build/tests      game-cli      agent-desktop
                         │              │
                  game semantics      KDE desktop
                         │              │
                         └──────┬───────┘
                                ▼
                               Game
```

`game-cli` should handle deterministic gameplay testing.

`agent-desktop` should handle the operating-system and window layer.

Together they provide a strong foundation for autonomous graphical development on Linux without forcing agents to rely entirely on fragile screenshot-and-mouse automation.
