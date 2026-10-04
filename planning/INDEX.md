# Implementation issues

> **Superseded (2026-10-03):** the remaining milestones (M5–M8) were replaced by [REVISED_PLAN.md](REVISED_PLAN.md) and issues #62–#68. This file is kept as history.


See [README.md](README.md) for sequencing and scope and [COVERAGE.md](COVERAGE.md) for requirement mapping.

## [M1: Prove the private KWin stack and record the dependency baseline](https://github.com/JosephWest2/kde_agent/issues/1)

A reproducible, bounded compatibility harness proves the proposed platform adapters on the target Arch Linux/KDE Wayland machine before their interfaces are committed to production.

- [M1.1: Record reproducible native and Python dependency setup](https://github.com/JosephWest2/kde_agent/issues/9)
- [M1.2: Build a bounded private-desktop harness and acknowledged Wayland fixture](https://github.com/JosephWest2/kde_agent/issues/10)
- [M1.3: Validate kdotool structured queries and bounded focus polling](https://github.com/JosephWest2/kde_agent/issues/11)
- [M1.4: Prove the minimal libei binding and device-lifecycle test mechanism](https://github.com/JosephWest2/kde_agent/issues/12)
- [M1.5: Prove ScreenShot2 capture and choose the control-loop integration boundaries](https://github.com/JosephWest2/kde_agent/issues/13)

## [M2: Establish the CLI, generation-safe protocol, and durable records](https://github.com/JosephWest2/kde_agent/issues/2)

The Python CLI and persistent-worker contract provide predictable machine-readable results, identity checks, caller-relative paths, finite deadlines and durable diagnostics.

- [M2.1: Scaffold agent-desktop and define JSON command/error contracts](https://github.com/JosephWest2/kde_agent/issues/14)
- [M2.2: Implement generation-bound Unix-socket request transport](https://github.com/JosephWest2/kde_agent/issues/15)
- [M2.3: Implement the responsive ordinary-action queue and cancellation channel](https://github.com/JosephWest2/kde_agent/issues/16)
- [M2.4: Implement caller-path normalization and durable generation records](https://github.com/JosephWest2/kde_agent/issues/17)

## [M3: Own private sessions with bounded supervision and cleanup](https://github.com/JosephWest2/kde_agent/issues/3)

A generation-bound systemd service owns the worker, private desktop and ordinary descendants, with reliable failure detection and an independent cleanup path.

- [M3.1: Implement generation-owned transient service start and idempotent lifecycle](https://github.com/JosephWest2/kde_agent/issues/18)
- [M3.2: Construct the private desktop and protected application environment](https://github.com/JosephWest2/kde_agent/issues/19)
- [M3.3: Gate readiness on capabilities and monitor essential-process health](https://github.com/JosephWest2/kde_agent/issues/20)
- [M3.4: Implement graceful shutdown orchestration and crash-independent cgroup cleanup](https://github.com/JosephWest2/kde_agent/issues/21)

## [M4: Launch owned applications and control windows by verified identity](https://github.com/JosephWest2/kde_agent/issues/4)

One native Wayland application and its dialogs can be launched, discovered, focused, waited on, closed or explicitly terminated with generation and process-ownership checks.

- [M4.1: Launch argv workloads with durable ownership and a one-active-application limit](https://github.com/JosephWest2/kde_agent/issues/22)
- [M4.2: Implement asynchronous structured KWin window discovery](https://github.com/JosephWest2/kde_agent/issues/23)
- [M4.3: Implement unambiguous target resolution, verified focus and bounded waits](https://github.com/JosephWest2/kde_agent/issues/24)
- [M4.4: Implement graceful window close without implicit escalation](https://github.com/JosephWest2/kde_agent/issues/25)
- [M4.5: Implement explicit termination of verified application processes](https://github.com/JosephWest2/kde_agent/issues/26)

## [M5: Deliver targeted input with bounded cancellation and recovery](https://github.com/JosephWest2/kde_agent/issues/5)

Keyboard and pointer actions use verified window focus, finite sequences, tracked releases and an event-responsive private libei connection.

- [M5.1: Implement the persistent libei connection and resumed-device lifecycle](https://github.com/JosephWest2/kde_agent/issues/27)
- [M5.2: Implement fully validated physical keys, chords, text and finite holds](https://github.com/JosephWest2/kde_agent/issues/28)
- [M5.3: Enforce window focus and current client-coordinate targeting for every input action](https://github.com/JosephWest2/kde_agent/issues/29)
- [M5.4: Guarantee bounded input cancellation and tracked release on every exit path](https://github.com/JosephWest2/kde_agent/issues/30)
- [M5.5: Implement input reset and recovery from uncertain release](https://github.com/JosephWest2/kde_agent/issues/31)

## [M6: Capture fresh complete screenshots and preserve useful artifacts](https://github.com/JosephWest2/kde_agent/issues/6)

The CLI captures the whole private output as a newly written PNG while cancellation and health monitoring remain responsive, and exposes durable correlated artifacts.

- [M6.1: Implement bounded private ScreenShot2 transport and pixel decoding](https://github.com/JosephWest2/kde_agent/issues/32)
- [M6.2: Publish unique complete PNG artifacts and screenshot metadata](https://github.com/JosephWest2/kde_agent/issues/33)
- [M6.3: Wire artifact discovery, correlated logs and complete durable manifests](https://github.com/JosephWest2/kde_agent/issues/34)

## [M7: Integrate the complete workflow and verify failure contracts](https://github.com/JosephWest2/kde_agent/issues/7)

Production adapters are connected to readiness, shutdown and every CLI command; a real-desktop integration suite demonstrates the full normal and failure workflows.

- [M7.1: Connect production adapters to readiness, doctor and shutdown](https://github.com/JosephWest2/kde_agent/issues/35)
- [M7.2: Assemble the real-desktop workflow integration suite](https://github.com/JosephWest2/kde_agent/issues/36)
- [M7.3: Verify lifecycle, identity, environment and target failure contracts](https://github.com/JosephWest2/kde_agent/issues/37)
- [M7.4: Verify cancellation, device faults and capture responsiveness against measured bounds](https://github.com/JosephWest2/kde_agent/issues/38)

## [M8: Qualify the first supported release and publish reproducible workflows](https://github.com/JosephWest2/kde_agent/issues/8)

A recorded target environment passes twenty consecutive full workflows and users have reproducible setup, smoke checks and precise support boundaries.

- [M8.1: Document reproducible setup, CLI workflows and measured support semantics](https://github.com/JosephWest2/kde_agent/issues/39)
- [M8.2: Pass twenty consecutive full CLI workflows in the recorded target environment](https://github.com/JosephWest2/kde_agent/issues/40)
- [M8.3: Record representative application validation and close requirement coverage](https://github.com/JosephWest2/kde_agent/issues/41)
