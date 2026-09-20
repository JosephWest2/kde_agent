# Explicit verified application termination

Product source `52dbeae70e663bcc56cd945dc77fc496ab332145` implements explicit
application kill, retained-pidfd completion settlement, and current sole-reaper
root status without refreshing cgroup observation timestamps. All **412
unit/process tests passed** ([unaltered test output](unit-tests.txt)). All final
installed suites below use clean archived, noneditable installations of that
exact source, with all **36 Python modules and one packaged JavaScript resource**
hash-verified. Native fixtures are rebuilt with warnings treated as errors.

The implementation keeps the existing 5-second default / 15-second maximum kill
work limit, including admission queue time; 15-second service cleanup limit;
2 ms shared Registry scheduling allowance; and unchanged <100 ms control/max-gap
targets. These are not broad realtime or latency guarantees. Only explicit kill
creates a termination cursor. Timeout/cancellation revokes further dispatch and
leaves Registry ownership for passive wait or another explicit request.

## Final-source qualification

- **22 explicit-kill scenarios passed across 23 generations**:
  [receipt](native/receipt.json), [raw selection](native/selection.json), and
  [archive/wheel provenance](native/prepared.json). These include an already
  completed old handle while a new app lives, normal TERM receipt, resistant
  trees and KILL, root-first exit with positive original-child reaping, stopped
  child, TERM/KILL-phase late descendants, actual controlled migration,
  cancellation/disconnect survival through the original signal deadline, queued
  expiry/cancellation, partial timeout and explicit recovery, prior close/wait/
  launch timeouts without implicit signals, foreign/private-desktop sentinels,
  stale generation, explicit stop and independent bus/KWin/worker failures.
- **Six #24 launch/wait regressions passed**: `subtree-exit`, `before-map-exit`,
  `delayed`, `launch-retention`, `launch-cancel`, `launch-disconnect`.
  [Receipt](targeting-regressions/receipt.json) and
  [raw selection](targeting-regressions/selection.json).
- **Six #25 close regressions passed**: `normal-window`, `normal-app`,
  `confirmation`, `selected-sibling`, `descendant`, `hook-success`.
  [Receipt](close-regressions/receipt.json) and
  [raw selection](close-regressions/selection.json). The maximum lifetime GLib
  gap in this selected suite was 91.629 ms; no >100 ms gap was observed there.
- **21 independently run lifecycle scenarios passed**, no fallback cleanup,
  maximum fault-to-empty/finalization **7.640 seconds** within the original
  15-second deadline. The deliberately blocked-finalizer recording exception is
  preserved. See [lifecycle evidence and installation provenance](CLEANUP.md).

All native suites and heavy unit/process suites were serialized. Cleanup checks
require independent service-cgroup emptiness and final recording within the
original deadline, not a transient empty cgroup before stop hooks run.

The selected final kill run had no >100 ms lifetime GLib gaps. Its SIGINT and
disconnect dispatch measurements were **7.750 ms** and **6.247 ms**. Internal
worker status dispatch was **0.191 ms** and **0.244 ms**, while full status CLI
durations were **72.525 ms** and **65.209 ms** respectively. These are separate
scopes, not interchangeable latency claims. Final #24 regressions retained
**104.067 ms** (`subtree-exit`) and **101.660 ms** (`launch-retention`) lifetime
gaps above the unchanged 100 ms target. Functional passes do not erase them.

Reproduce each native suite in a new directory; the preparer archives the chosen
commit, builds and installs an isolated wheel, and rejects installed/source hash
differences. Select predecessor cases with repeated `--case NAME` using the lists
above. All suites must run serially.

```sh
python -I evidence/issue-26/installed_kill.py prepare /new/prepared --commit 52dbeae70e663bcc56cd945dc77fc496ab332145
/new/prepared/venv/bin/python -I evidence/issue-26/installed_kill.py run /new/output /absolute/dependencies /new/prepared
```

Use the equivalent `prepare`/`run` interfaces in
`evidence/issue-24/installed_targeting.py` and
`evidence/issue-25/installed_close.py` for the selected regressions. Each selected
raw directory retains the exact runner/support/fixture sources and a SHA-256
inventory. Native request receipts include public CLI arguments, generation/app
cgroups, birth identities, phase/cutoff/submission/observation times and durable
request/application records. A worker killed before a final summary can leave a
timestamped counter prefix with an explicitly unknown unpublished tail; such a
record is not fabricated into a final application-kill response.

## Retained development evidence

[The first native receipt](development-first/receipt.json) and
[byte-for-byte selection inventory](development-first/selection.json) retain all
22 requested scenarios across 23 generations on the preceding product source.
21 scenarios passed; migration failed a harness assertion that required ownership
uncertainty in the first Registry turn. Its raw trace shows every intermediate
turn retained ownership with no completion, then positive migration uncertainty
on the next bounded turn, and zero application signals. The corrected harness
checks all intermediate turns and eventual detection instead of assuming one
turn can finish the bounded work. This run also exposed a known root exit code
hidden behind a stale subtree cache during uncertainty; the final product fixes
that reporting defect and adds a regression.

That first run also retains a **137.383 ms lifetime GLib gap** in `stop`, above
the unchanged 100 ms target. SIGINT and disconnect dispatch were respectively
9.375 ms and 12.103 ms in their measured scenarios. Functional acceptance and
selected dispatch measurements do not erase the lifetime timing negative.

Inherited #24 evidence retains 424.188, 132.647 and 136.902 ms lifetime gaps.
Inherited #25 evidence retains 102.895 and 111.008 ms lifetime gaps and 112.323 ms
full status CLI latency. No threshold is relaxed and no causal explanation is
inferred from these observations.

## Scope

Pidfds prevent numeric PID reuse from redirecting a retained descriptor. Unit
coverage includes a real dead original pidfd with a separate live sentinel
presented through a deterministic reuse simulation; it does not claim the kernel
actually recycled that numeric PID. Membership observations are sequential and
cannot atomically prevent hostile same-user migration after the last check.
Controlled migration moves an already acquired finite fixture descendant and
verifies retained ownership uncertainty, never successful application exit.
Separate independently justified service cleanup remains separately attributed.

Finite fixtures qualify normal TERM exit, resistant descendants and KILL,
root-first exit, setsid/double-fork/reparenting, stopped children, late descendants,
bounded partial outcomes, cancellation/disconnection, stale/no-op handles and
foreign/private-desktop sentinels. Evidence-only hooks for KILL-phase descendant
creation and partial timeout are explicitly labeled in the raw traces and are
not public product parameters. Signal tracing adds proc reads before submission;
its overhead and entry/submission timestamps are recorded, and it does not
qualify atomicity of the product's verification/dispatch sequence.

Production shutdown hook integration, input/capture workflow, representative
third-party applications and repeated release readiness remain separate #35 work.
