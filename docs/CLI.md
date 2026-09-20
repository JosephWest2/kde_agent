# agent-desktop command contract

The CLI installs, validates requests and contacts a persistent worker over a
private generation-bound socket. Help and version work. Desktop operations are
unwired: absent sessions report `session_not_found`, while the transport-only
worker returns `unsupported_operation` with its verified generation. `doctor` and
`session start` remain locally unsupported. Scheduling and durable records follow
in #16–17; real desktop operations follow in later milestones. See the
[transport contract](TRANSPORT.md) for the internal worker and verified limits.
Examples of those future results below are illustrative, not support claims.

## Install and inspect

Use a project virtual environment; no global installation is needed:

```sh
python -m venv .local/cli-venv
.local/cli-venv/bin/python -m pip install .
.local/cli-venv/bin/agent-desktop --help
.local/cli-venv/bin/agent-desktop --json session start --help
.local/cli-venv/bin/python -m agent_desktop --version
```

Packaging uses setuptools, requires Python >=3.11, and has no pip runtime
dependencies. The Python floor does not claim native adapter support on all those
interpreters. Platform native bindings remain governed by [setup](SETUP.md).
Installing or inspecting the CLI does not import native desktop adapters or run
feasibility scripts. `doctor` directs users to `tools/dependencies.py report` for
the separate M1 prerequisite evaluation; it does not certify production readiness.

Output is readable by default. `--json` emits exactly one JSON object and newline
on stdout for success, help, version, parser failures, unsupported commands and
operation failures. Diagnostics use stderr; application/desktop output will use
separate logs. Default errors use stderr and leave stdout empty. Parser failures
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
| `session start` | `--mode headless`; `--artifacts PATH` (default `.agent-desktop/artifacts`) | 30 / 30 |
| `session status` | none | 3 / 3 |
| `session stop` | none | 15 / 15 |
| `launch` | `--cwd PATH`, repeated `--env KEY=VALUE`, `--wait-window`; required `-- PROGRAM [ARG ...]` | 10 / 60 |
| `windows` | optional `--app APP_REF` | 0.5 / 0.5 |
| `focus` | exactly one of `--window WINDOW_REF` or `--app APP_REF` | 2 / 2 |
| `wait` | `--for window\|focus\|exit`; window/exit require `--app`, focus requires `--window` | 10 / 60 |
| `key` | required `--window WINDOW_REF`, positional `CHORD`; `--hold SECONDS` (default 0.05, at most 2) | 3 / 3 |
| `type` | required `--window WINDOW_REF`, positional literal `TEXT` (empty allowed) | 3 / 3 |
| `click` | required `--window WINDOW_REF --x INT --y INT`; `--button left\|middle\|right` (default left) | 3 / 3 |
| `input reset` | none | 3 / 3 |
| `screenshot` | optional `--output PATH` (default future unique artifact path) | 3 / 3 |
| `logs` | optional `--app APP_REF`; `--source all\|worker\|compositor\|application` (default all) | 3 / 3 |
| `close` | exactly one of `--window WINDOW_REF` or `--app APP_REF` | 5 / 60 |
| `kill` | required `--app APP_REF` | 5 / 15 |

Timeout and hold values must be finite and strictly positive. No unbounded mode
exists. Input targets always use a window; app selection for focus/close must
resolve unambiguously. Titles are descriptive, never identities. Click coordinates
are nonnegative client-content pixels, excluding decorations; the future worker
must validate current bounds before dispatch. Key/chord/US text mapping validation
belongs to #28; accepting argument shape now does not claim input support.

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
`logs` will return artifact locations and metadata, never an unbounded tail.
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

The client captures its absolute caller cwd in each Request and currently retains
raw path arguments. #17 implements normalization: omitted application cwd means
caller cwd; relative cwd/artifact/output/dependency paths resolve against caller
cwd, independently of worker cwd. Executables containing `/` resolve against the
final application cwd; bare names use the final application PATH. Environment
construction applies defaults, allowed explicit overrides, then protected private
session settings; attempted protected overrides are rejected. No inherited
environment or credentials are dumped to diagnostics or manifests.

Durable artifacts will be generation-specific and survive cleanup; omitted
screenshot paths will be collision-free. Desktop/settings separation is not
filesystem or network isolation. Applications remain trusted local programs.

## Requests, results and errors

The shared Request contains `schema_version: 1`, a fresh `request_id`, operation
(`session.start`, `input.reset`, etc.), session name, `expected_generation`,
`arguments`, finite `timeout_seconds`, and `caller_cwd`. Pure validators consume
ordinary data; the worker additionally enforces strict wire types, framing/version
and immutable live worker identity independently of the CLI.

An actual current scaffold failure from `agent-desktop --json session start`
has this shape (request IDs vary):

```json
{"schema_version":1,"request_id":"11111111111111111111111111111111","operation":"session.start","ok":false,"session":{"name":"default","generation":null},"result":null,"error":{"code":"unsupported_operation","message":"Operation is not implemented yet.","context":{"implementation_issue":18,"expected_generation":null},"outcome":"not_started","partial_result":null}}
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
| 7 | `application_exited` |
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

Illustrative future result objects (not currently returned by desktop commands):

```json
{"application":{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","application_id":"app-1"},"process":{"pid":1234,"start_time_ticks":5678},"logs":{"stdout":"/artifacts/app-1.stdout.log","stderr":"/artifacts/app-1.stderr.log"},"windows":[]}
```

```json
{"windows":[{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","window_id":"{2a63a414-1509-460a-bff9-b7c1103ba8d5}","pid":1234,"title":null,"application_class":null,"client_bounds":{"x":0,"y":0,"width":640,"height":480},"focused":true}]}
```

```json
{"dispatched":true,"application_acknowledged":false}
```

Input dispatch does not promise application acknowledgment, a rendered frame or
UI readiness. Screenshot success must report a fresh complete PNG's path,
dimensions, capture timestamp, backend and generation, for example:

```json
{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path":"/artifacts/capture-1.png","width":1280,"height":720,"captured_at":"2026-09-19T12:00:00Z","backend":"kwin-screenshot2"}
```

A launch/window-wait failure retains its handle, for example this illustrative
error object within the common failure envelope:

```json
{"code":"timeout","message":"Window wait expired.","context":{"phase":"window_wait"},"outcome":"partial","partial_result":{"application":{"generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","application_id":"app-1"},"process":{"pid":1234,"start_time_ticks":5678},"logs":{"stderr":"/artifacts/app-1.stderr.log"},"windows":[]}}
```

Ambiguity returns `target_ambiguous` with `context.candidates` containing full
window handles. Uncertain release returns `input_uncertain`, `outcome: unknown`,
and context describing the uncertainty; further input is blocked until a safe
reset or stop. Close/kill report `application`, `exited`, `exit_status` (null if
unknown), and `remaining_processes`; timeout retains available partial outcomes.
No generic success claim is made when an application remains alive.

## Deadlines and cleanup

The command table defines finite *work* budgets. The transport worker admits a
monotonic deadline and checks success acceptance; its production dispatcher fails
unwired operations immediately. #16 supplies cancellable task execution and queue
deadlines, including queue delay; a client socket timeout is not enforcement. Composite operations share one budget: target
checks, focus verification and key holds must all fit the remaining input time.
Individually valid settings do not guarantee completion within that time.

The work limits retain M1 choices; 10/60s wait and 5s close/kill defaults are
provisional interface choices, not performance measurements. Separate cleanup
reserves remain finite: query cleanup 1.5s; capture abort 1s plus owned-service
stop up to 15s if compositor cleanup is unconfirmed; failed-startup cleanup up to
15s. Complete session stop is bounded by 15s. A failed capture can therefore take
up to 19s including work and cleanup. Strict success acceptance deadlines do not
expand because cleanup takes longer. The [transport contract](TRANSPORT.md)
documents separate bounded connect/frame/response allowances.

M1 provisional targets include input cancellation dispatch within 100ms,
fixture-observed release within 500ms under recorded conditions, focus checks no
more frequently than 100ms between starts, query work at most 500ms, and holds at
most 2s. They require renewed production validation; they are not real-time
promises. Required cleanup continues after caller departure. Cancellation, reset
and stop bypass ordinary queued work and signal its owning execution context.
See [M1 evidence and limitations](M1_DECISION.md).

## Verification

```sh
python -m unittest discover -s tests -v
```

CLI tests cover every command, readable/JSON help and errors, exact application
argv boundaries, wrapped KWin identities, finite values, stale expectations,
partial/unknown outcomes and secret-free parser diagnostics. #14 also verifies a
built wheel in an isolated disposable venv outside the checkout. No real private
desktop is needed for these contract checks; later milestones own the real
workflow and twenty-run support qualification.
