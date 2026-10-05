# agent-desktop command contract

The CLI validates requests and manages persistent private generations. `doctor`
checks runtime prerequisites without starting a desktop. `session start` waits for
real control, structured window query, resumed EIS input and a complete screenshot;
`status` observes current service and worker health; `stop` is idempotent.

One list in `contracts.SUPPORTED_OPERATIONS` defines what is implemented:
- `--help` marks other commands "(not yet implemented)";
- `doctor` reports `supported_operations` and `unsupported_operations`;
- `session status` lists the desktop operations available once the session is ready.

Currently supported: `doctor`, `session start|status|stop`, `launch`, `windows`,
`focus`, `wait`, `key`, `type`, `click`, `move`, `scroll`, `screenshot`, `close` and `kill`.
Every command in the table below is supported. There is no `input reset`; recover
from `input_uncertain` with `session stop` and `session start`. Key names, text limits, pointer coordinates, scroll signs and release guarantees are
in [keyboard and pointer input](INPUT.md).

## Screenshots

`screenshot [--window REF] [--output PATH]` captures the 1280×720 output as PNG.
With `--window`, the image is the window's client area, rounded outward and
clipped to the screen; a fully offscreen window fails with `capture_failed`. This
is the coordinate space of `click`, `move` and `scroll` with `--window`: for a window that is fully on screen,
a pixel at (x, y) in the image is `click --x x --y y`. If the window extends past
a screen edge the image is clipped, so add `crop[0] - client.x` and
`crop[1] - client.y` (or click the screen point `crop[0] + x`, `crop[1] + y`
without `--window`). It includes a GTK header bar but not a KWin title bar (Qt/KDE
apps); full-screen screenshots show both. The window doesn't need focus.
Each capture has a unique `capture_id` and is stored in the artifact root. The
result gives `path`, `png_sha256`, `png_bytes`, `dimensions`, `screen_dimensions`
and `crop`, plus `window`, `client` and `frame` with `--window`. `--output` copies the PNG
to a file path, or into an existing directory as `capture-ID.png`. If the copy
fails, the result is `artifact_failed` with the capture in `partial_result`. A
failed screenshot fails only that request, not the session. See [application ownership](APPLICATIONS.md),
[window discovery](WINDOWS.md), [lifecycle](LIFECYCLE.md) and
[transport](TRANSPORT.md).

## Waits

`wait` polls the window observation (at most every 100ms, one query at a time)
until its condition holds or its deadline (default 10s, at most 60s, queue time
included) expires with `timeout`. It never sends input or changes focus. A wait
holds the ordinary execution slot while it runs, so other requests to the same
session queue behind it ([scheduling](SCHEDULING.md)).

| Condition | Target | Satisfied when |
| --- | --- | --- |
| `window` | `--app` | the app has at least one `window`-kind row |
| `focus` | `--window` | that window is observed active |
| `exit` | `--app` | the app and all its descendants have exited |
| `title` | `--window`, `--match TEXT [--regex]` | that window's title matches |
| `gone` | `--window` | that window is no longer listed; the app may keep running |

**`wait --for title --window W --match TEXT`** matches a case-sensitive substring
of W's current title. `--match` is 1–256 characters and only valid with `title`.
- An initial match returns on the first poll (`polls: 1`).
- A null or empty title never matches; the wait keeps polling.
- W absent on the first observation gives `target_not_found`; W disappearing
  later gives `target_lost` (`context.phase: title_wait`), as for `wait --for focus`.
- A `popup` or `compositor` row gives `unsupported_operation` with reason
  `popup_surface` or `compositor_surface`, as for any other `--window` command.
- A timeout's context has `phase: title_wait`, the `window` and, once a query
  has completed, `last_query_artifact` (the last observation, with the title it
  saw). `focus` and `wait --for focus|gone` timeouts carry the same fields
  (phases `focus_wait`, `gone_wait`); cancellation keeps them too.
- The result has the focus-style target fields (`window`, `app`, `client`,
  `frame`, `focused`, `active_window`, query times), plus `title` (the matched
  title), `row` (the full window row), `match` (`{text, regex}`) and `polls`.

With `--regex`, `--match` is a Python `re` pattern (full syntax, inline flags
such as `(?i)` included) searched with `re.search` semantics. The CLI compiles
it before sending the request: a compile error is `invalid_arguments` with
`context.field: match`, `reason: invalid_regex` and the error `position`; the
pattern is never echoed. The worker checks only the pattern's type and length
(at most 256 characters) and never compiles it, so a pattern sent directly over
the transport by another client is fully validated only when matching starts:
the helper's compile error ends the wait with `invalid_arguments`,
`reason: invalid_regex` (exit 2) and `position: null`. A pattern that can match
the empty string (`a*`) matches every non-empty title, so it succeeds on the
first poll. It isn't rejected, because deciding that means running the pattern,
which validation never does.

Because `re` can backtrack exponentially, and even compiling a 256-character
pattern can take over 100ms (wide case-insensitive classes), neither runs in the
worker's event loop. Each new title is checked in a short-lived helper process
(`python -I`, empty environment, working directory `/`), started only when the
title changes. It costs about 15ms. The pattern and title reach the helper on
its standard input, a private in-memory file (memfd), never on its command line
(`/proc/PID/cmdline` is readable by other processes); it inherits no other
descriptors. The helper arms its CPU limit before reading them and may use at
most 100ms of CPU time to compile and search; a real pattern and title (at most
4096 characters) take well under 1ms. A pattern that exceeds it ends the wait
with `invalid_arguments`, `reason: pattern_too_slow` (exit 2), and the helper is
killed and reaped. It is never retried. Cancellation, timeout and
`session stop` kill a running helper too. A helper that crashes, or doesn't
answer within 2s, gives `internal_error` (`reason: regex_helper_failed` or
`regex_helper_unresponsive`).

**`wait --for gone --window W`** succeeds on the first observation that doesn't
list W. Closing a dialog or one of several windows can be confirmed this way;
`close` and `wait --for exit` need the whole app to exit.
- If W is already absent on the first observation, the wait succeeds at once
  with `already_gone: true` (and `last_seen: null`). A typo in the UUID looks
  the same, so check `already_gone` when you expect the window to have existed.
- Otherwise `already_gone` is false and `last_seen` is W's last row (title,
  kind, geometry).
- Any row kind may be awaited, so `gone` also works for a tooltip, popover
  (`popup`) or KWin's window menu (`compositor`).
- The ref's generation must be the session's: a stale ref is
  `generation_mismatch`, never "gone".
- The result has `condition`, `satisfied`, `window`, `already_gone`, `last_seen`,
  `polls`, and the observation's `generation`, `query_id`, `query_artifact`,
  `observed_at` and `accepted_at`.

Query faults (`window_query_failed`), session failure and generation changes end
any wait with that error rather than being treated as "not yet".

## References

Applications and windows are addressed by generation-scoped references. In JSON
output, every handle object (`{"generation": ..., "application_id": ...}` or
`{"generation": ..., "window_id": ...}`) also carries a `ref` string,
`GENERATION:ID`. Pass that string straight to `--app` or `--window`:

```sh
ref=$(agent-desktop --json launch --wait-window -- /usr/bin/gnome-text-editor | jq -r .result.application.ref)
agent-desktop --json focus --app "$ref"
```

Window IDs keep KWin's UUID spelling, braces included when KWin reports them.
Handle objects are also accepted as JSON wherever the worker protocol takes a
handle. A `ref` inside an object must match its fields.

## Install and inspect

`tools/setup.sh` installs the CLI into the project venv
`.local/dependencies/venv` (with `--system-site-packages`) after building the
pinned dependencies; see [setup](SETUP.md). No global installation is needed:

```sh
tools/setup.sh
.local/dependencies/venv/bin/agent-desktop --help
.local/dependencies/venv/bin/agent-desktop --json session start --help
.local/dependencies/venv/bin/python -m agent_desktop --version
```

The session service runs the worker with the interpreter the CLI was installed
into (`python -I -m agent_desktop.worker`), so a `pip install .` into any other
venv must also give it the distribution bindings (`--system-site-packages`), and
must be repeated after code changes. Packaging uses setuptools, requires Python >=3.11, and has no pip runtime
dependencies. The Python floor does not claim native adapter support on all those
interpreters. Platform native bindings remain governed by [setup](SETUP.md).
Installing or inspecting the CLI does not import native desktop adapters or run
feasibility scripts. `doctor` checks that the interpreter the session service
will use (`python -I`, which ignores PYTHONPATH) can import
`agent_desktop.worker`. It validates libei through the same binding the worker
uses: x86_64, soname `libei.so.1`, version 1.0 or newer, and every required
symbol. It warns, without failing, when the KWin or libei version differs from
the tested one (`warnings`); run the smoke test then. It also checks installed
runtime executables, the pinned
kdotool build receipt/binary, native bindings/libei and the user service manager.
Missing/incompatible dependencies include repair instructions. It does not install
anything or certify capability execution or release support.

Output is readable by default. `--json` emits exactly one JSON object and newline
on stdout for success, help, version, parser failures, unsupported commands and
operation failures. Diagnostics use stderr; application and desktop output go to
separate logs (see [Logs](#logs)). Default errors use stderr and leave stdout empty. Parser failures
use controlled messages without repeating offending values. `--help` is readable
by default; in JSON its text is `result.help`. `--version` likewise produces
readable output or `result.version`.

`--json` may precede the command or appear among its options, including between
nested command names. Option abbreviations are rejected. No supported path asks
for input, opens a prompt, reads text from stdin, runs an implicit shell, follows
logs indefinitely or automatically retries an action.

## Commands, flags and defaults

Every session-addressed command takes `--session NAME` (default `default`),
`--generation TOKEN` (default current generation) and `--timeout SECONDS`.
`doctor` only takes the timeout and its dependency-root option. Session names are
1–64 ASCII letters/digits/underscore/hyphen, starting with a letter or digit.
Generation tokens are 32 lowercase hexadecimal characters.

| Command | Other arguments | Default / maximum work seconds |
| --- | --- | --- |
| `doctor` | `--dependency-root PATH` (default `.local/dependencies`) | 120 / 120 |
| `session start` | `--mode headless`; `--artifacts PATH` (default `.agent-desktop/artifacts`); `--dependency-root PATH` (default `.local/dependencies`) | 30 / 30 |
| `session status` | none | 3 / 3 |
| `session stop` | none | 15 / 15 |
| `launch` | `--cwd PATH`, repeated `--env KEY=VALUE`, `--wait-window`; required `-- PROGRAM [ARG ...]` | 10 / 60 |
| `windows` | optional `--app APP_REF` | 0.5 / 0.5 |
| `focus` | exactly one of `--window WINDOW_REF` or `--app APP_REF` | 2 / 2 |
| `wait` | `--for window\|focus\|exit\|title\|gone`; window/exit require `--app`, focus/title/gone require `--window`; title requires `--match TEXT` (optional `--regex`). See [Waits](#waits) | 10 / 60 |
| `key` | required `--window WINDOW_REF`, positional `CHORD`; `--hold SECONDS` (default 0.05, at most 2) | 3 / 3 |
| `type` | required `--window WINDOW_REF`, positional literal `TEXT` (empty allowed) | 3 / 30 |
| `click` | `--x INT --y INT`, client coordinates with `--window WINDOW_REF` or screen coordinates without; `--button left\|middle\|right` (default left); `--count 1-3` (default 1) | 3 / 3 |
| `move` | `--x INT --y INT`, as for `click`; moves the pointer (hover) and presses nothing | 3 / 3 |
| `scroll` | `--x INT --y INT`, as for `click`; `--dy -50..50` (positive down) and `--dx -50..50` (positive right) wheel notches, default 0, not both 0 | 3 / 3 |
| `screenshot` | optional `--window WINDOW_REF` (crop to its client area), optional `--output PATH` (file or existing directory) | 3 / 3 |
| `logs` | optional `--app APP_REF`; `--source all\|application\|worker\|compositor\|bus` (default all); `--tail 0-200` (default 20) | 3 / 3 |
| `close` | exactly one of `--window WINDOW_REF` or `--app APP_REF` | 5 / 60 |
| `kill` | required `--app APP_REF` | 5 / 15 |

For installed execution from another directory, pass absolute paths:

```sh
agent-desktop --json doctor --dependency-root /path/to/project/.local/dependencies
agent-desktop --json session start --session work \
  --dependency-root /path/to/project/.local/dependencies \
  --artifacts /path/to/project/.agent-desktop/artifacts
agent-desktop --json session status --session work
agent-desktop --json session stop --session work
```

Dependency root participates in duplicate-start compatibility. Omitting it from a
different cwd selects a different default root and can produce a configuration
conflict. The wheel includes its own Python and KWin script (`window_query.js`) code; the worker never
imports the checkout's tools/evidence or uses caller PYTHONPATH. Runtime setup does
not require Rust/compiler tools once the pinned build is prepared.
`session start` also fails with `session_conflict` (reason `install_in_progress`,
`context.lock`) when `tools/setup.sh` is still installing the package at the start
deadline; retry once setup finishes ([setup](SETUP.md#rerunning)).

Timeout and hold values must be finite and strictly positive. No unbounded mode
exists. Key and type targets always use a window; click, move and scroll use one
unless given screen coordinates. App selection for focus/close must resolve unambiguously.
Titles are descriptive, never identities. Pointer coordinates are nonnegative
integer pixels, bounds-checked against current geometry before dispatch. Key,
text, pointer and scroll-step validation happens before anything is sent; see
[keyboard and pointer input](INPUT.md).

`launch` recognizes the first literal `--` after the command as the end of toolkit
options. It cannot supply a missing value for `--cwd`, `--env` or another option.
A program must follow; all following tokens, including another `--`, `--json`,
`--help`, spaces, leading hyphens and shell-looking text, remain application argv.
Neither application help nor application JSON flags select toolkit behavior.
Pass leading-hyphen literal text after a `--` delimiter too.

Environment names use `[A-Za-z_][A-Za-z0-9_]*`. Values may be empty or contain `=`;
the last repeated explicit key wins. NULs are invalid in all argument strings.
The initial desktop configuration is one 1280×720 scale-1 output. A different
mode is explicitly unsupported; future viewing needs no backend/plugin framework.
`close` requests normal closure and reports application exit, leaving confirmation
dialogs to explicit interaction; it never escalates to a signal. `kill` separately
terminates verified owned application processes, never an arbitrary PID.

## Identity and paths

Application references are `GENERATION:APPLICATION_ID`; application IDs contain
ASCII letters/digits/underscore/hyphen. Window references are `GENERATION:UUID`,
accepting both native brace-wrapped KWin UUIDs and unwrapped UUIDs. Their exact
spelling is preserved, for example:

```text
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:{2a63a414-1509-460a-bff9-b7c1103ba8d5}
```

JSON handles use `{generation, application_id}` or `{generation, window_id}`.
A handle supplies its expected generation. A conflicting explicit generation or
another conflicting handle fails `generation_mismatch` before dispatch.
Name-only lookup deliberately resolves the *current* generation; it does not
protect against restart. The client pins the resolved generation once; the worker
checks name, generation and handles before dispatch. A correlated worker response
carries its verified identity. Before such a response, actual `session.generation`
is null; an unverified expectation is never reported as an actual identity.

The client captures its absolute caller cwd and normalizes relative
cwd/artifact/output/dependency paths before transport. Omitted application cwd
means caller cwd. Slash-containing executable names resolve against application
cwd; bare names use the final application PATH. No shell expansion is performed.
Protected environment overrides are rejected by both client and worker.

Owner-private generation records survive runtime cleanup. Launch producers retain
exact argv: keep credentials out of argv and supply them through environment
variables or application-owned files. Environment values and typed text are never
recorded by toolkit diagnostics; application output can itself contain sensitive
text. See [paths, environments and durable records](ARTIFACTS.md) for defaults,
producer boundaries, atomicity and storage-failure semantics. Desktop/settings
separation is not filesystem or network isolation; applications remain trusted.

## Logs

`logs` returns where each log is, its size, whether it is complete and its last
`--tail` lines (default 20, at most 200, from at most the last 16 KB of the file).
It never returns a whole log; read the `path` for more.

- With no `--app`, `application` means the most recently launched application.
  `--app REF` selects any application of this session, including exited ones,
  and cannot be combined with `--source worker|compositor|bus`.
- `worker`, `compositor` and `bus` are the session's own logs. They are never
  `complete` while the session runs.
- An application log is `complete` once the application and all its descendants
  have exited. Its path is the one `launch` returned and never changes.
- `truncated` means there is more before the returned lines. ANSI color codes
  and other control characters are removed from returned lines, not the files.

```json
{"logs":[{"source":"application","stream":"stderr","application":{"generation":"…","application_id":"…"},
  "path":"/…/requests/…/….stderr.log","complete":false,"bytes":812,"tail":["Gtk-WARNING …"],"truncated":true},
 {"source":"worker","path":"/…/logs/worker.log","complete":false,"bytes":0,"tail":[],"truncated":false}],
 "tail":20}
```

## Requests, results and errors

The shared Request contains `schema_version: 1`, a fresh `request_id`, operation
(`session.start`, `click`, etc.), session name, `expected_generation`,
`arguments`, finite `timeout_seconds`, and `caller_cwd`. Pure validators consume
ordinary data; the worker additionally enforces strict wire types, framing/version
and immutable live worker identity independently of the CLI.

For example, `agent-desktop --json session status --session nosuch` fails with
this shape (request IDs vary):

```json
{"schema_version":1,"request_id":"8262d4ac689e412faf5b548d6ab7aab7","operation":"session.status","ok":false,"session":{"name":"nosuch","generation":null},"result":null,"error":{"code":"session_not_found","message":"Session does not exist.","context":{},"outcome":"not_started","partial_result":null}}
```

Success has a result object and `error: null`; failure has `result: null` and an
error object. Parse failures may have `operation: null`. Doctor/help/version have
`session: null`. Every response includes a fresh request ID. Error context is
extensible; incompatible envelope changes require a new schema version. Stable
codes may be added without breaking version 1.

| Exit | Stable error codes |
| --- | --- |
| 0 | success |
| 2 | `invalid_arguments` |
| 3 | `prerequisite_missing`, `prerequisite_incompatible` |
| 4 | `session_not_found`, `session_failed`, `session_unavailable`, `session_conflict`, `generation_mismatch` |
| 5 | `unsupported_operation`, `unsupported_input` |
| 6 | `target_not_found`, `target_ambiguous`, `target_lost` |
| 7 | `application_active`, `application_exited` |
| 8 | `timeout` |
| 9 | `input_failed`, `input_unavailable`, `input_uncertain` |
| 10 | `capture_failed` |
| 11 | `protocol_error`, `transport_error`, `completion_unknown` |
| 12 | `artifact_failed` |
| 70 | `internal_error` |
| 130 | `cancelled` |

Errors contain controlled `message`, useful `context`, `outcome` and
`partial_result`. Outcomes are `not_started`, `partial` or `unknown`. Available
application handles, process identity, logs, windows and completed steps survive
partial failure. A lost response after dispatch means completion may be unknown.
Launch and input are never automatically retried. Request IDs support correlation
and cancellation, not exactly-once execution. Cancellation cannot undo a completed
launch, click or GUI change.

Representative launch and window candidate shapes (full discovery also returns observation and cleanup metadata):

```json
{"application":{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","application_id":"app-1"},"process":{"pid":1234,"start_time_ticks":5678},"logs":{"stdout":"/artifacts/app-1.stdout.log","stderr":"/artifacts/app-1.stderr.log"},"windows":[]}
```

```json
{"windows":[{"window":{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","window_id":"2a63a414-1509-460a-bff9-b7c1103ba8d5"},"kind":"window","pid":1234,"title":null,"class":null,"client":{"x":0,"y":0,"width":640,"height":480},"frame":null,"active":true,"app":null,"association":{"reason":"unverified_process","verified_at":null,"process":null}}]}
```

Input results report `dispatched: true` (see [INPUT.md](INPUT.md) for the full
fields). Pointer results (`click`, `move`, `scroll`) give the point as `x`, `y` and
the screen point where the pointer now is as `screen_x`, `screen_y`; `scroll` adds
`dx`, `dy` and `steps`. A `scroll` that fails after its first wheel step reports
`steps_sent`, `steps_total`, `dx_sent` and `dy_sent` in the error context, as `type`
reports `strokes_sent`. Exit codes are the ones in the table above: 2 for bad
coordinates or steps (including `zero_scroll`), 6 for `target_lost`, 8 for a
`timeout` (phase `budget` when nothing was sent), 9 for input errors. Input dispatch does not promise application acknowledgment, a rendered
frame or UI readiness. Screenshot success reports the fresh complete PNG's path
and the other fields listed under [Screenshots](#screenshots).

A launch/window-wait failure retains its handle, for example this illustrative
error object within the common failure envelope:

```json
{"code":"timeout","message":"Window wait expired.","context":{"phase":"window_wait"},"outcome":"partial","partial_result":{"application":{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","application_id":"app-1"},"process":{"pid":1234,"start_time_ticks":5678},"logs":{"stderr":"/artifacts/app-1.stderr.log"},"windows":[]}}
```

Each window row has a `kind`: `window`, `popup` (tooltips, menus, popovers) or
`compositor` (KWin's own surfaces, such as the window menu). Only `window` rows are
selected by `--app` or accepted by `--window`; an explicit popup or compositor row
gives `unsupported_operation` with reason `popup_surface` or `compositor_surface`.
While a compositor row is listed, `key`, `type`, and `click`, `move` or `scroll` with `--window`, fail with
`target_lost`, reason `compositor_surface_open`: outcome `not_started` when found
before the first stroke, or outcome `unknown` with input progress when a focus
recheck finds it mid-input. See [row kinds](WINDOWS.md#row-kinds-windows-popups-and-compositor-surfaces).
Ambiguity returns `target_ambiguous` with `context.candidates` containing full
window handles. Uncertain release returns `input_uncertain`, `outcome: unknown`,
and context describing the uncertainty; further input is blocked until the
session is stopped. Close/kill report `application`, `exited`, `exit_status` (null if
unknown), and `remaining_processes`; timeout retains available partial outcomes.
For close, `exit_status` is the root return code (also `root_returncode`), and
`remaining_processes` is null because complete enumeration is unavailable. The
bounded `process_state` reports cached subtree population, root reaping and
whole-app completion with observation time. `exited` is null when liveness is
unknown. `application_snapshot` and exact `window`/query references remain in
partial failures. `close_state` distinguishes uncertain dispatch from completed
transport; transport completion does not prove application acknowledgment.
Explicit UUID close requires a positively verified owned application association;
an existing unassociated surface gives `unsupported_operation` with reason
`application_association_unavailable` before dispatch. After dispatch window
absence is expected and does not satisfy app exit. See [close semantics](WINDOWS.md#graceful-selected-window-close).
No generic success claim is made when an application remains alive.

Explicit `kill --app` uses a fixed TERM → KILL → observe-only sequence under its
original deadline, including queue time. Cancellation or disconnect stops further
signals immediately; it does not undo submissions or terminate survivors during
cleanup. `kill_state` contains the phase, fixed cutoffs, bounded attempt/submission
counts and up to 64 verified identity samples with observation timestamps.
Samples are incomplete unless whole-app completion is positively established;
unavailable `remaining_processes` is null, while successful completion reports
`[]`. `sample_truncated` indicates possible omission at sample capacity, not an
exact omitted count. A completed historical handle succeeds with
`already_exited: true` and zero signals. `exit_status` is the original child's
code, not a descendant aggregate. See [termination semantics](APPLICATIONS.md#explicit-termination).

## Deadlines and cleanup

The command table defines finite *work* budgets. The transport worker admits a
monotonic deadline and checks success acceptance; its production dispatcher fails
unwired operations immediately. The worker supplies cancellable task execution and queue
deadlines, including queue delay; a client socket timeout is not enforcement. Composite operations share one budget: target
checks, focus verification and key holds must all fit the remaining input time.
Individually valid settings do not guarantee completion within that time.

The work limits retain M1 choices; 10/60s wait and 5s close/kill defaults are
provisional interface choices, not performance measurements. Separate cleanup
reserves remain finite: window query/activation/close cleanup 1.5s; capture abort 1s plus owned-service
stop up to 15s if compositor cleanup is unconfirmed; failed-startup cleanup up to
15s. Complete session stop is bounded by 15s. A failed capture can therefore take
up to 19s including work and cleanup. Strict success acceptance deadlines do not
expand because cleanup takes longer. The [transport contract](TRANSPORT.md)
documents separate bounded connect/frame/response allowances.

M1 provisional targets include input cancellation dispatch within 100ms,
fixture-observed release within 500ms under recorded conditions, focus checks no
more frequently than 100ms between starts, query work at most 500ms, and holds at
most 2s. They require renewed production validation; they are not real-time
promises; the failure-path tests ([TESTING.md](TESTING.md)) check the real release
timing. Required cleanup continues after caller departure. Cancellation and stop
bypass ordinary queued work and signal its owning execution context.
See [scheduler semantics](SCHEDULING.md) and [M1 evidence and limitations](M1_DECISION.md).

## Verification

```sh
python -m unittest discover -s tests -v
```

CLI tests cover every command, readable/JSON help and errors, exact application
argv boundaries, wrapped KWin identities, finite values, stale expectations,
partial/unknown outcomes and secret-free parser diagnostics. No real private
desktop is needed for these contract checks; the end-to-end smoke and failure-path
tests drive the installed package through the real workflow ([TESTING.md](TESTING.md)).
