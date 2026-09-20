# Corrected installed control qualification

Selected raw receipt: [receipt.json](receipt.json). Tested runner01ba26c,
installed production7e4f8d6; all module/resource hashes match source.

| Observation | Priority cancellation | Client EOF |
| --- | ---: | ---: |
| Action to Query.cancel dispatch | 5.001 ms | 4.989 ms |
| Action to confirmed query cleanup | 38.728 ms | 38.615 ms |
| Query.cancel to confirmed cleanup | 33.727 ms | 33.626 ms |
| Status reply during stalled query | 27.006 ms | 28.771 ms |

Both controls occur after independently observed exact script registration. The
original app PID1153983/birth15994473/cgroup remains live after each action and
filtered recovery succeeds. All query-owned children/scripts/temporary resources
are reclaimed. Query control receipts record lifetime GLib maximum77.294 ms;
maximum across every query receipt, including startup and subsequent native
metadata/disappearance work, is86.751 ms. No startup sample is reset away.

Native empty and omitted metadata, autonomous native disappearance, retained app
all-exited state, old references after restart, and fresh generation discovery all
pass. An app retirement invalidating the observation bracket is explicitly
rejected before bounded retry; no partial association is published.

Bounded in-memory wrappers call the original production methods unchanged and
record no per-call disk output. Observed maximum durations: Store._write14.278 ms,
Store.window_observation18.016 ms, lifecycle.atomic0.246 ms. These method scopes
can overlap and should not be added together. They do not attribute earlier
sporadic delays. Both service generations finish with independent cgroup-empty
observations and no fallback cleanup.
