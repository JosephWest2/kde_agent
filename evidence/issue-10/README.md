# Issue #10 private harness evidence

Recorded on the Arch/KDE target on 2026-09-16. The native fixture ran on a private
headless KWin output reporting **one 1280×720 current mode, scale 1, transform 0**.
Its custom wl_shm states received revision-specific `wp_presentation` feedback.
No viewer, accessibility metadata, XWayland, keyboard/pointer injector or
screenshot adapter was used. This establishes the #10 foundation, not full
REQ-009 readiness or a supported application/production session implementation.

[ runs.json ](runs.json) records all **15 development attempts**, source hashes,
selected runs, retained failure summaries and a final check that every recorded
owned cgroup and disposable runtime was removed. Selected sanitized manifests,
fixture events, cleanup records and probe evidence are linked below. `${CHECKOUT}`
and `${RUNTIME}` replace local absolute paths; exact generation IDs, timestamps,
PIDs, source/binary hashes and measured results remain intact. Complete original
artifacts remain locally under `.local/harness-runs/<generation>/`.

| Scenario | Result | Total run | Complete shutdown | Evidence |
| --- | --- | --- | --- | --- |
| Default smoke, separate control invocations | Passed | 1.146s | 0.198s | [normal.json](normal.json) |
| Failure after fixture presentation, SIGTERM-resistant grandchild | Expected startup failure | 3.920s | 3.264s | [startup_failure.json](startup_failure.json) |
| Shared 30s startup stall, resistant grandchild | Expected startup timeout | 33.517s | 3.140s | [startup_timeout.json](startup_timeout.json) |
| Worker SIGKILL | Expected service signal failure | 0.790s | 0.193s | [worker_kill.json](worker_kill.json) |
| `/tmp` cwd, fake host endpoints, environment/control negatives | Passed | 0.907s | 0.166s | [environment_control.json](environment_control.json) |

Each selected run independently verified an empty/removed owned cgroup. Shutdown
measurements start before worker graceful cleanup (or immediately before injected
SIGKILL), include systemd stop-post, and end after outside-controller verification.
They are below the provisional 15s shutdown bound; all total runs are below 170s.
The startup timeout consumed its real 30s shared deadline. The resistant
ordinary grandchild called `setsid()`, ignored SIGTERM and remained in the unit
cgroup, proving cleanup beyond direct children/process groups.

KWin is `6.7.5-1`, systemd `261.3-1`, D-Bus `1.16.2-1`, Wayland `1.26.0-1`,
wayland-protocols `1.49-1`, libxkbcommon `1.13.2-1`, and gcc
`16.2.1+r23+gd564253eb6c8-1`. Manifests link the unchanged #9 dependency report by
SHA-256 and record compiler argv, generated protocol XML hashes, fixture source
and executable hashes, Python version, protected environment and service argv.
There are no maintained upstream patches or new package installations.

The final source hashes are:

- `tools/private_harness.py`: `a38fc6db86b2cb1fe6ee8493fe28efcb82b1b79852b5823bc72e41b6ae9fd9f6`
- `tools/wayland_fixture.c`: `0ccb9da6e6053b95445850ad3711217cd4e1015941b639b940b157f793f62869`

The final change after the selected failure/environment runs only added argv/cwd
to the existing per-process identity receipts. The selected default smoke ran
that final source; each earlier manifest retains its actual source hash.

The default smoke submitted states 17 and 42 through independent command
processes and received distinct checksums and matching presented revisions. The
boundary probe also covered 0 and uint32 maximum. Fixture input listeners emit
real Wayland keyboard/pointer receipts, but **no real injected input was exercised
here**; #12 must validate that path. Buffer checksums establish different drawn
content; **no screenshot pixels were captured or verified**; #13 must wait for
the intended presented revision before issuing a fresh capture.

The environment probe supplied fake host display, session bus, accessibility,
HOME/XDG and launch-as-scope values while invoking from `/tmp`. It verified
actual readable bus/fixture/probe environments and cgroup membership, 0700
runtime/HOME and 0600 bus/Wayland/control sockets. It rejected wrong-generation,
invalid-state, malformed JSON, non-object and oversized requests without changing
the visible revision. KWin denies `/proc/PID/environ` reads on this target; that
limitation is explicit in the evidence, alongside its constructed launch
environment and observed private desktop endpoints.

Presentation feedback did not include `sync_output` on this installed virtual
backend. Events explicitly record that absence and identify `Virtual-0` from
the independently checked sole output. Feedback clock/flags are raw compositor
values and do not establish physical display timing. The fixture never treats
buffer commit, frame pacing, an input receipt or an arbitrary delay as presented
state. Unit tests additionally prove that intervening input/delayed old control
feedback cannot satisfy a new request, and a discarded exact revision fails.

All **18 tests passed** (0.883s), including the 12 unchanged dependency tests and
six environment/path/control-correlation/deadline boundary tests. These tests do
not substitute for the real KWin evidence above. The fixture compiled with
`-Wall -Wextra -Werror`, and `git diff --check` passed.

During development, one successful desktop run initially returned failure when
the cleanup observer attempted to stop an already auto-unloaded successful unit.
Its original failed result is retained, with separate postmortem proof that the
owned cgroup/runtime were removed. Two boundary attempts rejected D-Bus's default
0777 socket mode behind the private 0700 parent (one repeated because an edit
command used the wrong cwd); sockets are now explicitly 0600. Another probe
failed on KWin's denied environment read; the final probe records that observation
limitation. Every failed attempt retained diagnostics and was checked for owned
process/runtime cleanup. KWin's only selected stderr diagnostics were two
pre-existing RADV nonconformance warnings; no prompts occurred.

Use the commands and bounded probe contract in
[PRIVATE_HARNESS.md](../../docs/PRIVATE_HARNESS.md) to reproduce these checks.
Windows/focus (#11), real libei acknowledgments/device lifecycle (#12), fresh
capture/control-loop integration (#13), the broader production failure matrix,
representative application and twenty-run requirement remain outstanding. No
excluded tool, MCP SDK, host desktop operation or global installation was used.
