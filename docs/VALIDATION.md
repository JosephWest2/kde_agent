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
| #80 | Owner-loop stalls | Measured: the stalls are synchronous fsyncs of artifact records on the owner thread. Harmless on tmpfs; on an NVMe btrfs disk the owner sits in fsync for about a third of each smoke run; and under heavy write load they break 0.5s/1s deadlines (failed starts, requests and sessions). Two redundant fsyncs removed (−10%); a redesign is proposed, not shipped. [Details](#80-owner-loop-stalls). | this section |

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
Neither was delayed. Deadlines were:

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
- **Health observation, 1s** (`readiness.py:141`). It expired under load in 3
  generations and failed the session.
- **Not owner stalls.** Under load, 2 `windows` requests in the death scenarios
  hit their request deadline with no late probe while they were admitted, so
  that time went outside the owner. The `title-gone` wait timeouts are part of
  that scenario and happen on tmpfs too.

**Decision: a larger redesign, which is your decision.** A contained fix
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
