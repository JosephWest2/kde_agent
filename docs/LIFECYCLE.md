# Generation-owned service lifecycle

M3.1/M3.2 provide generation-owned services and private desktop construction.
M3.3 enables public start after real capability probes and adds live essential
health monitoring. A correlated control response and every required probe must
pass before start returns `state: ready, desktop_ready: true`. Installed probes
are explicitly `m1-provisional`, with `release_qualified: false` and replacement
owner #35 (M7.1); public desktop operations remain unsupported.

`session status` and `session stop` recognize managed generations. Standalone
foreground workers retain the previous transport behavior. Stop on an absent or
already stopped managed session succeeds. Absence returns a null actual generation,
including when the caller supplied an expected token: it does not authenticate
that token as a previous lifetime. A stopped managed generation retains its token
and terminal outcome. A supplied token that differs from the current name pointer
fails before unit calls, artifact writes or socket removal, even when the worker
is unavailable. Name-only commands resolve the current generation once.

## Ownership and compatible starts

The manager takes a bounded per-name flock across each lifecycle decision.
A random 32-hex generation determines `agent-desktop-TOKEN.service`, its exclusive
runtime directory, and its durable artifact directory. All currently supported
configuration participates in compatible-start comparison: headless mode, normalized
absolute artifact root, normalized dependency root, and the fixed 1280×720 scale-1 output. Timeouts, request IDs
and the caller cwd itself are not persistent configuration. A live compatible
start returns the same identity only after correlated control and current readiness;
a conflict fails. A name-only start after positive old-service quiescence creates
a fresh token. An expected-generation start never creates a replacement lifetime.

Runtime storage adds `g/TOKEN/lifecycle.json` to the existing private layout. It
contains validated identity, derived unit name, expected cgroup path, configuration,
submission certainty and lifecycle state. Older configuration records remain safely stoppable, but cannot silently
match a new dependency configuration. The manager initializes durable records
and publishes this ownership before submitting the service. Managed workers attach
the claim and artifact store; they never contend for the name lock or publish or
remove the name pointer. This avoids a startup-lock deadlock. Standalone workers
retain their original exclusive-claim behavior.

Generation claims, lifecycle metadata and name lock files are retained. After
stop, only positively quiescent generation socket residue is removed; the current
pointer remains as a terminal tombstone until the next name-only start replaces it.
Late cleanup checks the original generation before updating runtime state. A
replacement service has a different unit name, so old stop requests cannot target
it. No runtime or artifact directory is recursively removed by this milestone.

A timed-out submission may have reached systemd. Such ownership remains uncertain
until the exact unit is observed. An absent unit cannot prove that a delayed
submission will never materialize; the manager therefore refuses replacement or
successful retirement of an unresolved submission. A failed manager query also
never counts as proof of absence. A later observation of that exact unit permits
normal reconciliation. A stop already waiting for that submission issues exact-unit
termination when it first becomes visible, within its original deadline. There is
no automatic unsafe reservation reset.

## Manager and shutdown bounds

Lifecycle subprocesses use direct argv and a clean environment addressing the
current user's manager at `/run/user/<uid>`; this manager endpoint is confined to
lifecycle control. The worker receives an explicit private runtime and minimal
environment through `env -i`, independent of the manager's desktop variables and
credentials. The desktop owner constructs complete private settings/endpoints for
its bus, compositor, adapters and applications; see [environment policy](ARTIFACTS.md).

The installed Python runs the packaged worker with isolated Python imports from
`/`, without source cwd or PYTHONPATH dependence. The transient unit uses
`Type=exec`, `Restart=no`, `KillMode=control-group`, `SendSIGKILL=yes`, `UMask=0077`
and `TimeoutStopSec=3s`. It has no runtime expiry. Watchdog behavior is described below. Early service stdout/stderr append
to the generation's private durable worker log. Ordinary descendants inherit the
service cgroup; deliberate escape is outside the supported contract.

Start work is bounded by the request's maximum 30s, followed on failure by a
separate maximum 15s cleanup reserve. Status has a maximum 3s total manager budget;
stop has a maximum 15s total manager budget. Shorter caller budgets reduce these
work bounds. Name-lock waiting, subprocess calls and all worker transport phases
consume the same absolute monotonic budget. The separate start-failure reserve
never permits late successful start. Normal responsive local storage is assumed;
these are not hard real-time guarantees under a hung filesystem.

Stop uses the exact validated unit even when its worker socket is missing or
unresponsive. It returns cleanup complete only after no unit job remains, the
unit is inactive/failed, and the generation cgroup is absent or reports no
population (including descendant cgroups). Accepted stop submission and empty
MainPID alone do not establish cleanup. Uncertain cleanup preserves ownership.

Manager artifact attachment/update happens after termination. Intent or lifecycle
bookkeeping failure also cannot prevent fallback stop of the verified exact unit. Successful stop reports
`records_preserved: false` if its terminal record could not be updated; it does
not claim that write succeeded. Repeating stop can repair that bookkeeping when
storage becomes available. Earlier manifest failure outcomes are preserved and reconciled into the lifecycle
result. If stop is the first call after unexpected worker death (including a clean
exit before a stop request), it retains a failed outcome while completing cleanup.
A timeout/signal caused by an earlier requested stop does not turn its stopped
tombstone into a prior crash. The autonomous post hook preserves early worker
failure diagnostics even when a blocked graceful hook prevents the worker from
updating its aggregate manifest. It records ordinary-process/runtime cleanup;
the manager independently verifies whole-cgroup emptiness. The #21 shutdown
section below describes these hooks; readiness remains provisional in M3.3.

## Verification

The full unittest suite includes actual user services and fresh source-tree
controller/CLI processes. It requires the user manager and distribution gi; missing
prerequisites fail visibly. Tests cover parallel starts, start/stop ordering,
expected-token rejection, replacement cleanup safety, ambiguous submission,
pending jobs, remaining descendants, unsafe metadata, artifact lock failure,
short status budgets, actual worker death, and missing-socket stop of a frozen
worker that ignores SIGTERM. No private or personal desktop is started by #18.

[evidence/issue-18](../evidence/issue-18/README.md) separately records the installed
wheel worker, independent installed CLI processes, ordinary child/grandchild
cgroup membership, duplicate-start identity, stale requests and bounded fallback
termination. It is service evidence with `desktop_ready: false`, not release
qualification or completion of parent #3.

## Private desktop construction (#19)

The default packaged managed worker starts a private D-Bus daemon and KWin as
observed direct children. Its GLib owner polls startup without blocking control:
private bus socket, successful explicit bus Hello, then private KWin socket.
Construction shares a 30s monotonic deadline and continuously monitors bus/KWin
exit afterwards. Any essential exit or startup error makes the worker exit
nonzero; systemd terminates the generation's remaining cgroup. Construction alone still says `starting`, `desktop_ready: false`; sockets
alone do not establish the query/input/capture readiness contract.

`g/TOKEN/desktop/` contains exclusive 0700 runtime and HOME/XDG/TMP directories,
0600 bus configuration and owner-only bus/Wayland sockets. The bus has no service
activation directories or systemd activation. KWin uses the approved virtual
1280x720 scale-1 single output, no Plasma shell or XWayland, and no lockscreen,
global shortcuts or KActivities. ScreenShot2 permission is enabled only in that
created compositor's environment; no EIS permission override or global KDE edit
is used. Adapter/application environments never receive compositor controls.

Worker, bus and compositor output goes directly to preopened durable generation
logs. Internal application/adapter launches require output handles and preserve
selected executable, argv and cwd. This Python seam supports integration evidence
and future owners; it is not a public plugin or application-launch protocol.

The authenticated post hook removes the exact generation's disposable `desktop/`
subtree after proving owned ordinary processes are absent. Outside lifecycle
reconciliation waits for full service quiescence before retrying that disposal. It retains routing claims,
lifecycle metadata and all durable artifacts. Root symlinks/unsafe ownership are
rejected and nested links are not followed. Disposal failure returns uncertain
cleanup and later lifecycle calls retry. With a functioning post hook, crash
settings cleanup finishes automatically without a later stop/status/start call. [Issue #19 evidence](../evidence/issue-19/README.md) records real installed
bus/KWin output, project access and environment isolation with readiness false.

## Capability readiness and live health (#20)

One GLib owner uses Gio asynchronous explicit-address bus connection/authentication,
finite method calls and cancellable operation tokens. Late callbacks finish/dispose
results without installing connections or FDs. EIS FD handles are validated and
duplicated with Gio before transfer to the M1-audited libei owner. The input
connection remains alive, consumes events and requires a resumed keyboard. Both
FD and idle-continuation callback failures set sticky fatal state, gate input and
remove pending continuations; GLib exception logging alone is never treated as
owner failure propagation. Paused,
removed or disconnected input makes a previously ready generation unavailable.

After KWin registers on the private bus, the pinned kdotool fixed JS query validates
structured window metadata and observes the sole output name/dimensions. An empty
window list is valid. Successful script cleanup must be observed. A separate owned
capture process uses real ScreenShot2, drains full raw bytes through EOF, validates
fixed scale-1 metadata and a complete PNG, then publishes a receipt before the
parent's acceptance deadline. Any unconfirmed capture abort fails the session and
initiates owned-service cleanup; no host fallback or silent retry exists.

Prerequisites, service submission, private construction and all readiness phases
share the caller's maximum 30s startup budget. Query work is at most 0.5s, query
cleanup at most a separate 1.5s, input connection/resumption at most 3s and capture
acceptance at most 3s, each also capped by the remaining startup budget. Failed
startup has the existing independent maximum 15s cleanup reserve. Capability
failures retain component-specific receipts/logs and startup error context.

Bus and KWin health calls share one absolute 1s round every 1s, one round in flight
with no catch-up bursts. The KWin probe calls its actual `supportInformation`
method. Successful bus/compositor observations expire after 2s. Owner ticks and
control replies cannot refresh those timestamps: a delayed owner fails before
scheduler work or its next watchdog heartbeat, and the manager independently
rejects stale/missing/malformed essential timestamps. Bus failure makes compositor health unknown; a responsive bus with a failed
KWin call identifies compositor unresponsiveness. Child exits are polled before
scheduler effects and request admission. Status uses current manager/control and
worker observations within its complete maximum 3s budget. A nonresponsive worker
reports unavailable with cleanup pending; status does not wait a second 15s budget.

The approved systemd policy is `WatchdogSec=5s`, `WatchdogSignal=SIGTERM`,
`TimeoutAbortSec=3s`, `TimeoutStopSec=3s`, `FinalKillSignal=SIGKILL`,
`SendSIGKILL=yes`, `KillMode=control-group`, `NotifyAccess=main`, `Restart=no`.
A healthy GLib owner sends a heartbeat every 1s, including during starting while
its shared startup deadline remains valid. There is no heartbeat thread. A freeze
starts watchdog termination approximately 5s after the last heartbeat, with up to
3 additional seconds for forced termination. False timeouts fail the generation;
no automatic restart is permitted. These budgets assume normal host scheduling
and storage, not realtime guarantees under machine starvation.

Only systemd's notification socket/watchdog variables are preserved through the
fixed `env -i -S` bootstrap expansion; no caller text enters that expansion and
application/adapter environments never receive these values. Source fixtures can
exercise this policy while still reporting starting, without claiming readiness.

M7.1/#35 must replace/qualify the provisional provider and connect production
adapters before release qualification. The ordered graceful action shutdown and autonomous terminal record/runtime
cleanup hook are implemented by #21 below.


## Autonomous shutdown and finalization (#21)

A managed stop submits an independent systemd job. Its installed `ExecStop`
helper relays a generation-pinned priority shutdown with a fixed 2s deadline.
The GLib owner reserves response-delivery time, cancels ordinary work, caps
active cancellation cleanup at 0.5s, attempts tracked release for up to 0.5s,
and attempts normal application-window close with the remaining owner budget
(up to 1.8s total). Each hook must be bounded and nonblocking. A blocked callback
can prevent later hook attempts; the manager then terminates it and records
uncertainty. Cleanup continues when the initiating client disconnects.

Release and application-close adapters are **not connected in M3**. Their default
receipts say `not_connected`, `confirmed: false`, and identify #35. Internal
fixtures prove orchestration order; they do not qualify production input release
or application window closure. Provisional readiness resources close after the
ordered hooks, before direct-child disposal.

Systemd retains `KillMode=control-group`, `Restart=no`, a 3s `TimeoutStopSec`,
5s watchdog and 3s abort bound, and uses `TimeoutStopFailureMode=kill` for stalled
stop commands. `ExecStopPost` has its own fixed 2s internal deadline. The overall
supported ordinary-process shutdown bound remains 15s; per-command systemd timers
must not be mistaken for one overall 3s timer. The installed evidence records
actual normal, worker/dependency death/freeze and stalled-helper paths.

A stop-post command can begin while a SIGTERM-resistant descendant still lives.
The authenticated generation finalizer therefore pins remaining members with
pidfds, checks their exact service-cgroup membership, sends SIGKILL through those
pidfds and rescans within a 1s survivor budget. It excludes only its own verified
PID. It removes private desktop/settings and residual control sockets only after
all other owned members are absent. The generation directory, metadata, immutable
stop/relay records and routing pointer remain as ownership tombstones; they cannot
be reused for another service generation. Artifacts remain outside this subtree.

`terminal.json` preserves the stop-post's raw `SERVICE_RESULT`, `EXIT_CODE` and
`EXIT_STATUS`, explicit stop intent, final session classification and cleanup
observation. A `complete` post receipt means ordinary processes were absent and
disposable files removed while the finalizer was still running; it does not claim
the entire cgroup was empty. The independent controller verifies eventual unit
settlement and whole-cgroup emptiness. Later CLI retries retain that original
receipt and write `reconciliation.json` separately.

Explicit Manager or admitted external stop creates immutable intent. An internal
ExecStop relay, SIGTERM or watchdog signal cannot create user intent. Prior
session failures and watchdog/start failures remain failed. Requested fallback
termination, including a timeout or SIGKILL with no prior session failure,
remains stopped; its raw service result and unconfirmed graceful stages survive.
Unexpected clean or signal exits without explicit intent are failed.

Service hooks use the generation mutation lock and never acquire the per-name
lifecycle lock held by a waiting CLI. No generation lock is held while waiting
for service or transport completion. All metadata transitions preserve terminal
failure against stale startup/status snapshots, and stale hooks cannot touch a
replacement generation. Artifact-lock/write failure cannot prevent verified
process termination and settings disposal, but it prevents claiming the records
were preserved. Missing pidfd support or unverifiable surviving members withholds
settings deletion and complete status.

If the stop-post recorder itself is deliberately blocked or killed, systemd still
terminates the owned processes within the qualified bound, but that disabled
recorder cannot promise file disposal or a complete manifest. A later explicit
status/stop reconciliation retries cleanup after proving quiescence. The required
worker/bus/KWin/start/client failures complete autonomously with a functioning
recorder. Measurements assume ordinary killable processes and normal local
storage; uninterruptible kernel/filesystem stalls are not realtime guarantees.
