# Structured window discovery

`agent-desktop windows [--app GENERATION:APPLICATION_ID]` reads one fresh KWin
snapshot through the qualified pinned kdotool binary and explicit private bus.
The packaged `window_query.js` is fixed; requests cannot supply script source,
transport arguments or fault modes. Construction is effect-free. Readiness uses
the same adapter without process association, while input/capture qualification
remains `m1-provisional`, `release_qualified: false`, replacement issue #35.
KWin output scale is explicitly null when its scripting wrapper omits it; fixed
1280×720 geometry is validated here and scale-1 capture remains separately qualified.

The result contains `generation`, `request_id`, `query_id`, `observed_at`,
`accepted_at`, `observation_state`, `active_window`, `windows`, `query_artifact`
and cleanup confirmation. Each window carries a canonical bare UUID under
`window: {generation, window_id}`, compositor `pid`, `title`, `class`, `client`
and `frame` rectangles, `active`, optional `app`, and `association` metadata.
Unavailable metadata is null; empty strings remain empty. Client geometry never
falls back to frame geometry. Negative/fractional coordinates are preserved.
Repeated titles/classes/PIDs remain distinct candidates. Filtered discovery
includes only positively associated rows; a known exited app may return empty.
The global `active_window` still describes the compositor even with an app filter.

## Ownership and sampling

Reported PIDs are lookup keys, not authority. Before spawning, the registry takes
an acquisition cutoff for its current app. Only a matching live pidfd retained
before that cutoff can qualify. Current process birth and app-subtree membership
are verified with live-pidfd checks on both sides. Unknown, exited, moved, reused,
newly acquired or uncertain identities remain unassociated. These queries never
signal, focus, select or close windows. UUID native arguments for future focus/
close callers must be brace-wrapped at the kdotool boundary.

Each associated row records `association.verified_at` from its final bounded
verification. Rows are sampled across turns; `accepted_at` does not assert that
all processes were simultaneously alive or retain future membership. A final
constant-time generation/app/uncertainty check precedes pin commit without disk
work. KWin supplies numeric PIDs rather than birth tokens, so this bracket cannot
exclude every hypothetical stale compositor attribution predating the first
observation. Future effects must requery and revalidate their own authority.

Generation pins retain at most 4096 UUID-to-birth bindings without eviction or
reassignment, even after disappearance or app retirement. Unseen UUIDs after
capacity return `association_capacity`; changed bindings return `identity_changed`.
Failed partial annotation consumes no pins. A generation teardown clears pins.

## Bounds and cleanup

Work acceptance, including resource cleanup and retention, must finish strictly
before the original 500 ms deadline (including queue time). Failure latches one
separate 1.5-second cleanup reserve, clipped by lifecycle shutdown. The owner
supervises stdout/stderr concurrently: 256 KiB/64 KiB total, at most 16 KiB or
2 ms drained per turn. At most 256 rows and 4096 characters per title/class are
supported; decode/association/recheck use at most 16 rows/2 ms per turn. The
encoded public response must fit the existing 1 MiB frame.

Cleanup kills only owned children, observes their sole Children reaper, checks
only the unique owned script name and, when needed, runs pinned kdotool removal
followed by independent script-absence confirmation on a usable private bus.
A collision before own spawn never authorizes removal. Each callback is guarded
by operation epoch. No blocking waits or synchronous D-Bus occur in query turns.
Pipes close and the owned 0700 temporary directory is reclaimed before success;
anchored enumeration handles at most four entries/2 ms, 32 entries total, depth
one. Regular files and symlinks are unlinked without following targets;
unexpected subdirectories/ownership fail cleanup. Unconfirmed cleanup blocks
new queries and escalates to failed-session teardown. Killing kdotool does not
preempt already-running compositor JavaScript. Ordinary local filesystem timing
retains the established normal-storage assumption.

## Observation is distinct from request success

Exclusive immutable `window-observations/QUERY_ID.json` files are labeled
`observation_state: observed`: they contain staged data, not an accepted request.
After durable publication, final identity sampling, cleanup and a fresh deadline
check, the adapter commits its bounded pin delta and records `accepted_at`.
Public rows reflect those final samples and are labeled `accepted`.

Application records preserve a previous confirmed observation plus a pending
candidate. A successful write and post-write deadline check confirm the pointer
in memory; the registry subsequently checkpoints only that already-confirmed
state. Late/failed publication retains previous references and exposes candidate
uncertainty. Completed apps receive metadata without reopening process ownership.
An already-confirmed pointer can coexist with a later scheduler/transport failure;
there is no rollback claim and no inference of request success from a file or
accepted timestamp. Historical references assert observations, not present windows.
Generic request records retain at most 64 handles, an explicit `windows_omitted`
count and a validated owned `query_artifact` reference, never arbitrary titles.

Durable application publication stores at most two window-reference arrays:
confirmed top-level `windows` plus a full previous observation (confirmed state)
or proposed candidate (pending state). Other observation pointers carry metadata
without repeating arrays; the validated reader reconstructs those views using the
explicit publication state. Both pending and deferred confirmed encodings are
size-checked before publication, so 256 supported rows do not create an oversized
checkpoint on the next registry turn. Cleanup reuse also requires a fresh clock
check after its final local filesystem operation, strictly within the latched
reserve; a late cleanup observation remains a fail-closed result.

## Targeting, focus and waits

`focus --window GENERATION:UUID` resolves that exact current UUID. Braces and
uppercase are accepted and normalized; native kdotool receives a brace-wrapped
canonical UUID. Explicit identity can target an unassociated surface. `focus
--app GENERATION:APPLICATION_ID` requires exactly one currently associated
window: no candidates gives `target_not_found`, multiple candidates gives
`target_ambiguous` with every candidate handle. Titles, PID alone, active status,
window size and dialog parenting never break a tie. Selection occurs when the
queued task executes, using current geometry and associations.

Focus sends one exact activation and then queries again until the same UUID is
observed active with a consistent row focus flag. Native exit zero is only
transport completion; successful no-op activation times out. The selected UUID
is never replaced or activation retried. App-derived selection revalidates its
original process identity immediately before dispatch, after asynchronous
preparation and effect-record persistence. Disappearance is `target_lost` after
selection, or `target_not_found` on the initial observation. The compositor race
between query and action remains observable, not atomic.

`wait --for window --app APP` is passive and existential: any nonempty set of
positively associated windows satisfies it, and all rows are returned. Subsequent
`focus --app APP` still requires a unique candidate. `wait --for focus --window
WIN` never activates; it succeeds only on a fresh focused observation, errors on
absence/loss, and times out while the window remains unfocused. `wait --for exit
--app APP` requires root reaping and complete application-subtree emptiness;
root return code alone cannot satisfy it. Known completed records remain readable
without reopening process authority. A live `launch-failed` app is not complete.
Window/app-focus waits report `application_exited` on known complete app exit.
Unknown apps give `target_not_found`; uncertain ownership fails the session.

All composite work shares the admission deadline, including queue time. Each
query or activation is capped at 500 ms within that deadline. Query poll starts
are at least 100 ms apart, with one operation in flight and no catch-up bursts;
activation preparation is a separate native operation. Individual operation
faults terminate the wait rather than being treated as an unsatisfied condition.
Success requires fresh post-observation, post-retention and final deadline/health
checks; acceptance at or after the deadline fails. These observations do not
establish a total ordering of unseen real-world events or a freshness lease.

Focus returns current `client`/`frame`, exact `window`, `active_window`, `focused`,
association verification and query observation/acceptance times. `client` may be
null. The read-only `current_target`/`TargetTask(condition='observe')` seam permits
future input to demand focus and client geometry; missing geometry explicitly
fails with `client_geometry_unavailable` and never substitutes frame geometry.
No input is emitted and input/click qualification is unchanged.

Cancellation and disconnect keep the current native operation owned through its
single cleanup reserve; no following action starts until cleanup is confirmed.
Unknown cleanup triggers failed-session teardown. Effects record that activation
may have changed focus; cleanup does not restore focus. Explicit stop cancels
unfinished work. Detected essential session failure gives `session_failed` (or
`session_unavailable` before execution), preserving an already-latched cause.
A killed worker cannot send a terminal reply: the existing transport may report
`completion_unknown`, with authenticated retained generation/service failure
records supplying diagnosis. The toolkit does not fabricate a reply or retry.

The native fixture adds `--resize-after-ms LABEL:MS:WIDTH:HEIGHT` and
`--destroy-after-ms LABEL:MS`, for `primary`, `sibling` or `dialog`, relative to
fixture start. Scheduled receipts include actual elapsed time and whether the
surface existed. Configure receipts include labeled activated state. Client
resize does not claim control over global placement. Installed evidence and
its measured bounds are recorded under `evidence/issue-24`; they do not qualify
representative applications, held input, capture or release readiness.

## Graceful selected-window close

`close --window WIN` and `close --app APP` select exactly one window from a fresh
execution-time observation. App selection requires a unique positively associated
window. Explicit UUID selection also requires an owned application lifetime to
observe: a real unassociated surface returns `unsupported_operation`, reason
`application_association_unavailable`, before native dispatch. Explicit focus
continues to support unassociated surfaces. Missing/ambiguous targets return the
same documented selection errors, with exact window/query references.

Close sends one fixed `windowclose {uuid}` operation after checking the selected
process identity before and after persisting effect intent. Native completion
proves transport and adapter cleanup, not application acknowledgment or causation.
Success requires observed whole-application exit before the original deadline;
root exit or window disappearance alone is insufficient. After dispatch the
selected window may disappear, be replaced by a confirmation, or leave a sibling
alive. Close keeps waiting on the pinned application and never retargets, repeats
the close, dismisses a dialog, sends input, or signals an application.

Results and partial failures include the selected `window`, `application` handle,
`application_snapshot`, last accepted `windows` and query/timestamps, observational
`window_state`, and typed `close_state` with phase, native operation ID, uncertain
or completed transport, and exit-observation time. `exited` is true for complete
exit, false for a positively observed populated subtree, and null when unknown.
`root_returncode` / `exit_status` report the root code; descendant codes are unknown.
`remaining_processes` is null. The constant-size `process_state` gives the actual
cached cgroup populated bit, root reaping/code, whole-app completion and observation
time, with `enumeration: unavailable`. Empty cgroup before root reaping or pending
publication is not completion. Historical completion has no invented timestamp.
Old accepted observations remain labeled with their original times.

Close uses the same 5-second default / 60-second maximum admission budget, 500 ms
native/query cap, 100 ms query-start spacing and 50 ms lifetime observation cadence.
If app exit supersedes an in-flight read-only query, that query retains its single
1.5-second cleanup reserve. Success requires cleanup acceptance strictly before
the work deadline. If the deadline wins, timeout cleanup can use the remainder of
that same reserve, clipped only by actual scheduler/shutdown cleanup bounds.
Cancellation/disconnect retains exact effect identity and cleans only owned adapter
resources. Unconfirmed adapter cleanup or essential session failure follows the
independent failed-session policy; an ordinary close timeout does not escalate.
