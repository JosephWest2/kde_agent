# Issue 24 — exact targeting, observed focus and bounded waits

Implements [#24](https://github.com/JosephWest2/kde_agent/issues/24), parent #4.
Product source is `8919c37869ded43d08ab1eacae63edbd1900af29`; later commits change only evidence runners and retained receipts. The initial source commit `b1da8f1`
passed 354 unit/process tests and the complete 21-case installed lifecycle suite.
The final source adds a regression and fixes known exec acknowledgment retention
before a later record write can fail. Final verification passed **355 unit/process tests** and the complete
**21-case installed lifecycle regression**. Native results and timing limitations
are recorded below.

## Reproducible installation and scope

`installed_targeting.py prepare NEW --commit SHA` uses `git archive`, builds one
wheel from that clean archive, installs a new isolated system-site-packages venv,
and records commit/archive/wheel hashes. `run` verifies all 34 installed Python
modules and the packaged query JS against the committed archive, plus the runner
hash, fixture build, dependency versions and pinned kdotool hash. It invokes the
installed public CLI from `/` with generation-private endpoints. The evidence
worker adds fixed private transport faults and bounded trace/timing observations;
none are accepted in production requests.

Native suites and the complete lifecycle suite run one at a time. Each finite
scenario has its own generation, public CLI argv/outcome receipts, query/native
start and completion traces, fixture receipts where relevant, retained request/app
records, and final cgroup emptiness. Diagnostic JSON replacements and bounded
JSONL trace writes do not fsync; production artifact durability is unchanged.
Later runner revisions also record heartbeat gaps >=100 ms and Store write
intervals >=50 ms, with bounded aggregate timing. These observations support
measured scopes, not causal claims about an earlier run.

## Functional evidence

The first installed batch at `8919c37` proved ten alternating real primary/sibling
focus transitions, each corroborated by an independent public windows query and
labeled xdg activation receipts. It rejected all four same-title owned candidates
(primary, sibling, dialog and descendant) as ambiguous, accepted public braced
uppercase UUID spelling, and completed delayed multi-window launch/wait.

The next batch at runner `a720b1a` passed 14 of its 19 scenarios: successful native
no-op activation correctly timed out, pre-map application exit was distinct,
launch window timeout retained app/process/logs and later succeeded, unique app
focus succeeded, queued resize and vanished-target rejection used fresh state,
passive focus observed disappearance, root exit with a live descendant timed out
before eventual complete exit, slow and stopped native queries cleaned up before
reuse, explicit stop cancelled work, bus/KWin death returned `session_failed`,
worker SIGKILL returned `completion_unknown` with retained failed terminal state,
and restart rejected stale app/window generations before a new native operation.

Five scenarios stopped on evidence-runner errors and are retained: lifecycle
status has an internal worker request ID different from the outer CLI ID; direct
private kdotool controls require the production environment's
`KDE_SESSION_VERSION=6`; and a fixture width decoded as
`400.00000000000006` cannot be used as an exact integer dictionary key. The runner
was corrected for those specific causes, with no product changes or widened
bounds. A separate run qualifies those corrected controls, passive focus
transition, queued global-position movement, and dialog/descendant selection.

## Timing and limitations

The first focus generation recorded a **424.188 ms lifetime GLib gap**, retained
as a negative timing observation: it fails the unchanged 100 ms lifetime
maximum-gap target. Its readiness query had already recorded a
175.068 ms maximum; the 424.188 ms maximum first appears at the launch window-wait
query completion, before the explicit focus series. Those initial receipts lack
individual heartbeat intervals, so they do not identify the responsible callback
or establish a cause. The generation's functional focus assertions passed; it
must not be represented as meeting a universal 100 ms loop-gap guarantee.

The delayed-window generation's lifetime maximum was 79.075 ms. Across the later
19-scenario batch, the maximum among generations with a final worker heartbeat
receipt was 46.651 ms; the SIGKILL generation cannot supply a final worker receipt.
Minimum per-request query-start intervals in the first focus/delayed generations
were 100.500/102.651 ms. The metric covers query starts, excludes intervening
activation operations, and does not imply sustainable 10 Hz throughput. There is
one native owner, no overlap or catch-up burst, and strict work acceptance and
cleanup deadlines remain unchanged.

The earlier no-op attempt also remains a negative runner receipt: its injected
preparation accidentally called the dynamically replaced Query subclass method
on an Activation object and raised before native spawn. Calling the original
query preparation fixed that test-only shim; the subsequent native successful
no-op then produced the required public timeout.

Fixture evidence does not certify representative applications, close/kill,
input/click emission, capture, held-input checks or release readiness. The shared
read-only target/focus/client-geometry seam is implemented, but
`release_qualified` remains false and issue #35 remains responsible for production
input/capture qualification. Geometry is sampled observation, not an atomic
query-to-action lock or a freshness lease.

The corrected-control batch at runner `0247824` passed launch SIGINT,
CLI disconnect, externally driven passive focus, and explicit selection of all
four primary/sibling/dialog/descendant UUIDs. Cancellation dispatch measured
7.075/6.387 ms, and the internal worker status requests completed in
7.493/7.664 ms. These are distinct from full CLI process duration and from
whole-generation maximum GLib gaps. App root birth identities matched before
and after cancellation/disconnect and subsequent window waits succeeded.

That batch also retains a **132.647 ms** lifetime gap in the four-surface selection
generation, also failing the unchanged 100 ms lifetime maximum-gap target.
It overlaps launch admission before construction of the window-wait task,
specifically monotonic interval 162131.012161984–162131.144808827
(the diagnostic record was emitted at 162131.144813256). No single instrumented
Store write exceeded its 50 ms event threshold there. This does not exclude
multiple smaller writes, uninstrumented work, scheduling delay, or establish a
cause or exclude launch-path responsiveness risk. Other corrected-control generation maxima were 85.493–89.321 ms.

The queued-position control initially reported that it matched the UUID, but
both the returned and independent client geometry remained at `(320,180)`.
That attempt is a retained failure: mutating the Qt rectangle value did not
establish movement. The evidence script now uses the same plain-object
`Object.assign({}, w.frameGeometry)` copy-and-set sequence as the locally pinned
kdotool `windowmove` template, consistent with KDE's documented writable
[frameGeometry property](https://develop.kde.org/docs/plasma/kwin/api/).
Only observed changed client coordinates can satisfy the queued-move assertion.

The final queued-move run at `7cfa228` passed: the selected client's position
changed from `(320,180)` to `(370,210)` while its focus request was queued; width
and height stayed `640×360`. No native operation for that queued request started
before the private move completed. Focus returned the new client rectangle,
matching a separate public windows query. This run also recorded a **136.902 ms**
lifetime gap, failing the same unchanged 100 ms target. The observed interval
162338.430986566–162338.567888368 overlaps launch admission at
162338.431158538 and precedes window-wait construction at 162338.652858692.
There was no >=50 ms instrumented Store write event, which again establishes
neither a cause nor exclusion of launch responsiveness risk. All three failing
lifetime maxima (424.188, 132.647 and 136.902 ms) remain part of this qualification.

## Retained receipts and verification

- [First native batch](native-first/receipt.json): functional focus/delayed
  success, the initial no-op shim failure, and the 424.188 ms gap.
- [Main native batch](native-suite/receipt.json): 14 functional passes plus five
  explicit runner failures; every attempted public call and native outcome stays
  in the receipt.
- [Corrected controls](native-controls/receipt.json): four functional passes,
  cancellation/status timing, the unsuccessful rectangle mutation and the
  132.647 ms gap.
- [Queued move](native-move/receipt.json): verified queued-position change and
  the 136.902 ms gap.
- [Installed cleanup regression](CLEANUP.md): all 21 lifecycle cases on final
  product source, plus the retained earlier finalizer-observation race and its
  narrowly reviewed runner correction under the original deadline.

Together these cover all 21 planned runner scenarios functionally, without
claiming that every recorded run passed its timing target. Each native directory
includes the exact tested runner, prepared wheel/archive receipt, raw durable
artifacts and SHA-256 inventory. Build binaries/generated protocol sources and
empty lock files are omitted; fixture build hashes and immutable source commits
are retained. Paths in raw receipts name their original run locations and have
not been rewritten. The per-request query-start metrics exclude activation starts.
Unit/process test output is [tests.txt](tests.txt).

Reproduce with a new preparation/output directory (never overwrite an earlier
receipt):

```sh
/usr/bin/python3 -I evidence/issue-24/installed_targeting.py prepare /tmp/NEW --commit SHA
/tmp/NEW/venv/bin/python -I evidence/issue-24/installed_targeting.py run NEW_OUTPUT .local/dependencies /tmp/NEW
```

Acceptance here is functional behavior plus the explicitly measured control
scenarios. It is not a blanket latency or sustainable throughput qualification,
and does not relax the 100 ms responsiveness target or any work/cleanup bound.
