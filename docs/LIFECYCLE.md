# Generation-owned service lifecycle

M3.1 implements service ownership, duplicate-start compatibility, live control
observation, and manager-side stop. The public `session start` command remains
`unsupported_operation` until #20 can require control, window, input and capture
probes. The internal Python manager and explicit tests-only controllers exercise
service creation in this milestone; there is no production fake-desktop flag.
A live infrastructure worker reports `state: starting, desktop_ready: false`.
A successful manager observation is not desktop readiness.

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
absolute artifact root, and the fixed 1280×720 scale-1 output. Timeouts, request IDs
and the caller cwd itself are not persistent configuration. A live compatible
internal start returns the same identity only after a correlated control response;
a conflict fails. A name-only start after positive old-service quiescence creates
a fresh token. An expected-generation start never creates a replacement lifetime.

Runtime storage adds `g/TOKEN/lifecycle.json` to the existing private layout. It
contains validated identity, derived unit name, expected cgroup path, configuration,
submission certainty and lifecycle state. The manager initializes durable records
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
and `TimeoutStopSec=3s`. It has no runtime expiry. Early service stdout/stderr append
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

Artifact attachment/update happens after termination, so a blocked or damaged
artifact record cannot prevent fallback stop. Successful stop reports
`records_preserved: false` if its terminal record could not be updated; it does
not claim that write succeeded. Repeating stop can repair that bookkeeping when
storage becomes available. Earlier manifest failure outcomes are preserved and reconciled into the lifecycle
result. If stop is the first call after unexpected worker death (including a clean
exit before a stop request), it retains a failed outcome while completing cleanup.
A timeout/signal caused by an earlier requested stop does not turn its stopped
tombstone into a prior crash. Worker exit only records uncertain cleanup; the manager asserts completion after
its cgroup observation. Unexpected worker death is reconciled on a later lifecycle
call. Autonomous terminal hooks and ordered release/window-close attempts remain
#21; full capability readiness remains #20.

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
nonzero; systemd terminates the generation's remaining cgroup. Public start is
still gated and status still says `starting`, `desktop_ready: false`: sockets
alone do not establish the #20 query/input/capture readiness contract.

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

Only after observed service quiescence does lifecycle reconciliation remove the
exact generation's disposable `desktop/` subtree. It retains routing claims,
lifecycle metadata and all durable artifacts. Root symlinks/unsafe ownership are
rejected and nested links are not followed. Disposal failure returns uncertain
cleanup and later lifecycle calls retry. Crash settings cleanup currently requires
that later stop/status/start reconciliation; automatic post-stop cleanup remains
#21. [Issue #19 evidence](../evidence/issue-19/README.md) records real installed
bus/KWin output, project access and environment isolation with readiness false.
