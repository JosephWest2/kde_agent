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
