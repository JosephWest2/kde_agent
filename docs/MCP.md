# MCP server

`agent-desktop mcp` serves every CLI command as an [MCP](https://modelcontextprotocol.io)
tool over stdio, so an MCP client (Claude Code, an IDE agent) can call them
directly and get screenshots back as images. The CLI keeps working on its own and
nothing about it changes; both drive the same sessions.

## Register it

Run `tools/setup.sh` first. Then, with absolute paths to this checkout:

```sh
claude mcp add agent-desktop -- /path/to/kde-agent/.local/dependencies/venv/bin/agent-desktop mcp \
  --dependency-root /path/to/kde-agent/.local/dependencies
```

`claude mcp add` options such as `-s project` (write `.mcp.json` in the current
project) or `-s user` go before the name; everything after `--` is the server
command (checked against `claude mcp add --help` in Claude Code 2.1.293). The
`.mcp.json` form:

```json
{
  "mcpServers": {
    "agent-desktop": {
      "type": "stdio",
      "command": "/path/to/kde-agent/.local/dependencies/venv/bin/agent-desktop",
      "args": ["mcp", "--dependency-root", "/path/to/kde-agent/.local/dependencies"]
    }
  }
}
```

Server options, all optional:

| Option | Default | Used for |
| --- | --- | --- |
| `--dependency-root PATH` | `.local/dependencies` | `doctor` and `session_start` when the call doesn't give one |
| `--artifacts PATH` | `.agent-desktop/artifacts` | `session_start` when the call doesn't give one |
| `--session NAME` | `default` | every tool when the call doesn't give `session` |

Relative paths, in these options and in tool arguments (`artifacts`,
`dependency_root`, launch `cwd`, screenshot `output`), resolve against the server's
working directory at startup, exactly as the CLI resolves them against the shell's.
The client chooses that directory, so pass absolute paths when it matters. The dependency root and
artifact root are part of a session's configuration: a `session_start` that names
a running session with different roots fails with `session_conflict`, as with the
CLI. Use the same absolute roots as your CLI calls, or a different `--session`.

## Tools

One tool per command. Arguments are the CLI flags in snake case with JSON types;
every tool except `doctor` also takes `session`, `generation` and `timeout`
(seconds, the command's default and maximum as in [CLI.md](CLI.md#commands-flags-and-defaults)).

| Tool | Command | Arguments |
| --- | --- | --- |
| `doctor` | `doctor` | `dependency_root`, `timeout` |
| `session_start` | `session start` | `artifacts`, `dependency_root`, `mode` (`headless`) |
| `session_status` | `session status` | |
| `session_stop` | `session stop` | |
| `launch` | `launch -- PROGRAM ARGS` | `argv` (required, array), `cwd`, `env` (object), `wait_window` (boolean) |
| `windows` | `windows` | `app` |
| `focus` | `focus` | `window` or `app` |
| `wait` | `wait` | `for` (required), `app`, `window`, `match`, `regex` (boolean) |
| `key` | `key CHORD` | `window`, `chord` (both required), `hold` |
| `type` | `type TEXT` | `window`, `text` (both required), `method` (`auto`, `keys` or `input-method`; default `auto`). Non-ASCII text is one input-method commit of at most 4000 UTF-8 bytes; the result's `method` and `confirmed` are as in [INPUT.md](INPUT.md#non-ascii-text-the-input-method) |
| `click` | `click` | `x`, `y` (required), `window`, `button`, `count`, `modifiers` (array) |
| `move` | `move` | `x`, `y` (required), `window` |
| `scroll` | `scroll` | `x`, `y` (required), `window`, `dx`, `dy`, `modifiers` |
| `drag` | `drag` | `window`, `from`, `to` (required; `[x, y]` arrays), `button`, `duration`, `modifiers` |
| `screenshot` | `screenshot` | `window`, `output`, `include_image` (boolean, default true) |
| `logs` | `logs` | `app`, `source`, `tail` |
| `close` | `close` | `window` or `app` |
| `kill` | `kill` | `app` (required) |

`app` and `window` are the `ref` strings from results (`GENERATION:ID`).
`tools/list` gives each tool's JSON Schema (draft 2020-12, `additionalProperties:
false`), a title, a description and annotations. Read-only: `doctor`,
`session_status`, `windows`, `wait`, `logs`. Destructive: `key`, `type`,
`click`, `scroll`, `drag` (the application may delete, close or overwrite
things in response), `screenshot` (each call stores a capture, and `output`
replaces an existing file), `close`, `kill`, `session_stop`. Neither: `focus`,
`move`, `launch`, `session_start`.

The schema checks JSON types, ranges and names. Everything else (refs, key names,
text, paths, environment names, `--regex` compilation) goes through the CLI's own
validation, so an MCP call and a CLI call reject the same requests with the same
errors.

## Results and errors

A tool result carries the CLI's JSON envelope (`schema_version`, `request_id`,
`operation`, `ok`, `session`, `result`, `error` with `code`, `message`, `context`,
`outcome` and `partial_result`) unchanged:

- as `structuredContent` (protocol 2025-06-18 and later), and
- as the same JSON in one text block (every version).

`isError` is `!ok`. Every failure the CLI reports, including argument errors and
schema violations (`invalid_arguments` with `context.field` and
`context.reason`, never the value), is a tool result with `isError: true`, so the
model can read the code and correct itself. JSON-RPC errors are only for
requests that are themselves malformed: unparseable JSON (`-32700`), a
non-object or batch message, a bad `jsonrpc`/`id`/`method` (`-32600`), an
unknown method (`-32601`), an unknown tool, a non-object `arguments` or a
missing protocol version (`-32602`), and an unsupported protocol version
(`-32022`). Messages are limited to 1 MiB, the worker's frame limit; a longer one
is `-32600` and the stream continues. A request whose `id` belongs to a call still
in flight gets `-32600` before anything else is checked, so no other reply can
carry that id. More than 32 calls in flight is refused at once with `-32603`,
`data: {"reason": "too_many_calls", "limit": 32}` (see below).

## Screenshots

A successful `screenshot` returns the envelope plus one image block:
`{"type": "image", "mimeType": "image/png", "data": BASE64}`. The server reads
the PNG from `result.path`; it never crosses the worker's 1 MiB frame. The path
must lie inside `ARTIFACTS/generations/GENERATION/` of the session generation in
the envelope, where ARTIFACTS is the root recorded in that generation's lifecycle
record (the one `session status` reads), not the server's `--artifacts`. It is
opened one component at a time below that root, never following a symlink, with
every directory and the file owner-private (the artifact store's own checks), and
the file must be regular (opened non-blocking, so a FIFO cannot hang the server);
at most 3 MiB plus one byte is read. Before sending, the server checks the PNG
signature, that its SHA-256 equals `result.png_sha256` and its size equals
`result.dimensions`; both fields are required. The same confined open (`artifacts.open_capture`) guards
`output`, which the call process copies exactly as the CLI's `--output` does
([CLI.md](CLI.md)), also with `include_image: false`.

`result.path` always stays in the result, and `result.image` records what
happened:

| `result.image` | When |
| --- | --- |
| `{"included": true, "mime_type": "image/png", "bytes": N, "dimensions": [W, H]}` | the image block is attached |
| `{"included": false, "reason": "not_requested"}` | `include_image: false` |
| `{"included": false, "reason": "too_large", "bytes", "dimensions", "limit_bytes", "limit_side"}` | over 3 MiB of PNG (4 MiB once base64 encoded) or over 2048 pixels on a side |

Images over the limit are left out, never downscaled: the output is a fixed
1280×720 (a full-screen PNG is typically well under 1 MiB), so only an
unusually noisy capture can exceed 3 MiB, and resampling would change the pixel
coordinates that `click --window` relies on. Read `result.path` instead. A
capture that cannot be sent fails like a failed `--output` copy:
`artifact_failed`, outcome `partial`, the capture in `partial_result`, and
`context.reason` one of `result_incomplete` (no valid `png_sha256` or
`dimensions`), `outside_artifacts`, `not_regular_file`, `unreadable` (missing, a
symlink on the way, not owner-private, or no lifecycle record), `not_png`,
`digest_mismatch` or `dimensions_mismatch`. `output` still copies the
PNG, as `--output` does.

## Cancellation, disconnect and sessions

Each `tools/call` runs in its own process, `python -P -m agent_desktop.mcp_call`,
which runs the same dispatcher as the CLI (`cli.run`). Its arguments arrive on
stdin, so typed text and launch environment values are not in any command line
(`/proc/PID/cmdline` is readable by other processes of the user).

To stop a call the server sends that process SIGINT, which is the CLI's Ctrl-C:
the transport client sends the worker the correlated priority `request.cancel`
for that request ID and closes its socket, and the worker cancels exactly that
request and releases anything held ([scheduling](SCHEDULING.md),
[input](INPUT.md)). This happens on:

| Event | What the server does |
| --- | --- |
| `notifications/cancelled` for an in-flight call | SIGINT that call, through a pidfd opened at spawn and closed once the call's thread has reaped the process, so the signal can never reach a process that reused its PID. No response is sent for it (the MCP rule); the call's `cancelled` envelope is dropped. A cancel for an unknown or finished request is ignored. A call still waiting for a slot never starts. |
| client disconnect: stdin closes, or the client stops reading stdout (64 MiB of responses queued, or one write blocked for 30s) | SIGINT every running call except `session_stop`, which finishes (stop is idempotent and its cleanup should not be cut short); wait for them, at most 30s; exit 0 |
| SIGTERM, SIGHUP or SIGINT to the server | the same as a disconnect |
| the server dies (SIGKILL, crash) | each call process gets SIGINT from Linux (`PR_SET_PDEATHSIG`), so it still sends the correlated cancel. Linux can repeat that signal while a multithreaded server's threads exit, so a call process honors only its first SIGINT (at a terminal a second Ctrl-C skips the cancel; here a repeat never means that) |
| a call process that doesn't finish in its budget plus 45s | SIGINT, then SIGKILL after 5s (20s for `session_start`, which cleans up a half-started session); the result is `timeout`, outcome `unknown`, `context.phase: mcp_call` |

If the call process is killed outright, the worker still sees its socket close and
cancels the request, which also releases held input.

Measured on the real desktop with the native fixture (`tests/integration/mcp.py`),
during `key --hold 2 w`: the fixture saw the key released 3ms after
`notifications/cancelled`, 3ms after the client closed stdin and 7ms after the
server was SIGKILLed.

**Sessions are never stopped by the server.** They are persistent systemd user
services, like sessions started from the CLI, and outlive the MCP server: a
client restart, a crash or a second client must not destroy a desktop and its
applications that someone may still be using. A disconnect only cancels the calls
in flight. Call `session_stop` (or `agent-desktop session stop`) when done.

## Concurrency and timeouts

Calls run in parallel, each in its own process, up to 8 at once; later calls wait
for a slot (a cancel while waiting means the call never starts). At most 32 calls
are in flight, running or waiting: each waiting call holds its arguments and a
thread, so a 33rd is refused at once with a JSON-RPC error (`-32603`,
`data.reason: "too_many_calls"`) rather than queued. It is a protocol error, not
a tool result, because no CLI error code means "server busy" and the call never
reached the CLI; retry once a call finishes.

Responses are written by their own thread from a queue, so a client that stops
reading stdout never stops the server reading stdin: cancellations and EOF are
still acted on and held input is still released. A queue over 64 MiB or a single
write blocked for 30s is treated as a disconnect. The server adds
no ordering of its own: the worker already queues ordinary requests in arrival
order and runs one at a time per session, with cancellation and stop on a
separate priority path ([scheduling](SCHEDULING.md)). Parallel calls therefore
behave like parallel CLI invocations. Time spent waiting for a slot is not part of
the command's budget.

`timeout` is the CLI's `--timeout`: the command's work budget, enforced by the
worker (or by the CLI for `doctor` and `session start`). The server only adds the
outer safety bound above. The client's own request timeout should allow the
budget plus the cleanup reserve ([deadlines](CLI.md#deadlines-and-cleanup)), for
example 60s for `session_start`, and 130s for `doctor`.

## Protocol

The server is dual-era, standard library only (no MCP SDK), newline-delimited
JSON-RPC 2.0 on stdin and stdout; diagnostics go to stderr and never include
arguments, results or the environment. They are best effort: a bounded queue
written by its own thread, so a client that stops reading stderr cannot stall
disconnect handling or a call (lines are dropped once 256 are waiting).

- **Modern, 2026-07-28.** Every request carries
  `_meta["io.modelcontextprotocol/protocolVersion"]` and
  `_meta["io.modelcontextprotocol/clientCapabilities"]` (missing: `-32602`; an
  unknown version: `-32022` with `data.supported` and `data.requested`). Methods:
  `server/discover`, `tools/list`, `tools/call`. Results carry
  `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`;
  `server/discover` and `tools/list` carry `ttlMs` and `cacheScope: "public"`.
- **Legacy, 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05.** `initialize`
  negotiates the version (an unsupported one gets 2025-11-25), then `tools/list`,
  `tools/call` and `ping`. A second `initialize` is refused. Before either an
  `initialize` or modern `_meta`, only `ping` is answered.
- Capabilities: `tools` with `listChanged: false`. No resources, prompts,
  logging, progress or server-to-client requests. `notifications/cancelled` is
  honored in both eras; other notifications and client responses are ignored.

## Design decisions

**One process per call, running the CLI's dispatcher.** The alternatives were to
subprocess the public CLI, or to call the dispatcher inside the server process.

- In process, a call could not be cancelled through the existing path: the
  transport client sends its correlated `request.cancel` when `KeyboardInterrupt`
  interrupts its blocking socket wait, and only the main thread gets that. Doing
  it from a thread would mean reworking `transport.exchange` and the lifecycle
  manager (which also cleans up a half-started session on interrupt) around a
  cancel token, and threads would share process state such as signal handlers.
  A separate process keeps the tested Ctrl-C path unchanged and isolates each call.
- Subprocessing the public CLI would also reuse that path, but it would put every
  argument, typed text and launch environment values included, on a command line,
  and round-trip typed JSON through argparse strings.
- So `cli.main` was split into `cli.run(request)` (doctor, lifecycle, transport and
  the screenshot copy) and `cli.execute(...)` (every failure, Ctrl-C included, to
  one envelope and exit status). The CLI and `agent_desktop.mcp_call` both call
  them; the call process builds the Request with the same `make_request`,
  `--regex` compile check and path normalization as the CLI. Error fidelity is
  exact: the envelope is the CLI's. The cost is one Python start per call (about
  0.1s) and a second small entry point to maintain. Nothing in the CLI contract
  changed.

**No new dependency.** The official `mcp` Python SDK pulls in pydantic, anyio,
httpx, starlette and more, while the package has no runtime pip dependencies and
the session service runs from the same venv. The stdio protocol needed here
(framing, five methods, cancellation, version negotiation, a schema subset for
validation) is a few hundred lines in `agent_desktop/mcp.py` and
`agent_desktop/mcp_tools.py`.

## Tests

- Unit (`tests/test_mcp.py`): malformed input, oversized messages, both eras'
  negotiation and errors, the tool list against `SUPPORTED_OPERATIONS`, schema
  validation that names only the field, argument mapping accepted by the CLI
  contract for every tool, error mapping (CLI errors, a crashing or silent call
  process), parallel calls and the slot limit, the image block and size limits,
  unreadable captures, and cancel/disconnect/SIGTERM/server death against a
  fixture worker that records the correlated `request.cancel`.
- Real desktop (`tests/integration/mcp.py`, see [TESTING.md](TESTING.md#mcp-tests)).
