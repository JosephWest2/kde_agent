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
