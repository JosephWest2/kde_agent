# Issue 25 — graceful selected-window close

Implements [#25](https://github.com/JosephWest2/kde_agent/issues/25), parent #4.
Product source is `9584243e6ab7fef57c9b263f4cef440dc9e8e4de`.
The approved plan and fresh plan review select owned-application exit observation:
explicit UUID close of a real unassociated surface is unsupported before dispatch;
explicit focus remains available for that surface.

## Implementation and unit/process checks

One fresh selected window receives one native `windowclose {uuid}` request.
Completed transport is distinct from application acknowledgment. Success requires
positively observed whole-application exit within the admission deadline. Window
loss after dispatch and root exit with descendants alive do not satisfy it.
Confirmation, refusal, timeout and cancellation never cause implicit application
signals or dialog interaction. Exact references and typed bounded lifetime state
remain in partial/durable records.

The independent close owner underlies the public task and a bounded reusable hook.
Production shutdown policy/wiring remains #35. Superseded read-only queries keep
one existing cleanup reserve; success still requires acceptance before the work
deadline. Actual scheduler/shutdown deadlines hard-clip that same reserve.

The final integrated unit/process suite passed **383 tests in 22.172 seconds** at
`57d644a2`. The later hook-only correction reports unresolved foreign adapter
ownership as unconfirmed without touching that owner; all 21 affected close tests
passed. Focused close tests include the five reviewed supersession
boundaries, a real Query cleanup/reuse check, and retained terminal identity after
disconnect. Application tests cover missing root metadata, empty-before-reap,
pending publication, historical completion and constant-size 4096-identity reporting.
Native-adapter process tests exercise actual child cleanup and a hook whose owner
pumps child reaping. The fixture compiled with
`-std=c11 -O2 -Wall -Wextra -Werror`.

The baseline source `3ec00739` passed the complete installed **21-case cleanup
regression**, with all 35 Python modules plus packaged JS matching the archive,
no import fallback, and a maximum observed cleanup duration of 7.632 seconds.
Final corrected-source installed qualification passed all **21 cases** at product
`9584243e`, with exact installed hashes and a maximum observed cleanup of 7.556
seconds. See [cleanup qualification](CLEANUP.md).

## Installation and native qualification

`installed_close.py prepare NEW --commit SHA` builds an isolated noneditable
wheel from a clean git archive. `run` verifies all 35 installed Python modules and
packaged query JS, runner/support hashes, fixture build and pinned dependencies.
Runs use separate public CLI invocations and generation-private endpoints.

[Initial receipts](native-initial/receipt.json) pass normal selected-window close,
confirmation persistence/discovery/focus without dialog input, and the bounded
hook timeout. [Remaining receipts](native-remaining/receipt.json) pass the other
25 cases: app/normalized UUID selection, ambiguous app rejection, selected sibling
and dialog closure with remaining process lifetime, refusal and finite delays,
root exit with a live detached descendant and later whole-subtree completion,
queued disappearance/new dialog/expiry/cancellation, native successful no-op,
SIGINT/disconnect/stop, service failures, bounded slow/stopped adapter faults,
hook success and restart generation rejection. This is **28 scenarios across 29
generations**, all functionally passed, with finalizer recording plus independently
observed cgroup emptiness inside each original 15-second stop bound (maximum
0.401 seconds). Each selected folder includes a hash inventory and exact tested
runner/support; no failed attempt or latency outlier was discarded.

The hook host uses the real Adapter/Registry while the actual scheduler pumps
Children and Registry. Its timeout bound is 350 ms inside the outer public
request budget so the hook itself reaches terminal state; this is an evidence-only
host, not production lifecycle wiring. Supplied deadline and terminal idempotence
are traced. Native bus/KWin-death receipts preserve unconfirmed adapter script/temp
cleanup and the independent failed-session finalization. Those faults and worker
kill landed during native-close cleanup; supplemental exit-wait fault observations
are being added before final review. Stop, SIGINT and disconnect already reached
exit wait and retained the live app until independent cleanup.

Measured query starts were at least 100.163 ms apart. Selected status/cancellation
measurements use internal worker request records, separately from full CLI latency:

| Scenario | Worker status ms | Full status CLI ms | Cancel dispatch ms |
| --- | ---: | ---: | ---: |
| SIGINT after close | 0.340 | 68.820 | 2.846 |
| CLI disconnect | 0.153 | 74.257 | 4.594 |
| Slow query | 0.122 | 112.323 | n/a |
| Stopped query | 0.120 | 91.516 | 5.171 |
| Stopped close child | 0.107 | 85.701 | 1.649 |

The slow-query full CLI status exceeds 100 ms. Lifetime GLib gaps of **102.895 ms**
(queued cancellation) and **111.008 ms** (worker-death generation) also fail that
unchanged target. These remain negative evidence; selected internal status/cancel
measurements alone qualify their passing scopes. Diagnostic writes are bounded
atomic replacements/JSONL without fsync; production durability is unchanged.
Native and complete installed cleanup suites ran one at a time.

## Scope and limitations

Functional acceptance is separate from latency qualification. Inherited #24
lifetime GLib gaps of 424.188 ms, 132.647 ms and 136.902 ms exceed the unchanged
100 ms target and remain negative evidence. New runs must retain every measured
outlier. Only measured passing controls can qualify their selected scopes; none
of this establishes broad latency or release readiness.

Confirmation persistence/discoverability is the close acceptance criterion.
Public key/click confirmation input remains #28/#29. A separately labeled private
fixture input control, if used, is evidence-only. These cases do not qualify
representative third-party applications, production lifecycle wiring, or the
future twenty-run release acceptance.

Controlled process migration is not qualified here. The inherited lifetime observer
can retire an empty application cgroup after root reaping even if a previously
acquired live descendant was deliberately moved elsewhere. These native cases
cover cooperative descendants retaining inherited membership. Issue #26 must add
a bounded retained-pidfd completion check and report foreign-membership uncertainty
without signaling it before parent #4 can be considered complete.
