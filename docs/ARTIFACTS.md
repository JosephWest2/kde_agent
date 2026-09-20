# Caller paths and durable records

The CLI resolves relative application cwd, artifact root, screenshot export and
dependency root against `os.getcwd()` in the caller. Omitted application cwd means
that caller directory. The artifact default is `.agent-desktop/artifacts` beneath
the caller directory. Normalization is lexical; it neither requires destinations
to exist nor expands `~`, variables or globs. Symlinks have normal filesystem
meaning for application paths. The worker rejects relative/noncanonical wire path
fields and omitted launch cwd rather than interpreting them against worker cwd.
An omitted screenshot output remains unset for the future capture allocator.
Explicit screenshot output is a future export destination, not a replacement for
its generation-owned diagnostic record. Capture publication/export policy is #33.

Argv crosses transport unchanged. The executable policy helper resolves a name
containing `/` from normalized application cwd, and searches a bare name only in
the final application PATH. Relative and empty PATH entries mean application cwd;
an explicitly empty PATH has one such entry. Missing PATH uses `os.defpath`,
installed as a default, not the worker's ambient PATH. The selected executable
must be an executable regular file. Future launch rechecks cwd/spawn failures at
execution; selection alone does not establish process identity.

The environment helper removes protected values from the supplied base, applies
allowed explicit overrides (last duplicate wins), then installs private values.
PATH remains overridable. Protected settings are HOME, XDG_RUNTIME_DIR,
XDG_CONFIG_HOME, XDG_CACHE_HOME, XDG_DATA_HOME, XDG_STATE_HOME and XDG_CONFIG_DIRS.
The latter points to private config search; XDG_DATA_DIRS can retain ordinary
system resource search. Missing required private settings, session bus or Wayland
values fail closed. Private settings and bus paths must remain under private
runtime. M3 provisions those paths and verifies actual environment isolation.

DISPLAY, XAUTHORITY, WAYLAND_DISPLAY, WAYLAND_SOCKET, DBUS_SESSION_BUS_ADDRESS,
DBUS_SESSION_BUS_PID, DBUS_SESSION_BUS_WINDOWID, DBUS_SYSTEM_BUS_ADDRESS and
AT_SPI_BUS_ADDRESS are protected too. X11 and AT-SPI endpoints stay absent;
QT_ACCESSIBILITY=0, QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0 and NO_AT_BRIDGE=1 cannot be
overridden. The system-bus address names an absent private runtime socket, matching
the feasibility policy. Bus addresses allow one literal private `unix:path=`
address; alternate-address, option and percent-escape syntax is rejected. This is
not a sandbox against trusted programs deliberately contacting host endpoints.

## Store and producer boundaries

The internal foreground worker requires an explicit absolute `--artifacts` root.
It allocates the store before importing native GLib or publishing routing. Its
Python API also accepts a supervisor-precreated Store, validating immutable
session/generation identity; create and open are separate operations. Tests may
inject task doubles without pretending to establish a desktop. Production public
operations remain unsupported by their respective later milestones.

```text
ROOT/generations/GENERATION/
  manifest.json
  record.lock
  events.jsonl
  logs/{worker,compositor,bus}.log
  requests/REQUEST_ID/ATTEMPT_ID/
    record.json
    launch.json                  # dedicated future launch producer
    ALLOCATION_ID.KIND.partial
    ALLOCATION_ID.json
  applications/APP_ID/record.json
```

A generation is exclusively claimed and never reused; repeated request IDs get
independent attempt directories. Random allocations use exclusive creation with
at most eight collision attempts. Directories are 0700 and toolkit files 0600.
Existing unsafe components, symlinks, wrong owners and artifact roots overlapping
disposable runtime/settings are rejected. Unrelated user directories are not
chmodded. Worker/runtime shutdown never removes durable records. An allocator
returns a reserved `.partial` path and metadata; only the producing adapter may
mark it complete after its full validation. The allocator does not certify PNGs.

| Record | Producer |
| --- | --- |
| Identity, headless mode, requested 1280x720 scale-1 output, package/Python versions | Store creation |
| Actual output and native dependency version/source/hash/patch inventory | Explicit provenance API; M3/doctor real collectors |
| Worker PID and Linux start ticks | Actual worker; service identity remains uncollected for M3 |
| Admission, start, effects, cancellation, finalizing and accepted terminal result | Scheduler bridge and transport acceptance callback |
| Exact argv/cwd, application handle/process birth identity, selected executable, log paths and window refs | Dedicated Store APIs; M4 launcher/window producers |
| Allocated/partial/complete/failed artifact state and dimensions | Allocation API; M6 capture producer |
| Sticky session failure, cleanup state and final outcome | Store transitions; M3 authoritative supervisor/cleanup hook |

Absent real observations are explicitly `not_collected`; copied M1 dependency
versions are never presented as observations. Infrastructure loop exit records
cleanup `uncertain`, not proof of production descendant/service cleanup. A stored
PID is diagnostic identity, not authority to signal a future process.

Exact launch argv lives only in the private dedicated launch record. Callers must
keep credentials out of argv and supply them via environment variables or files.
Environment values, typed text, arbitrary exception strings, window titles and
raw context/partial dictionaries are not dumped. The diagnostic projector retains
validated generation-qualified handles, process birth references, known owned
artifact/log paths and fixed phase/code fields. Raw application/compositor logs
can contain sensitive text emitted by those programs; content redaction is not
promised. JSON CLI stdout never carries subprocess log streams.

## Atomicity and failures

Each update takes one nonblocking advisory-lock attempt. There are no sleeps,
spins or retry loops on the event-loop owner. Under the lock it reads the current
bounded snapshot, writes a private same-directory temporary, flushes/fsyncs,
atomically replaces, then fsyncs the directory. Interrupted updates leave prior or
new complete JSON. Failure of directory fsync after replace means durable commit
is uncertain; it does not imply the old version is still visible. Toolkit-owned
files use directory-relative no-follow operations. Append-only events are
supplementary; individual snapshots are authoritative and there is no multi-file
transaction or durable replay cache.

Generation and request/application snapshots are capped at 64 KiB, events at
4 KiB, dependency inventories at 128 entries and reference lists at 64 entries.
Full launch argv is written once in a separate record capped at the 1 MiB wire
limit. Required oversize records fail explicitly. Request history never grows the
generation snapshot or causes whole-history reads/rewrites. History is retained
and therefore consumes disk over time; no automatic evidence deletion is implied.

Admission and start persistence failures prevent new ordinary effects. Storage
time consumes the existing admission-based deadline. Cancellation, release,
reset teardown and mandatory stop/cleanup proceed first, independently of storage
failure. Durable updates then run best effort; inability to record cleanup never
cancels the cleanup owner or authorizes new effects. Original session failure is
sticky: successful cleanup cannot overwrite it with success.

`finalizing` is pending, never success. Transport performs final deadline and
serialization checks before freezing its accepted payload; only its terminal
notification writes the accepted outcome. If that postaccept write fails, the
response remains accepted and the record stays pending/uncertain, with a safe
`artifact_failed` diagnostic when a sink is available. Response receipt is not a
transactional guarantee that the terminal record was committed. Replaying an
uncertain launch/input is not automatic. Small synchronous storage operations
are measured under normal storage; hung filesystems are not covered by a hard
cancellation-latency guarantee.
