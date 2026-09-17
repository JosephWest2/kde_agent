# Issue #13 — fresh ScreenShot2 capture and responsive integration

**Pass for M1 feasibility under the recorded conditions.** Eleven selected fresh
private desktops produced 32 complete 1280×720 PNGs. Thirty captures checked all
four fixture colors and all 32 state bits: **1,080 exact pixel assertions**.
Partial/hung captures failed, reclaimed the capture child and stopped their
private desktop with an observed empty cgroup. Slow capture/query work preserved
GLib cancellation and real input-release receipts. This is not production
readiness, application support, or the twenty-run production requirement.

Reproduce the scenarios in [CAPTURE_PROBE.md](../../docs/CAPTURE_PROBE.md).
The consolidated [M1 decision](../../docs/M1_DECISION.md) links all preceding
adapter evidence and records required architecture choices. The user approved
both the individual capture process and failed-private-session policy before
implementation. No host desktop operation, viewer, XWayland, global installation,
excluded tool, native library patch or additional compositor plugin was used.

## Selected runs and provenance

[summary.json](summary.json) includes every selected timing sample and final
source hashes. [runs.json](runs.json) indexes complete checked-in selected raw
receipts, logs, presentation/input events, manifests, PNGs, introspection and
controller cleanup results. It records both original and checked-in file hashes;
only checkout paths in text files are replaced with `<checkout>`. PNG bytes are
unchanged. [all-attempts.json](all-attempts.json) retains all 24 development and
selected desktop attempts, their actual source hashes, outcomes and cleanup.
All observed cgroups were empty after cleanup. Expected capture failures remain
failed operations and failed harness runs; the containment assertions pass.

The final selected source is `tools/capture_probe.py` SHA256
`299ae8c4f3034195d5f59cbc3d3202a6c78d9017efe3bb5784815b9c7a994404`,
with codec SHA256
`57307c987b3ed6bfd067db01d36bf9acfc8f95f2dbcde41afa950a2b3aa1bdfb`.
Every selected capture report matches those hashes; harness/input/query hashes
are recorded in the summary. Input code is unchanged from merged #12 `ddacc3b`,
including the audited native binary gate and immediate negative-setup FD close.

| Scenario | Selected generations | Result |
| --- | ---: | --- |
| `functional` | 3 | Ten captures each, two exact controlled states, rendered input, repeat capture and aggregate adapter readiness sequence. |
| `invalid-screen` | 1 | Exact InvalidScreen rejection before capture; subsequent complete capture succeeds. |
| `partial` | 1 | Actual reader closes after 4,096 bytes; no PNG success; child and session cleanup. |
| `stall-reply` | 1 | Child stopped after request queueing before its reply dispatch; deadline and session cleanup. This does not claim server acceptance was observed. |
| `stall-pipe` | 1 | Child stopped after a positive partial raw read; pipe capacity is far below remaining image bytes; deadline and session cleanup. |
| `stall-encode` | 1 | Child stopped at the encode-stage boundary after exact raw bytes and EOF; independent parent deadline kills it. This is a controlled stage stall, not an observed Pillow defect. |
| `responsive-pipe`, `responsive-encode` | 2 | Real Shift+W acknowledgment, stalled capture, separate cancellation process, actual releases and independent status response before capture finishes; then expected deadline/session stop. |
| `slow-query` | 1 | Existing finite 750ms KWin query stall, 500ms query deadline cancellation while child cleans up, actual release after KWin resumes, then successful capture. |

All selected generations ran sequentially. Normal capture latency includes child
startup, actual request, concurrent drain, metadata/codec checks, PNG encoding,
close/reopen/full decode, publication, child exit/reap and timely parent
acceptance. Ten captures in a functional generation are distinct requests with
unique paths, even where stable fixture state gives identical PNG hashes.

## Metadata, freshness and complete image proof

The actual private service reported ScreenShot2 interface version 5. Every
successful capture returned `type=raw`, `format=6`, `width=1280`, `height=720`,
`stride=5120`, `screen=Virtual-0`, `scale=1.0`. Each yielded exactly **3,686,400
raw bytes and EOF**, followed by a fully decoded PNG. Unknown formats, types,
geometry or stride are rejected, rather than guessed. Qt format 6 is native-word
ARGB32 premultiplied: B,G,R,A bytes on this x86_64 host. Pillow's premultiplied
mode converts to straight RGBA; zero-alpha pixels are explicitly normalized.
Synthetic tests cover alpha, both endian paths, channel order and orientation.
The actual whole-output fixture assertion is opaque; it does not claim tested
transparent application capture or another native architecture.

For states `0x13579BDF` and `0x2468ACE0`, the exact generation/control/revision
presentation receipt precedes a new capture request. Geometry comes from the
live KWin client rectangle. All 32 marker bits and four asymmetric colors are
checked at those coordinates; neither fixture checksum nor changed PNG hash
substitutes for captured pixels. A W press/release is separately acknowledged,
its latest committed revision receives presentation feedback, and that settled
input revision's pixels are then captured. Dispatch, acknowledgment, presentation
and screenshot completion remain separate observations.

A representative complete image is
[functional-1 PNG](runs/functional-1/capture-1bb8895166d349b0b17579fc9a8c483c/image.png).
Its original request, metadata, raw hash, pixel assertions, EOF, completed PNG
hash and parent acceptance timestamps are in the adjacent receipt. Timestamps
are monotonic request/completion observations in this generation, not a claimed
compositor exposure time. The output is captured with cursor disabled and no
viewer.

## Measurements and enforced bounds

| Measurement | Samples | Maximum |
| --- | ---: | ---: |
| Admission through published-PNG parent acceptance | 32 | 126.158ms |
| Local aborted child kill/reap, FD and partial-artifact cleanup | 7 | 5.344ms |
| 3s timeout detection by 5ms GLib timer | 5 | 3.003762s |
| External cancellation through explicit release dispatch during capture | 2 | 0.860ms |
| External cancellation through actual fixture release during capture | 2 | 2.610ms |
| Independent status CLI completion during stalled capture | 2 | 50.396ms |
| Maximum GLib timer gap | 11 runs | 6.295ms |
| Complete service shutdown | 11 | 241ms |
| Failed capture admission through observed empty service cgroup | 6 | 3.319s |
| Conservative aggregate startup/adapters/input/ten-capture sequence | 3 | 1.765s |

Retain **3s capture**, including timely parent acceptance of the fully published
PNG; **1s separate local abort cleanup**; and **15s complete failed-session
shutdown**. A full-timeout failure may therefore take up to 19s through session
cleanup, with each phase clamped to enclosing harness budgets. Success is never
accepted after the 3s deadline. Detection is observed on a 5ms timer and includes
scheduler variation; it is not a claim of hard real-time interruption at exactly
3.000000 seconds. Failed requests and completed cleanup have distinct timestamps.

Input retains #12's independent 100ms dispatch and 500ms fixture-release bounds;
connection/lifecycle/shared reset 3s, holds at most 2s, and #11 100ms focus cadence,
500ms query work with separate 1.5s query cleanup. The finite compositor stall
necessarily delays application delivery while parent GLib remains responsive.
These measurements assume the recorded responsive host/kernel/filesystem.

The aggregate sequence supports a proposed 30s full adapter startup budget with
headroom. The existing harness still labels its earlier bus/fixture stage
`foundation_ready`; it has not acquired a production readiness API. The sequence
proves these adapters on one generation and does not mislabel the earlier phase.

## Ownership and abort policy

The child alone owns capture D-Bus, raw pipe, decode and filesystem work. The
GLib input owner reads bounded status records and observes the child without
waiting in callbacks. Original write FD, UnixFd duplicate and native message
copy are distinct ownership steps. A native dbus-python message test proves
append duplicates the FD, explicit original/wrapper close does not prematurely
close the message copy, and message destruction closes its last writer. Real
replies record no surviving local write copy; exact raw bytes plus EOF proves
normal writer closure. Child exits reclaim all its remaining local descriptors.

KWin's source sends metadata before launching its writer, which polls up to
60 seconds per iteration. The source renders synchronously before that reply;
killing a helper cannot interrupt compositor render work. Read-only inspection
of this owned KWin's `/proc/PID/fd` returned PermissionError. A broken-pipe log
precedes runnable destruction and a fresh capture cannot prove an old task was
reclaimed. Consequently **every unconfirmed aborted capture rejects further
capture work and exits the probe, causing bounded whole-private-service stop**.
No reusable-session cleanup guarantee is claimed. Only exact InvalidScreen is
classified as safe rejection, based on its source path before FD duplication.

The opt-in permission override is constructed solely for private KWin. Unit
coverage exercises actual mocked launch environments with and without the
existing EIS test plugin; real manifests record clean application/probe settings
and the explicit KWin-only capability. The run retains the prior owner-private
bus/runtime, generation identity, independent service lifetime and observed
cgroup cleanup.

## Local failure coverage and retained development correction

All **58 unit/regression tests passed in 1.889s**; see [unit-tests.log](unit-tests.log). Coverage includes
native FD/message ownership, failure before D-Bus queueing, real full raw payload
with a writer kept open (deadline failure without EOF/PNG), forced child reap,
late-success publication rejection, wrong generation/request status, oversized
status output, metadata/payload validation, pixel/alpha/geometry checks and all
preceding dependency/window/input/harness tests.

The first oversized-status test exposed repeated parsing of an over-limit pipe
after abort had already started, preventing final reap/cleanup. The
[original failing log](development-failure/status-cap-test.log) is retained.
The supervisor now skips further status parsing once abort starts, kills/reaps
and removes partial output. All transport tests pass; the full selected desktop
matrix was rerun against that final source. Earlier passing desktop runs remain
indexed as development attempts, rather than silently attributed to final code.
