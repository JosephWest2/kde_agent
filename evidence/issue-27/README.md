# Persistent production libei connection qualification

The final readiness correction is qualified from **7dd3ceed**. Fresh PR review
found that completed health replies could publish startup readiness while an
undrained event backlog had already paused the keyboard. Startup now retains
the completed health round and its original deadline until input actually
becomes ready. The regression uses the production Input class and 257 queued
events: healthy backlog eventually publishes readiness after draining; PAUSED
backlog never publishes readiness and fails after draining.

**436 tests passed** on the correction ([output](review-startup-fix/unit-tests.txt)).
A newly archived, noneditable installation passed focus, delayed startup/launch
and slow-query scenarios with cleanup verified ([receipt](review-startup-fix/worker/receipt.json),
[provenance](review-startup-fix/prepared.json)). These three runs also retain
**326.964 ms**, **286.619 ms**, and **140.993 ms** lifetime GLib gaps respectively,
all above the unchanged 100 ms target. The largest focus gap overlaps three
synchronous artifact writes of 53.474, 58.431 and 114.138 ms during request
admission after startup. The other maxima have insufficient in-gap tracing for
precise attribution. Functional passes do not certify the responsiveness target.
No concurrent native/heavy test suite ran during these samples.

The connection, binding and private-bus module bytes are unchanged from the
nine native lifecycle scenarios below; their [matching hashes](review-startup-fix/summary.json)
are preserved. Those earlier traces remain attributed to their original source,
and their smaller timings do not replace the corrected-source worker samples.
The final correction has not been claimed as full-release qualification.

The original qualification below records **8f1760ce**:


Product and evidence-runner source **8f1760ce** was archived, built as a wheel,
and installed noneditably into an isolated system-site-packages venv. Every
installed package file is hash-checked against that immutable archive. The
[preparer receipt](prepared.json), [production ABI audit](native/audit/audit.json),
[raw native selection](native/selection.json), [public worker receipt](worker/receipt.json)
and [summary](summary.json) retain the exact tested provenance. **435 unit/process
tests passed** in 22.194 seconds ([unaltered output](unit-tests.txt)).

Nine real compositor generations passed: acknowledged input with eight repeated
replacements and stable FD count; cancellation/EOF/timeout; idle and held pause;
held removal plus idle rebind; connection loss; failed production async
negotiation; focus loss; slow compositor query; and pause during independent
slow child work. All finish with neutral fixture state, no unresolved input
uncertainty, and independently verified empty service cgroups. The five lifecycle
recovery runs and eight repetition samples use explicitly disconnected old EIS
contexts and fixture neutral receipts before acknowledging recovery.

Six separate installed public-worker scenarios passed: focus, delayed launch,
slow query, private bus death, private KWin death and stop during wait. These
exercise production readiness and persistent input ownership while ordinary
operations run. Public input/reset remain unsupported until subsequent subissues.

| Recorded scope | Maximum observed | Retained acceptance target |
| --- | --- | --- |
| Adapter GLib heartbeat gap | 15.650 ms | 100 ms |
| Explicit cancellation/EOF to release dispatch | 0.761 ms | 100 ms |
| Explicit cancellation/EOF to fixture release | 3.220 ms | 500 ms |
| Pause/cancel while independent 1-second child still runs | 0.649 ms | 100 ms |
| Explicit replacement through resumed/neutral evidence gate | 8.956 ms | 3 seconds |
| Native harness observed complete service shutdown | 219 ms | 15 seconds |
| Selected public-worker lifetime GLib gap | **108.421 ms** (bus-death) | 100 ms |

The public-worker bus-death sample exceeds the unchanged lifetime-gap target;
functional success does not erase that observation. Other selected public-worker
gaps are 83.980–94.102 ms. These are machine/run observations, not realtime
promises or full-release qualification. Native input dispatch/release samples
remain separately scoped; the worker lifetime maximum is not an input dispatch
measurement. All native and heavy unit/process suites ran serially.

The [retained initial failure](development-failure/runner-output.txt) is a
query-helper dependency mismatch before input negotiation: the current dependency
build has kdotool SHA256 `b7a300d5a2f0b95a21d71dca5757328382bb6dd887e4ac975fffb59e2351bd21`,
while the historical M1 helper requires its audited #9 binary. Adapter fault
runs therefore use the preserved unchanged kdotool build SHA256
`62e7ee53096d933ec29e8e5d439b895590f851a40f0dcd87b87db9a6dc4749de`;
public worker regressions use the current dependency directory. The failed
private generation also cleaned up successfully. Earlier successful development
matrices remain under `.local/issue27-*`; only the qualified final-source matrix
is selected here.

Reproduce from a clean committed source, in fresh directories, serially:

```sh
python -I evidence/issue-24/installed_targeting.py prepare /new/prepared --commit HEAD
/new/prepared/venv/bin/python -I /new/prepared/source/evidence/issue-27/audit.py /new/audit
python -I /new/prepared/source/tools/private_harness.py run --artifacts /new/runs -- \
  /new/prepared/venv/bin/python -I /new/prepared/source/evidence/issue-27/installed_input.py \
  /absolute/audited-m1-kdotool input
```

Repeat `input`, `cancel`, `removal`, `disconnect`, `focus-loss`, `slow-query`,
`reset-failure`, `pause`, and `slow-external`. For `pause` and `slow-external`, pass the locally built, audited M1
`--eis-fault-plugin /absolute/issue12_eis_fault.so` before `--`.
The native matrix generally takes under two minutes; each harness generation
retains its existing finite deadlines and independent owned-service cleanup.

The runner reuses the M1 fixture and scenario observations, but replaces its
Input class with the installed production owner and uses production asynchronous
PrivateBus/EIS negotiation. Reset between evidence actions includes neutral
fixture observations; it does not implement or certify public issue #31 reset.
Source and installed hashes are checked before scenarios; raw logs record actual
emission/fixture timestamps and real lifecycle events. No public input operation
is enabled by this PR. The full public input workflow and twenty-run support
qualification remain subsequent milestones.

Public-worker reproduction with the same prepared wheel:

```sh
/new/prepared/venv/bin/python -I /new/prepared/source/evidence/issue-24/installed_targeting.py \
  run /new/worker /absolute/current-dependencies /new/prepared \
  --case focus --case delayed --case slow-query --case bus-death --case kwin-death --case stop-wait
```

Cancellation of a pending EIS call revokes its private-bus token and closes any
stale local FD-list on completion. The production fail/stop path also closes
the originating bus, destroying its server contexts. This evidence does not
certify successful reset/reuse of a retained bus after unknown server-side
negotiation completion; issue #31 must establish that recovery boundary.
