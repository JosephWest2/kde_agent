# M1 private-desktop feasibility harness

`tools/private_harness.py` runs a bounded development probe inside a private KWin
service, then removes that service's processes and disposable settings. It is
**feasibility tooling**, not the supported `agent-desktop` session CLI. Its
`foundation_ready` phase means private bus, native fixture, output and initial
presentation are available. Full REQ-009 readiness still requires the window,
input and screenshot probes in #11–#13. No application support or twenty-run
production reliability claim is made here.

Use the [recorded dependency setup](SETUP.md) on the Arch/KDE target, with a
running systemd user manager. The fixture additionally exercises the already
listed gcc, pkg-config, wayland-scanner, Wayland protocols/client and xkbcommon
packages; it does not install packages or use a Python package installer.

From the checkout:

```sh
/usr/bin/python -I tools/private_harness.py run
/usr/bin/python -m unittest discover -s tests -v
/usr/bin/python -I tools/private_harness.py run --inject after-fixture
/usr/bin/python -I tools/private_harness.py run --inject startup-timeout
/usr/bin/python -I tools/private_harness.py run --inject worker-kill
```

The three injected runs must return nonzero, record their intended failure and
report `cleanup.verified_empty: true`. Inspect each returned manifest. An
injected error is not a passing desktop operation. The default run builds the
fixture, opens it without a viewer, requests two visual states through separate
command processes, waits for matching presentation receipts, closes it and
verifies service cleanup. No keyboard/pointer injection or screenshot occurs.

Each invocation returns one JSON result on stdout; child output goes to durable
files. Exit status is 0 for a passing run, 1 for runtime failure, and 2 for invalid
command syntax. Argparse help/errors use its conventional text interface; this
is not the production JSON CLI contract. Full logs and generation manifests are
under `.local/harness-runs/<generation>/`; generated protocol code, the fixture
binary and its build receipt are under `.local/fixture-build/`. Both are ignored.
`--artifacts PATH` and `--build-root PATH` accept caller-relative paths. Artifact
roots must be owner-owned directories without symlink ancestors. Each run
creates a new exclusive generation directory.

## Probe interface for #11–#13

Pass an argv after `--` to replace the default smoke probe:

```sh
/usr/bin/python -I tools/private_harness.py run -- \
  /usr/bin/python -I /absolute/path/to/probe.py
```

The probe is a normal child of the worker in the **existing** service cgroup. It
can invoke additional independent commands in that same desktop. It inherits
explicit private endpoints plus `HARNESS_GENERATION`, `HARNESS_CONTROL`,
`HARNESS_ARTIFACTS` and `HARNESS_DEADLINE` (absolute `time.monotonic()` seconds).
There is no detached session registry, production launch API or environment
override option. Its cwd is the outer caller's cwd. Use absolute executable and
script paths when invoking from a different directory.

The fixture control socket accepts one bounded JSON object plus newline per
connection, with `generation`, `request_id` (1–64 characters) and `op`:

- `status` returns the last presented revision and observed output.
- `set_state` accepts uint32 `state`; success waits for that request's exact
  committed revision to receive presentation feedback.
- `close` asks the fixture to exit normally; it acknowledges the request, not
  process exit. The worker records the actual close exit during cleanup.

A helper usable from probe subprocesses is:

```sh
/usr/bin/python -I /absolute/path/to/tools/private_harness.py control \
  --socket "$HARNESS_CONTROL" --generation "$HARNESS_GENERATION" \
  --state 17 set_state
```

Control is separate from Wayland input and never fabricates an input receipt.
Malformed, oversized (>16 KiB), wrong-generation or invalid-state requests fail.
One request is serviced at a time. Busy, discarded, disconnect and timeout are
failures; do not automatically retry a state request after losing its response,
since its state may still render. Internal numeric control IDs keep delayed
feedback from an abandoned connection from satisfying a later request.

## Fixture and rendered-state semantics

The fixture is a custom-rendered native xdg_toplevel (`org.kde_agent.fixture`,
`KDE Agent Native Fixture`) with 640×360 client content. It draws XRGB8888 wl_shm
pixels, four fixed color regions and a 32-bit state marker, with no fonts,
animation, toolkit widgets or accessibility metadata. Configures are acknowledged
and buffers remain owned until compositor release. The actual Wayland output
snapshot must report one 1280×720 current mode, scale 1 and normal transform.

`fixture-events.jsonl` records actual key press/release, received xkb modifiers
and keysym/text, pointer entry/motion/button/axis events, and visual transitions.
Keyboard codes are Wayland/evdev codes; xkb lookup uses code + 8. The fixture does
not synthesize repeat events. Key/button receipt increments visual state; updates
may coalesce while a prior frame is pending. Every event has generation,
fixture PID, event sequence and a monotonic timestamp. Actual acknowledged
keyboard/pointer operation remains to be demonstrated with libei in #12.

`committed` means the fixture wrote and submitted that revision's pixels.
`presented` means `wp_presentation` reported presentation of the **same commit**;
it carries immutable revision, state, source, checksum and internal control ID,
plus the compositor's presentation clock/timestamp/flags. A later input event
cannot relabel an earlier frame. A discarded frame never yields rendered-state
success. `wl_surface.frame` is a scheduling hint and is not used as proof of
presentation. Missing/broken presentation capability fails startup explicitly.

On the recorded KWin virtual backend, feedback arrives without `sync_output`.
Events report `sync_output_received: false`, `output_matched: false` and
`sole_output_name: Virtual-0`; that name is attributed through the independently
validated single-output configuration. Presentation flags are recorded verbatim,
not interpreted as physical display timing guarantees. This virtual desktop has
no physical presentation or viewer requirement.

The pixel checksum is an FNV-style accumulator over generated uint32 pixel
values. It proves distinct fixture buffer contents, not a captured screenshot.
For #13, wait for the intended generation/revision's `presented` event, then
request a fresh screenshot and verify its actual pixels. The completed fixture
state stays unchanged until another control command or input event; use sample
regions away from decorations and cursor edges. Input acknowledgment alone does
not imply a rendered frame or generic application readiness.

## Separation and ownership

The outer controller contacts the host **user service manager only** to create,
observe and stop its unique `kde-agent-m1-<generation>.service`. The service
starts through `env -i` and isolated Python. Each child receives a constructed
private HOME/XDG/runtime/TMP, D-Bus address and Wayland display, with fixed
PATH/locale/US layout. No inherited host display, accessibility endpoint,
launch-as-scope, Python/loader override or credential variable is forwarded.
A deliberately nonexistent private system-bus address prevents system-bus
fallback. Installed read-only resources in `/usr/share` remain available.

The private bus has an explicit configuration with no service directories or
systemd activation. KWin uses `--virtual`, explicit 1280×720 scale 1/output count,
and disables lockscreen/global shortcuts/KActivities. No Plasma session,
XWayland, viewer, EIS permission override, screenshot permission override or host
input/capture adapter is launched. The temporary runtime/HOME and artifact
roots are 0700; private bus, Wayland and test-control sockets are explicitly 0600
inside that runtime. Ordinary descendants remain in the service's cgroup.

This is desktop/settings separation for trusted programs, **not filesystem or
network isolation**. Local project files remain accessible. Deliberate escape
from supervision/contact with other endpoints is outside this feasibility
contract. No ydotool, kwin-mcp, MCP SDK, global package install or host desktop
operation is used.

## Provisional enforced limits and cleanup

| Phase | Limit |
| --- | --- |
| Fixture build | 30s work plus at most 1s subprocess kill/reap |
| Package inventory / each manager call | 5s plus at most 1s kill/reap |
| Shared worker startup | 30s |
| Probe work | 60s |
| Fixture control/render response | 3s |
| Fixture graceful shutdown | 2s |
| Entire shutdown, including stop-post and observed empty cgroup | 15s |
| Independent systemd service lifetime | 95s from service start |
| systemd stop timeout / stop-post command timeout | 3s each |
| Controller overall run budget | 170s |

Worker waits share monotonic phase deadlines. `RuntimeMaxSec=95s` is an
independent backstop covering worker startup/probe hangs or controller loss;
`Restart=no`, `KillMode=control-group` and `SendSIGKILL=yes` terminate remaining
ordinary descendants. Stop-post removes only the generation-marked disposable
runtime and records service outcome, including worker signal death. The controller
checks an observed cgroup is empty/removed, not merely that stop returned 0.
Successful transient units can auto-unload before that observation; their
previously recorded cgroup remains the cleanup authority.

The total budget accounts conservatively for build (31s), package inventory (6s),
service submission (6s), lifetime/observation plus the last manager call (106s),
cleanup/controller command reaping (16s) and final diagnostics (5s): 170s.
Calls use the remaining overall/phase allowance, reserving shutdown time.
Observed complete shutdown is separately compared with the stricter 15s limit,
and exceeding the overall budget cannot return success. These are provisional
normal-host limits, not real-time scheduling guarantees on a stalled kernel or
filesystem. Bounds and timings are recorded per generation.

On normal completion the worker requests fixture close, then exits; systemd
owns termination of anything left. Injected startup failures include an ordinary
grandchild that changes process group and ignores SIGTERM. The worker-kill case
exercises cleanup without its finally block. Input release is inapplicable here;
#12 must add it with the input adapter. All logs, build provenance, manifests,
cleanup records and probe results survive removal of the settings tree.

The real boundary probe can be run from another cwd using absolute paths:

```sh
# From /tmp, substitute your absolute checkout path.
/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tests/private_harness_probe.py /tmp
```

It checks private socket modes, actual readable child environments, cgroup
membership, invalid control traffic and distinct presented states. KWin denies
`/proc/PID/environ` reads on this target; evidence records that observation
limitation alongside its explicit launch environment and working private
endpoints. See [issue #10 evidence](../evidence/issue-10/README.md) for actual
measurements, failure history and remaining milestone checks.

## Structured window/focus feasibility probe (#11)

Use the exact #9 candidate executable and the existing private probe seam:

```sh
# Substitute the absolute checkout path; repeat latency in three fresh runs.
/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tools/kdotool_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool functional
/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tools/kdotool_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool faults
/usr/bin/python -I /checkout/tools/private_harness.py run -- \
  /usr/bin/python -I /checkout/tools/kdotool_probe.py \
  /checkout/.local/issue9-clean/bin/kdotool latency
```

The probe refuses a missing/private-environment mismatch before connecting to
D-Bus and verifies the executable's hash against #9's selected artifact. It uses
the compiled kdotool CLI, unchanged, for every query, activation and forced script
removal. A read-only observer on the explicit private bus checks the exact owned
script name with `isScriptLoaded`; it does not implement another window transport.

`tools/kwin_window_query.js` is the fixed snapshot source. JSON results include
KWin UUID, reported PID, title, application class, separate global client/frame
rectangles, per-window active state and active UUID. Missing metadata is null;
empty available titles/classes stay empty. The fixture must actually supply
identity and exact content geometry. Frame geometry never substitutes for
client geometry. Dynamic values enter an ASCII-escaped JSON data declaration;
titles and classes are descriptive and cannot select one of equal candidates.
Schema validation rejects incomplete, duplicate, inconsistent or nonfinite data.

The functional scenario keeps the primary fixture healthy and launches a second
owned fixture solely to test ambiguous descriptions, real focus transitions and
a formerly valid vanished UUID. It closes/reaps that child through its own stdin.
This is a fault fixture, not a production multi-application interface. Focus first
checks existence, requests activation, then polls fresh snapshots until that UUID
is actually active. The successful-no-op activation case must time out. Closing
the second fixture must produce `target_missing` when its old UUID is focused.

The proposed query work deadline is **500ms**, focus deadline **2s**, and focus
poll cadence **100ms** between starts with no overlapping queries/catch-up burst.
A successful query includes parsing and independent script absence observation.
A successful focus requires a matching fresh snapshot before its absolute
deadline; a failed query never permits continuing with stale focus. Full latency
samples additionally include diagnostic writes and return to the probe caller.
The instrumented feasibility code writes considerable per-query evidence; it is
not the production worker adapter or a throughput benchmark.

Failures reserve **1.5s** separately for killing/reaping the owned CLI, unloading
only its exact script name if necessary, independently observing absence, and
removing its private temporary files. Each phase is clamped to the harness's
remaining budget. Request failure detection and final cleanup return are distinct
measurements: a 100ms deadline cannot interrupt JavaScript already executing in
the compositor. The fixed 750ms slow-script test detects its request timeout near
100ms but requires the script to finish before unload observation can complete.
If cleanup cannot be confirmed, the probe fails and the bounded service backstop
ends that private desktop; it cannot report per-query cleanup success.

Faults cover thrown errors, syntax failure without completion callback, absent or
malformed JSON, finite compositor delay, native missing-completion timeout and
SIGSTOP of a demonstrably registered owned CLI. No infinite compositor loop is
used. Native timeout is tested with a separate 12s outer budget because the pin
has separate 5s D-Bus/result/cleanup limits. That budget is not proposed for
ordinary polling. The fault test verifies the exact native timeout diagnostic,
script absence and fresh-query recovery. All scenarios retain stdout/stderr,
script inputs, per-query process/lifecycle/timing receipts and the final probe
report beside the usual harness logs and cleanup manifest.

[Issue #11 evidence](../evidence/issue-11/README.md) records the measured retain
assessment and reproducible samples. A proposed focus-loss detection window is
100ms poll delay plus the 500ms query budget, under the recorded target conditions.
This does not establish input release/cancellation timing. Production integration
must observe these helpers asynchronously; #13 must still demonstrate that a
slow query/cleanup cannot stall the GLib cancellation loop, and #12 must prove
actual libei release. Full session readiness and production support remain
outside this window-transport probe.
