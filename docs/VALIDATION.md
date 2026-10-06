# Validation history

The original per-issue evidence archives (receipts, logs, installed-source
copies) were removed from the working tree in #62. They are still in git
history: every folder exists at commit `d1efe95b` under `evidence/issue-N/`,
for example `git show d1efe95b:evidence/issue-27/README.md`.

Ongoing validation is the unit suite plus the end-to-end smoke test (#65) and
failure-path tests (#67); see [TESTING.md](TESTING.md).

| Issue | Area | Headline result | Added in |
| --- | --- | --- | --- |
| #9 | Dependency setup | Reproducible prerequisite report and pinned kdotool build. The report now lives in `tests/data/issue9-environment.json`. | `137dc97d` |
| #10 | Private harness | Native fixture ran on a private headless KWin with one 1280×720 output at scale 1 and received presentation feedback. | `3ffde8b6` |
| #11 | Window transport | Pinned kdotool kept: structured metadata, verified activation, vanished-UUID rejection and bounded cleanup all worked. | `beb29d45` |
| #12 | libei binding | 8 input lifecycle scenarios passed in 3 fresh private desktops each, with empty cgroups after cleanup. A setup FD-ownership fix was found in review and verified. | `ddacc3b3` |
| #13 | Capture | 11 private desktops produced 32 complete PNGs, with 1,080 exact pixel assertions. Hung or partial captures were reclaimed and stopped their desktop. | `e81d8ad9` |
| #16 | Queue and cancellation | 119 tests with a real GLib worker, Unix sockets and separate CLI processes. | `b832fb6d` |
| #17 | Paths and records | 149 tests. Installed wheel verified from a different working directory. | `71a12e78` |
| #18 | Service lifecycle | 172 tests on real user services: duplicate starts, worker death, frozen-worker fallback, descendant cgroup cleanup. | `6ac423be` |
| #19 | Private environment | 181 tests. Installed wheel ran 3 service generations with environment separation checked. | `094d0133` |
| #20 | Readiness and health | Start becomes ready only after control, window query, resumed EIS input and a complete screenshot. 5s systemd watchdog for frozen workers. | `ac6982c0` |
| #21 | Shutdown and crash cleanup | All 21 installed cleanup cases passed. | `69d6f838` |
| #22 | Launch ownership | Installed launch with durable cgroup ownership, using clean caller environments and separate working directories. | `0768f6ec` |
| #23 | Window discovery | Structured discovery with 256-window retention and a strict cleanup-deadline recheck. | `6541bd24` |
| #24 | Targeting, focus, waits | 355 tests plus the full 21-case installed lifecycle suite. | `acfadd33` |
| #25 | Graceful close | Close works through owned-application exit observation, with no implicit escalation. | `88108561` |
| #26 | Termination | 412 tests, retained-pidfd kill settlement, installed suites. | `76c73c8a` |
| #27 | Persistent libei connection | 436 tests and 9 real compositor lifecycle scenarios. Readiness waits for drained, resumed input. GLib stalls of 140–327 ms were seen (deferred). | `d1efe95b` |
| #61 | kwin-mcp comparison, target app | Continue the project, using kwin-mcp as a reference. gnome-text-editor passed 5 full cycles. Worker SIGKILL cleanup verified. | `planning/REVISED_PLAN.md` |
| #80 | Owner-loop stalls | Measured: the stalls are synchronous fsyncs of artifact records on the owner thread. Harmless on tmpfs; on an NVMe btrfs disk the owner sits in fsync for about a third of each smoke run; and under heavy write load they break 0.5s/1s deadlines (failed starts, requests and sessions). Two redundant fsyncs removed (−10%). Window-query, KWin name-check and health-round budgets no longer count owner stalls (failed starts under load 10 → 5 of 25). Moving the writes off the owner thread is #96. [Details](#80-owner-loop-stalls). | this section |

## #80 owner-loop stalls

Method: the opt-in owner profile ([TESTING.md](TESTING.md#owner-loop-profile)),
which times a 5ms GLib probe and every owner callback, request step, Store call,
fsync, D-Bus call, child spawn, screenshot and window-query step, import and GC.
Runs: the smoke test (`--loop 5`) and the failure-path tests (`--loop 2`) on
KWin 6.7.5, Python 3.14.7 and Linux 7.2.6 (Arch). Artifacts went to tmpfs (`/tmp`, what the tests use by
default), or to btrfs on the system NVMe (Crucial P3 CT1000P3PSSD8,
`compress=zstd:3,ssd,discard=async`, `/home`), where the CLI's default artifacts
root (`.agent-desktop/artifacts` under the caller's directory) normally lives.
"Load" is a background `dd` rewriting 512 MiB of random data with `conv=fsync`
on the same disk, about 830 MiB/s. `worker-stopped` freezes the worker on purpose
and is left out.

Probe lateness (ms; one probe every 5ms):

| Artifacts | Probes | p50 | p95 | p99 | max | >50 | >100 | >250 | Runs passed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tmpfs | 16,837 | 0.0 | 0.0 | 0.1 | 15.7 | 0 | 0 | 0 | 25/25 |
| tmpfs, disk under load (smoke ×5) | 10,272 | 0.0 | 0.0 | 0.1 | 12.2 | 0 | 0 | 0 | 5/5 |
| btrfs | 16,798 | 0.0 | 12 | 51 | 551 | 172 | 73 | 6 | 25/25, plus an earlier set that stopped at a failed start (below) |
| btrfs, before the fix | 16,702 | 0.0 | 15 | 40 | 495 | 132 | 40 | 3 | 25/25 |
| btrfs, after the fix | 16,634 | 0.0 | 11 | 45 | 381 | 148 | 60 | 5 | 25/25 |
| btrfs under load (after the fix) | 5,256 | 0.0 | 88 | 340 | 2,473 | 314 | 262 | 140 | smoke 0/5, failure scenarios 9/18 |

On tmpfs the worst stalls are first-use imports at startup (`gi.repository` in
`private_bus.py`, 8.6ms; `readiness`, 7ms); nothing else reaches 10ms.

**What blocks the loop on disk.** On btrfs, fsync is 79% of owner self time:
44.4s in 23 generations, 3.4–5ms each. That is 4.4–5.0s of fsync in each
13–20s smoke run. Under load it is 97%, at 28–35ms per fsync on average and
over 1s at worst. The rest of the Store code is 7% and other Python 10%. Window
queries, D-Bus, child spawns, libei, screenshots and GC are each 2% or less.
By site (the btrfs row above, before the fix; line numbers are the current tree):

| fsync site | Calls | Self ms | Worst ms | Heaviest single path |
| --- | --- | --- | --- | --- |
| `Store._write` file, `artifacts.py:522` | 3,510 | 17,378 | 62 | `application_windows` checkpoint from `Registry.tick`, `app_processes.py:614` |
| `Store._write` directory, `artifacts.py:531` | 3,510 | 17,018 | 71 | the same |
| `mkdir_durable`, `artifacts.py:78` | 1,560 | 5,718 | 65 | `Store.request` from `records.py:50`, for a status ping |
| `window_observation`, `artifacts.py:933`/`934` | 418 + 418 | 3,994 | 43 | `Query.step[observation]`, `windows.py:330` |

After the fix, an input or window request costs 16–22 fsyncs: the request
record (`records.py:50`: 4, plus one redundant before the fix), five more
record transitions at 2 each (admitted `records.py:62`, started `records.py:72`,
effects and finalizing `records.py:62`, terminal `records.py:33`), the window
observation, and the application window record (`app_processes.py:625`, then
the confirmed checkpoint at `:614`). A launch costs 52 (`Store.allocate` ×2,
`launch`, `application_prepare`, `applications.py:51`–`56`). Each
`session status` is 6, and `session start` sends one every 20ms
(`lifecycle.py:509`) until ready, 8–11 per start. Writes are not spread out:
the worst turns are a launch's admission and preparation (about 20 fsyncs in
one turn, 551ms on btrfs; under load a single launch step took 2.2s), then any
request's admission plus its first step, then status pings during startup.

**Time-sensitive work, measured.** On tmpfs no input timing was off by more
than 5.6ms, and no stall overlapped a held key or button. On btrfs, 34 stalls
(667ms in all) fell inside a held key or button (worst: 64ms of a 155ms
`application_windows` checkpoint). 4ms typed holds ran up to 57ms over,
20ms clicks up to 22ms over, 20ms wheel steps up to 34ms late. Under load a 4ms
hold ran 167ms over and a type gap 137ms. Focus rechecks were never more than
6.3ms late. A cancel's release came before its records were written
in all 12 cancel stalls that released something (0.1–0.9ms into the turn).
Neither was delayed.

Deadlines were (before the budgets [below](#budgets-now-writer-thread-next)):

- **Readiness window query, 0.5s** (`readiness.py:87`). Status-ping record
  writes used up its budget. Unloaded on btrfs, 1 of about 90 starts failed
  (556ms of status-ping stalls before the query could finish). Under load,
  starts failed often:
  - both attempts in the first load run;
  - 5 of 8 starts before the fix and 2 of 8 after (too few and too noisy to
    credit the fix);
  - 3 of the 18 load scenarios.
- **Request window query, 0.5s** (`windows.py:263`). It expired under load in
  4 of the 5 smoke runs and in 1 launch's window wait.
- **Health observation, 1s rounds** (`readiness.py:141`). It failed the
  session under load in 3 generations. In the load sets below, every such
  failure (7) was the 2s expiry of the last successful observation, not a 1s
  round timing out.
- **Request deadlines were owner stalls too (corrected).** This section first
  said that 2 `windows` requests under load timed out with no late probe while
  they were admitted, so the time went outside the owner. That matched late
  probes by when they were recorded, not by the time they cover, and across
  all profiles. The analyzer now matches each timeout to the late probes of its
  own generation that overlap its admitted time. In the two load sets below,
  all 20 request timeouts that weren't meant to happen had the owner late for
  94–100% of their admitted time. That includes the `windows` timeouts: 762
  of 812ms and 1,224 of 1,224ms before the budgets, and 971 of 1,016ms, 1,367
  of 1,367ms and 1,482 of 1,482ms after.
  The `title-gone` wait timeouts are part of that scenario and happen on tmpfs
  too (about 1,030ms admitted, of which the owner was late 51–64ms on idle
  btrfs).

**First decision: remove what is redundant, propose the rest.** A contained fix
cannot remove the cost: it is the per-transition durable write protocol run
synchronously on the one owner thread. What shipped is the part that keeps
every guarantee:

- `Store.request` no longer fsyncs the request directory a second time; the
  attempt directory's `mkdir_durable` has just done it.
- `window_observation` fsyncs the generation directory only when it links
  `window-observations/`, not on every query (`mkdir_durable(durable=...)`).

That removes 10% of fsyncs (1,278 → 1,149 per smoke run, 204 → 189 per failure
scenario; per request: key 20 → 18, launch 58 → 52, status 7.2 → 6.2). Lateness
before and after (rows above) is within run-to-run disk variance: idle btrfs
fsync time per run varied 3.4–5ms across sets.

Proposals, largest effect first. Each changes durability timing, ordering or
what is recorded, so none is shipped:

1. **Writer thread.** One FIFO thread owns the Store. The owner queues
   snapshot and event writes and keeps running. Effects that the docs gate on
   admission and start persistence wait for the writer's acknowledgement in a
   task phase, without blocking the loop. A single queue keeps per-request
   and `events.jsonl` order. This needs new synchronization (a queue, a GLib
   wakeup, failure hand-back) and moves the point where a persistence failure
   is seen. It is the only option that also fixes input timing under load.
2. **Fewer durable writes per request.**
   - (a) Fold the `admitted` transition into `Store.request`. It rewrites the
     same phase straight after (`records.py:50`, `:62`), costing 2 fsyncs per
     request. Records would start at revision 0, not 1.
   - (b) Write `effects` and `finalizing` without fsync, keeping `admitted`,
     `started` and `terminal` durable. A crash could then lose the
     intermediate phases.
   - (c) No durable record for `session.status`. It has no effects, and its
     6 fsyncs per ping are what used up the readiness query budget.
3. **Startup only.** Back off the CLI's 20ms readiness ping, or give the
   readiness window query a budget that owner stalls don't consume. This trades
   start latency, or tolerance, for the failed starts. It leaves input
   timing alone.
4. **Documentation only.** Advise a fast or tmpfs `--artifacts` root. tmpfs
   records do not survive a reboot.

### Budgets now, writer thread next

The decision on the proposals: give observation budgets the owner's own stall
time back now (the second half of proposal 3, made general), and move the
writes to a writer thread next (proposal 1, #96, before drag in #82).
Proposals 2 and 4 are not taken up for now.

**What shipped** (`owner_time.py`). The worker turns an `OwnerClock` at the
start of every owner tick. Owner stall time is the gaps between turns beyond
10ms, plus the current turn's overrun. A `Budget` of `s` seconds starts with
the deadline `start + s`. It moves that deadline later by the owner stall time
since the start, but by no more than `s`, and never past its limit (the startup
or request deadline). A window query's budget starts when the query is started,
not at its first step. Without stalls the deadline is no later than before.
Bounds for a hung KWin or bus, from when the wait starts:

| Wait | Budget | Worst case before | Worst case now |
| --- | --- | --- | --- |
| Window query work (readiness, targeting, activation, waits, close) | 0.5s | 0.5s | 1s, within the caller's deadline |
| KWin name check during start | 1s | 1s | 2s, within the startup deadline |
| Bus and KWin health round | 1s | 1s | 2s; once ready, the unchanged 2s observation expiry usually ends it first |

Gio's own call timeout is set to the latest the deadline can be. Not changed:
the `windows` request's 0.5s contract deadline (its query is capped by it),
other request deadlines, the 2s health freshness in the worker and the CLI, the
1s transport frame deadline, query cleanup (1.5s) and the connection budgets
(3s). Changing any of these would change the CLI contract or the detection of a
stuck worker.

**Measured** with the same runs on btrfs, idle and under load, on the builds
just before (`bb99248b`) and after the budgets. Scenarios leave out
`worker-stopped`; starts and request timeouts count all 25 runs.

| Set | Probes | p95 | p99 | max (ms) | Smoke | Scenarios | Failed starts: window query / name check | Health expiry | Request timeouts (on purpose) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| idle, before | 16,865 | 15 | 32 | 154 | 5/5 | 18/18 | 0 / 0 | 0 | 2 (2) |
| idle, after | 17,296 | 7.4 | 21 | 106 | 5/5 | 18/18 | 0 / 0 | 0 | 2 (2) |
| load, before | 4,055 | 120 | 470 | 3,762 | 0/5 | 6/18 | 10 / 1 | 1 | 9 (1) |
| load, after | 4,527 | 130 | 490 | 2,706 | 0/5 | 8/18 | 5 / 0 | 6 | 13 (1) |

- **Gone: failures where the owner stalled for less than the budget again.**
  Before the budgets, the owner was stalled for 82–100% of the 0.5s budget in
  each of the 10 failed starts. Half of them are gone, and so is the name-check
  timeout. With this few runs and this noisy a load that ratio is rough; the
  stall times before each failure are the firmer evidence.
- **Left: stalls longer than the budget again.** Each of the 5 failed starts
  left reached the new 1s cap, with the owner stalled for 91–100% of the second
  before the failing check, mostly in one turn of 0.65–2.3s.
- **Left: the 2s health observation expiry.** It failed 6 sessions, against 1
  before, each with the owner stalled for 78–97% of the 2s before. More
  sessions got through start into the workload, where these stalls are. The
  budgets don't change when a health round completes, so they can't cause
  these.
- **Left: request deadlines**, including the `windows` request's 0.5s. They
  are owner stalls too (above).
- **Unchanged: input timing.** Under load a 20ms click was held up to 682ms
  too long and a 20ms wheel step was 113ms late. Only #96 addresses that.
- **Idle:** everything passed both times, and the lateness difference is disk
  variance, as before.
- **The profile's own writes** (`profiler.write`) were 0.7–0.8% of owner self
  time on idle btrfs (4,026–4,728 writes per set, 0.07ms each on average) and
  0.1% under load. The slowest single write took 6.8ms, under the 10ms stall
  threshold.

#96 is done when the same loaded runs show owner probe lateness p99 under 20ms
and max under 100ms, no fsync in owner self time, input timing at most 10ms
over intent at p99, and no failure from these deadlines.
