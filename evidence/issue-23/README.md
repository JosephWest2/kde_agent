# Issue #23 installed structured-window evidence

Selected production source is **`7e4f8d6`**, including both fresh PR review fixes:
full 256-window durable retention and a strict final cleanup-deadline recheck.
All 33 installed Python and one JavaScript resource hashes match current source.
The wheel was built from a clean archive in an isolated directory. Qualification
remains `release_qualified: false`, with replacement issue #35 open.

| Selected run | Tested runner commit | Receipt |
| --- | --- | --- |
| Native discovery, cadence and failures | `76971e2` | [native-review](native-review/receipt.json) |
| Cancellation, EOF, metadata, retirement and restart | `01ba26c` | [controls-review](controls-review/receipt.json) |
| Complete 21-scenario cleanup regression | `8e11df0` | [cleanup-review](cleanup-review/cleanup.json) |

Runner-only commits do not change installed production modules. Exact tested
native/control runner copies accompany the receipts; [provenance.json](provenance.json)
records source, runner and copied-file hashes. Selection inventories preserve
original absolute raw paths and hashes. Historical `native/`, `controls/` and
`cleanup/` are superseded e7d8827 evidence, retained without rewriting raw receipts.

## Native discovery and cadence

Separate installed CLI processes run with clean environments and `/` as caller
cwd. The committed custom-rendered fixture is rebuilt with strict compiler
warnings. Four same-title owned surfaces are distinguished: primary 640×360,
sibling 480×300, parented dialog 320×180 and descendant 400×240. Native configure
receipts match client dimensions. A fifth real infrastructure window remains
unassociated outside the app subgroup; app-filtered discovery returns four.
Snapshots preserve active identity and independent client/frame rectangles.

| Batch | Success / submitted | Worker p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sequential | 100 / 100 | 75.30 ms | 82.80 ms | 83.95 ms | 85.03 ms |
| Starts every 100 ms | 30 / 30 | 83.53 ms | 96.27 ms | 97.96 ms | 100.47 ms |

Worker latency includes admission queue time through adapter acceptance. Durable
terminal-record latency p50/p95/p99/max was 90.23/100.32/102.34/110.72 ms sequential
and 97.93/109.22/111.57/113.89 ms paced. Actual paced start intervals were
99.981–100.070 ms. All 130 terminal outcomes are recorded; five subsequent recovery
queries passed. The normal generation's lifetime GLib maximum was **46.49 ms**,
including startup. Across all native fault generations the maximum was **99.68 ms**
(stalled remover). These are recorded-condition observations, not a guarantee of
10 successful queries per second or future held-input focus performance.

## Failure and control checks

Fault hooks are evidence-only Python; the public protocol exposes no arbitrary
scripts. The missing-completion hook suppresses kdotool's final callback rather
than using an empty script, which would auto-complete.

- Malformed output produces `window_query_failed` without acceptance.
- Missing completion and SIGSTOP after independently observed registration time
  out. Detection overshoot was 2.60/2.72 ms and cancellation-to-cleanup was
  37.40/37.37 ms. Owned children are reaped, exact scripts removed and absence
  checked, pipes closed, and temporary directories removed.
- Finite 800 ms KWin JavaScript times out while the worker remains responsive;
  cleanup completes 354.69 ms after cancellation. Killing kdotool does not claim
  to preempt compositor JavaScript.
- A stalled remover exhausts the shared reserve with `clean: false`, unconfirmed
  absence and incomplete temporary cleanup. The adapter fails closed and the real
  session tears down; complete cgroup emptiness is independently observed. The
  raw uncertain query receipt is preserved.
- Actual priority cancellation and client EOF dispatch in 5.001/4.989 ms, with
  action-to-confirmed-cleanup 38.728/38.615 ms. Status replies take 27.01/28.77 ms.
  The control-query lifetime maximum is 77.29 ms; the **whole controls run maximum
  is 86.75 ms**, including startup and later metadata/disappearance work. Original
  app birth identity/cgroup survive both controls, followed by successful recovery.
- Native empty/omitted metadata expose caption `""` and compositor-default class
  `"wayland-fixture"`; synthetic null handling is separately tested. Native close
  disappears from a fresh snapshot, while the retired app and historical pins
  remain. Old app/generation references after restart reject `generation_mismatch`.

[controls-review/report.md](controls-review/report.md) gives timing scopes. Every
selected native/control generation stops with its complete service cgroup empty.
[Prerequisites](prerequisites.json) record KWin, pinned unpatched kdotool and private
endpoint support. [CLEANUP.md](CLEANUP.md) documents the passing full 21-case matrix,
with maximum fault-to-empty interval 7.707 s and no fallback cleanup.

## Instrumentation and negative observations

The selected native runner writes diagnostic receipts atomically without fsync;
production Store durability is unchanged. The first receipt-write cost has maximum
1.071 ms (it excludes the small following timing-file write). The controls runner
also measures original durable calls in bounded in-memory statistics: maxima
14.28 ms for Store._write, 18.02 ms for Store.window_observation, and 0.246 ms for
lifecycle.atomic. These observations and the standalone [I/O benchmark](diagnostic-io/receipt.json)
do **not** establish the cause of earlier pauses.

[negative-attempts](negative-attempts/README.md) retains failed timing conditions,
including lifetime gaps of 218.91 and 226.49 ms. They did not satisfy the 100 ms
qualification target. Earlier paced queue overload produced bounded timeouts;
one startup accepted its adapter observation before the work deadline but timed
out during later publication. Acceptance and request success are distinct. No
production deadline or cleanup requirement was relaxed. Concurrent attempts and
cleanup harness corrections are separately retained in
[concurrent-timeout-attempts](concurrent-timeout-attempts/README.md) and
[cleanup-negative](cleanup-negative/). The initial unavailable-scale failure is
retained in [historical-startup-failure](historical-startup-failure/).

## Automated and bounded-input checks

[tests.txt](tests.txt) records **330 passing tests**. A subsequent focused 24-test
run includes an additional stderr-flood subcase ([windows-tests.txt](windows-tests.txt)).
Coverage includes null/malformed rows, bounded stdout/stderr, owned-child cleanup,
partial allocation and anchored directory ownership, immutable publication,
late cleanup acceptance, PID birth/membership brackets, 4096 pins and reappearance,
and actual 256-window current/previous/pending Store persistence under 64 KiB.

[measure_bounds.py](measure_bounds.py) ran 100 iterations at exactly 256 KiB raw
JSON and 256 candidates against the installed decoder. [bounds.json](bounds.json)
records maxima: JSON decode 0.743 ms, complete row validation 0.788 ms, individual
validation turn 0.150 ms and public encoding 1.185 ms. These synthetic measurements
are distinct from native compositor evidence.

Reproduce with a clean installed wheel, no older live generations, and serialized
suites. Current runners include timing-only diagnostics added after the selected
native run; its exact tested runner is preserved beside its receipt.

```sh
VENV/bin/python -I evidence/issue-23/installed_windows.py NEW_NATIVE .local/dependencies
VENV/bin/python -I evidence/issue-23/installed_windows_controls.py NEW_CONTROLS .local/dependencies
VENV/bin/python -I evidence/issue-23/measure_bounds.py NEW_BOUNDS_JSON
```
