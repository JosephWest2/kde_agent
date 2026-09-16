# Issue #11: retain pinned kdotool for the window transport

The real private-service probes passed on the target Arch/KDE machine on
2026-09-16. Retain kdotool revision
`be03ce90c09350898556436bac74ed35fe928617` unchanged for the proposed window
composition: structured native-window metadata, verified activation, vanished
UUID rejection, ordered custom JSON results and bounded owned cleanup all worked.
No custom result bridge, dependency patch/update, global installation, host desktop
operation or excluded tool was used. This is feasibility evidence, not production
readiness, application support or the twenty-run requirement.

## Selected runs and reproducibility

Run the `functional`, `faults` and `latency` commands in
[PRIVATE_HARNESS.md](../../docs/PRIVATE_HARNESS.md#structured-windowfocus-feasibility-probe-11),
repeating latency in three fresh private generations. All five selected runs used
the same final probe/query sources and ran sequentially. The fixture and harness
are unchanged from #10. The executable is the exact [#9 artifact](../issue-9/README.md),
SHA-256 `62e7ee53096d933ec29e8e5d439b895590f851a40f0dcd87b87db9a6dc4749de`.

| Scenario | Total run | Complete service shutdown | Evidence |
| --- | ---: | ---: | --- |
| Discovery, encoding/null checks, focus, ambiguity, vanished UUID | 3.467s | 0.193s | [functional.json](functional.json) |
| Failed/slow scripts, native timeout, stopped registered CLI | 7.378s | 0.102s | [faults.json](faults.json) |
| Query latency and result ordering, generation 1 | 5.787s | 0.198s | [latency-1.json](latency-1.json) |
| Query latency and result ordering, generation 2 | 6.025s | 0.154s | [latency-2.json](latency-2.json) |
| Query latency and result ordering, generation 3 | 5.660s | 0.133s | [latency-3.json](latency-3.json) |

Every selected run and development attempt independently observed an empty/removed
owned cgroup and removed runtime. [runs.json](runs.json) indexes all ten attempts,
including the one failed expectation. Full durable raw scripts, stdout/stderr and
per-query receipts remain in `.local/harness-runs/<generation>/`. Checked-in
records include complete selected operation receipts, every latency sample,
representative complete ordering output plus every ordering output hash, actual
fixture presentation/commit events, fault scripts/output, dependency linkage and
harness cleanup manifests. Only checkout paths are replaced with `<checkout>`;
protected environment values in the manifests are the harness-constructed private
environment, not a dump of inherited variables or credentials.

Target packages were KWin `6.7.5-1`, systemd `261.3-1`, D-Bus `1.16.2-1`, Wayland
`1.26.0-1`, wayland-protocols `1.49-1`, libxkbcommon `1.13.2-1`, and gcc
`16.2.1+r23+gd564253eb6c8-1`. Python/native/Cargo baseline provenance remains #9.
Installed KWin header hashes are in the probe reports. Final implementation hashes:

- `tools/kdotool_probe.py`: `948b32e7c647607bde1d979900137d0e06265b820ddd10f04a23c7aa6ac5c46b`
- `tools/kwin_window_query.js`: `2829c59538fcd57f95a43ae3f5fd605a135a65e2c42fee3708fb27e2f4f8dc20`

## Metadata, observed focus and identity

The custom-rendered fixture was discovered through KWin with no accessibility
metadata. Its reported PID matched the harness-owned live fixture process; the
snapshot included its KWin UUID, title `KDE Agent Native Fixture`, class
`org.kde_agent.fixture`, active UUID/state, and actual global client rectangle
`x=320, y=180, width=640, height=360`. Fixture commit events independently record
640×360 buffers and exact presented revisions. The separate `frameGeometry`
rectangle was also `(320,180,640,360)` because this fixture has no decorations.
The implementation reads `clientGeometry` and never infers it from frame values;
this run does not establish decorated-application geometry support.

A second ordinary owned fixture deliberately had the same title/class. Its distinct
live PID and UUID provided two candidates; titles did not choose a window. Ten
requested primary/secondary focus transitions each required a fresh snapshot
matching the target UUID, taking 22.376–61.557ms. A successful no-op activation
while the other fixture was active reached the 2s deadline and failed. After the
second fixture closed and was reaped, its formerly valid UUID disappeared and
focus returned `target_missing`; the primary remained healthy and could be
focused again. The second instance is a fault fixture, not production multi-app
support.

A fixed synthetic metadata path used the same normalization functions to prove
explicit nulls; actual required fixture identity/content bounds were available.
A JSON-encoded dynamic payload containing quotes, backslash, newline, backticks,
`${...}`, non-ASCII and U+2028 round-tripped exactly without code execution. These
are explicitly serialization checks, not fabricated compositor metadata. Snapshot
validation also rejects duplicate UUIDs, nonfinite/invalid geometry, missing
fields and inconsistent focus identity.

## Repeated latency and ordering

[summary.json](summary.json) aggregates three generations without dropping warmup,
outliers or failed samples. Full caller-measured durations include admission,
kdotool process execution/reap, parsing, independent named-script absence checks,
temporary cleanup, receipt/report writes and return to the caller. Per-operation
`seconds` ends before its diagnostic file writes; the separate full-call arrays
are the basis of the following latency claims.

| Samples | Median | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| 300 sequential full queries | 17.066ms | 21.733ms | 25.063ms | 27.760ms |
| 90 queries at 100ms cadence | 23.497ms | 31.400ms | 34.635ms | 34.635ms |

There were zero failed queries and zero paced queries exceeding 100ms. The 87
within-generation start intervals ranged 100.020–100.282ms (median 100.074ms).
Sixty independent custom-script invocations emitted 64 numbered JSON records
each: all 3,840 records, payloads, final records and their exact output order were
verified. This tests result transport ordering; window rows are separately sorted
by UUID and make no claim about KWin's enumeration order.

These measurements support **100ms** proposed polling cadence, a **500ms** query
work budget and **2s** total focus deadline in the recorded environment. The
cadence leaves headroom above observed full query costs; overdue queries fail
instead of continuing with stale state. A provisional detection window is the
poll delay plus query deadline (**600ms**), not instantaneous compositor targeting
or a hard real-time guarantee under a stalled kernel/filesystem.

## Faults, native limits and cleanup

Each invocation records a unique generation-scoped script name and owned PID.
The private-bus observer checks `isScriptLoaded` before work, after native cleanup
where applicable, and after forced cleanup. A timed-out CLI is killed/reaped;
only its exact script is removed through kdotool when it remains loaded. A second
independent observation must prove absence. Per-invocation private TMPDIRs make
abandoned generated files attributable and removable without deleting another
query's files. All ten final fault/recovery operations confirmed script absence,
process reap and temporary-directory removal.

| Fixed fault | Request completion/detection | Final observed cleanup | Result |
| --- | ---: | ---: | --- |
| Thrown script error | 4.611ms | 4.903ms | Native error, script already unloaded |
| Syntax error preventing callback | 500.104ms | 502.710ms | External timeout, process reaped |
| Missing expected JSON | 14.124ms | 14.764ms | Invalid result |
| Malformed JSON | 16.007ms | 16.698ms | Invalid result |
| Finite 750ms compositor script, 100ms work budget | 100.042ms | 766.676ms | External timeout, named script forcibly unloaded |
| Suppressed completion callback | 5,009.341ms | 5,010.143ms | Verified native timeout diagnostic and native unload |
| SIGSTOP after observed registration, 200ms work budget | 200.101ms | 211.366ms | Stalled CLI killed/reaped and named script unloaded |

The final observed cleanup column is elapsed time from admission through cleanup,
not additional time after failure detection. Forced cleanup has a separate
**1.5s** reserve, enforced with a shared absolute deadline; the longest measured
cleanup phase was **666.634ms** after the 100ms slow-script deadline. A successful
query/focus is never inferred from a successful process exit alone. Failed or
uncertain cleanup fails the private probe rather than allowing more work.

The pin's internal calls and completion wait each have their own 5s timeouts.
The native-timeout test therefore uses an explicitly separate 12s outer budget;
ordinary polling still uses 500ms. A syntax error does not execute the completion
marker and reaches the external deadline. The initial probe incorrectly expected
an immediate script-error callback for that case; its [failed run](initial-syntax-expectation-failure.json)
retains the actual timeout, diagnostics and successful cleanup. The expectation
was corrected to the observed failure mechanism, without relaxing the work bound.
Other development runs passed and are indexed with their actual source hashes;
one preliminary latency generation overlapped an independent fault run, so the
selected performance measurements use the final sequential generations.

The 750ms script is deliberately finite: killing a CLI cannot preempt KWin's
already-running JavaScript. Normal query admission/error detection and eventual
script cleanup are distinct bounds. Fresh queries succeeded after slow, native
timeout and stopped-process cleanup. No infinite loop or host process signaling
was used.

## Remaining integration work

Retain kdotool with an externally bounded, asynchronously observed process owner;
its native timeouts alone are insufficient for polling. No measured blocker
requires a pin change, maintained patch or custom window bridge here. The
feasibility helper itself is synchronous and must not be copied into a GLib
callback: #13 must prove that slow query and cleanup work cannot stall control or
cancellation. #12 must prove libei event handling and actual input release before
any final cancellation/release bound is claimed. Screenshot readiness, full
REQ-009 session readiness, production lifetime/failure integration, representative
applications and twenty consecutive full workflows remain outstanding.

All **26 unit tests passed** (0.914s), including eight focused metadata, encoding,
private-context, vanished-target, exit-versus-focus and late-focus tests. Existing
#9/#10 tests also passed, and `git diff --check` passed. No fixture/harness source
or build parameters changed, so their existing strict compiler evidence applies.
The real runs above provide the compositor evidence that unit tests cannot.
