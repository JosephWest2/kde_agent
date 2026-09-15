# Agent Desktop Toolkit — Requirements

## Overall goal

Give coding agents and developers a small, dependable local toolkit for testing
graphical applications in a separate desktop. The toolkit lets them launch an
application, identify its windows, send basic input, inspect screenshots, and
clean up the session without taking over the user's working desktop.

Reuse existing tools wherever that reduces total implementation and maintenance
work. Builds and ordinary tests stay in the normal project environment; the
toolkit runs the resulting native application or game. The first version uses a
headless KWin desktop and saved screenshots. Live viewing can be added later as
an optional way to observe that desktop.

## Status and requirement conventions

This document defines the proposed product contract. [ARCHITECTURE.md](ARCHITECTURE.md)
proposes how to satisfy it. An implementation plan will follow these documents.
The requirements describe intended behavior, not capabilities already delivered.

- **MUST**: required for the first supported version, unless explicitly conditional.
- **SHOULD**: expected, but an exception may be made with a documented reason and
  its consequences.
- **MAY**: optional capability; its inclusion is not a delivery commitment.

IDs are permanent references. Do not renumber or reuse them when requirements
move, change priority, or are removed. Preserve a retired ID with a retirement
note; allocate the next unused ID for a new requirement. Substantial changes to
meaning should retire the old requirement and introduce a new one.

Terminology:

- A **session** is one toolkit-owned desktop and its associated processes.
- A **generation** is the immutable identity of one session lifetime. Restarting
  a session with the same human-readable name creates a new generation.
- An **application** is a directly launched program and its tracked descendants.
- A **window** is a compositor-reported window, including an application's dialogs.
- A **bounded** operation has a finite, documented deadline enforced by the
  process performing the work. Timing limits and their tested conditions must be
  stated before the implementation is declared supported.

## MUST requirements

### Scope and interface

| ID | Requirement |
| --- | --- |
| REQ-001 | The toolkit **MUST** provide a dedicated headless KWin desktop with one 1280×720 output at scale 1 as the initial supported configuration. Screenshot operation must work without a live viewer. |
| REQ-002 | The initial supported workload **MUST** include one directly launched native Wayland application and its dialogs per session on the target Arch Linux/KDE Wayland machine. Support claims must identify tested dependency versions and applications. |
| REQ-003 | The toolkit **MUST** expose its required operations through a local CLI usable by shell scripts and coding agents without depending on a particular agent product. After prerequisite setup, supported operations must run without interactive prompts. |
| REQ-004 | Every command **MUST** offer machine-readable JSON results, including failures, and meaningful exit status. In JSON mode, stdout must contain only the result; diagnostics and application output must use separate streams or files. |
| REQ-005 | The toolkit **MUST** provide a prerequisite diagnostic command that reports missing or incompatible dependencies and unsupported capabilities with actionable errors. Blocking commands must have finite deadlines; unsupported operations must fail explicitly. |

### Desktop separation and session lifetime

| ID | Requirement |
| --- | --- |
| REQ-006 | Desktop operations and launched applications **MUST** use the created session's display, D-Bus, runtime, and HOME/XDG settings. Host display and accessibility endpoints must not leak into application environments, user variables must not override protected endpoints, and input/capture must never fall back to the personal desktop. Runtime control files and sockets must be accessible only to their owner. |
| REQ-007 | The toolkit **MUST** support running trusted local builds with access to their project files and document that desktop/settings separation is not filesystem or network isolation. It must not require moving the build workflow into the session. |
| REQ-008 | A session and its necessary desktop connections **MUST** survive between separate CLI invocations until stopped or failed. |
| REQ-009 | Session start **MUST** report readiness only after control connectivity, window queries, input-device readiness, and a screenshot probe succeed. Startup failure must trigger bounded cleanup and preserve diagnostics. |
| REQ-010 | Starting an already running compatible session **MUST** return that session's identity; conflicting settings must produce an error. Stopping an already stopped session must succeed. |
| REQ-011 | Session status **MUST** reflect live helper and essential dependency health, rather than trusting saved state alone. Unexpected helper, compositor, or private-bus failure must make the session unavailable, reject further desktop work, and trigger bounded cleanup. |
| REQ-012 | Session stop **MUST** attempt input release and graceful application closure, then terminate remaining owned processes within a finite shutdown deadline. Cleanup must cover ordinary descendants and remain possible when the helper is unresponsive or has crashed. Artifacts must survive cleanup. |
| REQ-013 | The toolkit **MUST** return a unique session-generation token, accept an expected generation on every session-addressed command, and reject a mismatch before side effects. Application and window references must include their generation. Name-only lookup must be documented as resolving the current generation, rather than protecting against a restart. |

### Applications and windows

| ID | Requirement |
| --- | --- |
| REQ-014 | Launch **MUST** accept an argument vector, working directory, and explicit application environment overrides. Documentation must define executable/PATH resolution and environment precedence. Relative working-directory, artifact, and output paths must resolve from the calling CLI's directory, independent of the helper's directory. |
| REQ-015 | Once a process is launched, the toolkit **MUST** return or retain its application ID, process identity, log paths, and discovered window references. A bounded window wait must distinguish application exit from timeout and retain the application handle when it times out. |
| REQ-016 | The toolkit **MUST** track ownership and process lifetime before signaling an application. It must not signal an unrelated process because a PID was reused. |
| REQ-017 | Window discovery **MUST** work for custom-rendered windows without accessibility metadata and return generation-scoped KWin identity, reported PID, title, application class, client bounds, and focus state. Unavailable metadata must be represented explicitly. |
| REQ-018 | Window selection **MUST** use explicit identity or unambiguous association with an owned application. Titles must be descriptive rather than authoritative identity. Ambiguous selection must return candidates and require an explicit choice; vanished targets must produce an error. |
| REQ-019 | An explicit focus operation **MUST** verify that the requested window actually became active within its deadline before reporting success. |
| REQ-020 | The toolkit **MUST** support bounded waits for an application's window, a window's focus, and an application's exit, distinguishing those conditions from timeout or session failure. |
| REQ-021 | Graceful close **MUST** request normal window closure and report whether the application exited. It must not automatically dismiss confirmation dialogs or escalate a close timeout into a signal. |
| REQ-022 | The toolkit **MUST** provide a separate explicit termination operation for an owned application and report whether the tracked application processes exited. |

### Input and cancellation

| ID | Requirement |
| --- | --- |
| REQ-023 | Every keyboard or pointer action **MUST** name a window and verify its existence and focus before sending input. This is a checked focus-based operation, not a promise of atomic compositor-level targeting. |
| REQ-024 | Ordinary actions within a session **MUST** be serialized. During held or repeated input, the toolkit must recheck target existence and focus at documented bounded intervals, cancel further input upon detecting loss, and attempt release of held input. Instantaneous focus-loss detection is not required. |
| REQ-025 | Keyboard input **MUST** support documented physical key mappings, chords, and finite key holds, including common game keys. The initial interface must not allow indefinite key-down state across commands. |
| REQ-026 | Text entry **MUST** support a documented US-layout character set. Complete text and chord requests must be validated before emitting input; unsupported characters or keys must be rejected rather than skipped or partially typed. |
| REQ-027 | Pointer clicks **MUST** use client-content coordinates, with `(0, 0)` at the content's top-left excluding decorations. Each click must validate bounds and translate coordinates using current target geometry. |
| REQ-028 | Cancellation **MUST** remain serviceable during an active action. CLI interruption/disconnection and worker-enforced timeout must cancel the corresponding unfinished ordinary action. Input emission must stop and release must be attempted within a documented cancellation bound, without waiting for the normal action queue. Required session cleanup must continue independently of the client. |
| REQ-029 | The toolkit **MUST** track the keys and buttons it presses and attempt their release on completion, cancellation, target loss, and failure. If connection loss prevents confirming release, it must report uncertainty and block further input until the connection is safely reset or the session is stopped. |
| REQ-030 | The input backend **MUST** wait for usable, resumed devices and continue processing device pause, removal, and connection-loss events throughout the session. Input on an unusable device must fail explicitly. |
| REQ-031 | An input-reset operation **MUST** release tracked input or replace the private input connection, reporting readiness only after the resulting connection is usable. Failed reset must leave input unavailable. |

### Observation, results, and artifacts

| ID | Requirement |
| --- | --- |
| REQ-032 | Screenshot capture **MUST** request a fresh image of the whole dedicated output and report a complete PNG's path, dimensions, capture timestamp, backend, and session generation. It must not report success before writing finishes or reuse an earlier image as a fresh result. |
| REQ-033 | Results and documentation **MUST** distinguish successful input dispatch from acknowledgment by the application. The toolkit must not imply that action completion guarantees a rendered frame or generic UI readiness. |
| REQ-034 | Application output, helper/compositor diagnostics, screenshots, and a session manifest **MUST** be stored outside temporary session settings and survive stop or failed startup. Artifacts must be attributable to a generation, and generated filenames must not collide. The manifest must record launch arguments, working directory, mode, output configuration, dependency versions, and final session outcome. |
| REQ-035 | Failures **MUST** use stable error codes with useful context, distinguishing prerequisites, session failure, unsupported input/operation, missing or ambiguous target, application exit, cancellation, timeout, input failure, and capture failure. Uncertain side effects must be identified; launch and input must not be automatically retried when completion is unknown. |

### Dependency management and validation

| ID | Requirement |
| --- | --- |
| REQ-036 | Dependency setup **MUST** identify exact tested source revisions or releases, resolved dependencies, native-library versions, and any maintained patches. Known input correctness defects must be resolved before support is claimed. Dependency changes must rerun the affected compatibility checks. |
| REQ-037 | Verification **MUST** exercise the real private KWin desktop through separate CLI invocations: launch, discover, focus, capture, type, hold/release, click, close, and stop. An instrumented application must acknowledge input and show an expected visual change; a mock backend alone is insufficient. |
| REQ-038 | Verification **MUST** cover generation mismatch, ambiguous/vanished targets, unsupported text, focus loss, cancellation during holds and modified typing, input-device failure, startup failure, independent helper/compositor/bus death, and descendant cleanup. It must also check environment separation and invocation from a different working directory. |
| REQ-039 | Before the first version is declared supported, the full CLI workflow **MUST** complete twenty consecutive runs in the recorded target environment without unexpected prompts, stuck input, or leftover owned processes. Failures and environment limitations must be recorded. |
| REQ-040 | User documentation **MUST** provide reproducible setup, a basic workflow, a repeatable smoke check, timing/cancellation semantics, and supported versus deferred capabilities. A native application or game must have a recorded successful run before support for that particular application is claimed. |

## SHOULD requirements

| ID | Requirement |
| --- | --- |
| REQ-041 | The project **SHOULD** reuse existing tools and libraries when doing so reduces total build, integration, testing, and maintenance work. Dependency count alone should not determine the choice. |
| REQ-042 | Project-owned code **SHOULD** concentrate on session ownership, command contracts, targeting, cancellation, and diagnostics. Replacing an existing desktop transport should require evidence of a concrete limitation. |
| REQ-043 | The command model **SHOULD** leave room for optional live viewing while preserving screenshot operation and application-control semantics. This should not require implementing a viewer or a general backend framework in the first version. |
| REQ-044 | The initial validation **SHOULD** include a representative native application or game chosen for the user's development workflow, in addition to deterministic test fixtures. |
| REQ-045 | Diagnostics **SHOULD** correlate requests, application/window references, and artifacts so a failed automation run can be understood without reproducing it immediately. |
| REQ-046 | Initial configuration **SHOULD** use explicit CLI flags and documented defaults. A persistent project configuration format should follow demonstrated usage needs. |

## MAY requirements

| ID | Requirement |
| --- | --- |
| REQ-047 | A later version **MAY** provide optional live viewing of the agent desktop. Screenshots must remain available when live viewing is enabled. Attaching a viewer to an already running headless session is not an initial promise. |
| REQ-048 | A later version **MAY** support X11 applications through a private XWayland instance when a target application requires it, preserving the separation in REQ-006. |
| REQ-049 | Later versions **MAY** add richer pointer actions, Unicode/IME text, additional output configurations, window-specific capture, or multiple active applications after defining and validating their targeting and coordinate behavior. |
| REQ-050 | A later version **MAY** expose the CLI's capabilities through MCP or another integration, while preserving independent local use. |

## Outside the initial scope

The first version does not include personal-desktop control, a complete second
Plasma shell, security isolation through containers or VMs, accessibility/OCR
automation, visual assertions, video streaming/recording, session restoration
across login, concurrent automation clients, or cross-desktop/distribution
support. It does not provide game simulation timing or replace an application's
own testing API. Optional requirements above remain outside first-version
acceptance unless explicitly brought into scope.
