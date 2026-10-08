"""`agent-desktop mcp`: a stdio MCP server whose tools are the CLI commands.

Standard library only. It speaks both MCP eras on one stdin/stdout pair:

- modern (2026-07-28): every request carries its protocol version and client
  capabilities in `_meta`; `server/discover` lists the supported versions;
- legacy (2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05): the `initialize`
  handshake negotiates one version for the process.

Each tools/call runs in its own `python -P -m agent_desktop.mcp_call` process,
which runs the CLI's own dispatcher (cli.run), so results, errors, timeouts and
Ctrl-C cancellation are exactly the CLI's. `notifications/cancelled`, a client
disconnect (stdin EOF), SIGTERM and SIGINT send that process SIGINT: the
transport client then sends the worker its correlated `request.cancel` and held
input is released. Sessions are never stopped implicitly. See docs/MCP.md.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .contracts import ContractError, NAME, OPERATIONS, response, with_refs
from .mcp_tools import TOOLS, SchemaError, call_spec, definitions

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = {"name": "agent-desktop", "title": "agent-desktop private KWin desktop", "version": __version__}
CAPABILITIES = {"tools": {"listChanged": False}}
INSTRUCTIONS = (
    "Private headless KDE desktop for testing GUI applications. Typical flow: session_start, "
    "launch (argv, wait_window: true), focus the window ref, key/type/click, screenshot (returns a PNG "
    "image), close, session_stop. Copy `ref` strings from results into window/app arguments. Results "
    "are the agent-desktop JSON envelope: check ok, error.code, error.outcome and error.partial_result. "
    "Sessions keep running after this server exits; call session_stop when done.")
LIST_TTL_MS = 3600 * 1000  # The tool list only changes with the installed version.

MAX_LINE = 1 << 20  # Bytes per JSON-RPC message, as the worker's frames.
MAX_CONCURRENT = 8  # Call processes at once; later calls wait for a slot.
MAX_CALLS = 32  # Calls in flight (running or waiting for a slot); more are refused at once.
MAX_OUTBOUND = 64 * 1024 * 1024  # Bytes of responses queued for a client that isn't reading stdout.
STALL_SECONDS = 30.0  # One stdout write blocked this long: the client is treated as gone.
FLUSH_SECONDS = 2.0  # At exit, the longest the server waits to write queued responses.
MAX_IMAGE_BYTES = 3 * 1024 * 1024  # PNG bytes before base64 (4 MiB encoded).
MAX_IMAGE_SIDE = 2048  # Pixels; the desktop output is 1280x720.
OUTER_SLACK = 45.0  # Seconds past a call's work budget before its process is interrupted.
CANCEL_GRACE = 5.0  # Seconds from SIGINT to SIGKILL (session start may clean up for 15s).
START_CANCEL_GRACE = 20.0
SHUTDOWN_SECONDS = 30.0  # Longest the server waits for calls after a disconnect.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022


class RPCError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Stop(Exception):
    """SIGTERM or SIGHUP: shut down as for a client disconnect."""


def log(message):
    """Controlled diagnostics only: never arguments, results or the environment."""
    try:
        sys.stderr.write(f"agent-desktop mcp: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def id_key(value):
    return json.dumps(value)


def valid_id(value):
    return isinstance(value, str) or (type(value) is int)


class ImageError(Exception):
    pass


def capture_root(session, generation):
    """The artifact root of SESSION's GENERATION, from its lifecycle record (as session status reads it)."""
    from .lifecycle import read_metadata
    from .runtime import Runtime
    return Path(read_metadata(Runtime(), session, generation)["configuration"]["artifacts"])


def open_capture(root, generation, path):
    """A read-only fd for a capture file inside ROOT/generations/GENERATION.

    Opened one component at a time below the root (artifacts.root_directory, then
    each hop with O_NOFOLLOW and the owner-private check the artifact store uses),
    so no symlink anywhere is followed. O_NONBLOCK and the regular-file check keep
    a FIFO or device at the path from blocking the read.
    """
    from .artifacts import check_fd, root_directory
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ImageError("outside_artifacts")
    try:
        parts = Path(path).relative_to(root / "generations" / generation).parts
    except ValueError:
        raise ImageError("outside_artifacts") from None
    if not parts:
        raise ImageError("outside_artifacts")
    fd = final = None
    try:
        with root_directory(root) as root_fd:
            fd = os.dup(root_fd)
        for part in ("generations", generation, *parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            check_fd(fd, True)
        final = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(final).st_mode):
            raise ImageError("not_regular_file")
        check_fd(final)
        opened, final = final, None
        return opened
    except (OSError, ContractError):
        raise ImageError("unreadable") from None
    finally:
        for descriptor in (fd, final):
            if descriptor is not None:
                os.close(descriptor)


def read_png(fd, sha256, dimensions):
    """The capture's PNG bytes and size from FD, checked against its result; None when over the limits."""
    size = os.fstat(fd).st_size
    if size > MAX_IMAGE_BYTES:
        return None, size, None
    chunks, total = [], 0
    while total <= MAX_IMAGE_BYTES:
        chunk = os.read(fd, MAX_IMAGE_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_IMAGE_BYTES:
        return None, len(data), None
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ImageError("not_png")
    width, height = struct.unpack(">II", data[16:24])
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ImageError("digest_mismatch")
    if dimensions != [width, height]:
        raise ImageError("dimensions_mismatch")
    if max(width, height) > MAX_IMAGE_SIDE:
        return None, len(data), [width, height]
    return data, len(data), [width, height]


def load_capture(payload):
    """(data, size, dimensions) for a screenshot payload, or ImageError naming the reason."""
    result, session = payload["result"], payload.get("session")
    dimensions = result.get("dimensions")
    # The worker always reports these; without them nothing can be checked, so nothing is sent.
    if (not isinstance(result.get("png_sha256"), str) or not SHA256.fullmatch(result["png_sha256"])
            or not isinstance(dimensions, list) or len(dimensions) != 2
            or not all(type(value) is int and value > 0 for value in dimensions)
            or not isinstance(session, dict) or not isinstance(session.get("name"), str)
            or not isinstance(session.get("generation"), str)):
        raise ImageError("result_incomplete")
    try:
        root = capture_root(session["name"], session["generation"])
    except (OSError, ContractError, KeyError, TypeError, ValueError):
        raise ImageError("unreadable") from None
    fd = open_capture(root, session["generation"], result.get("path"))
    try:
        return read_png(fd, result["png_sha256"], dimensions)
    except OSError:
        raise ImageError("unreadable") from None
    finally:
        os.close(fd)


def attach_image(payload):
    """Read a successful screenshot's PNG for an image block; record the decision in result.image.

    Over the limits the image is left out (reason too_large), never downscaled.
    A capture that cannot be read back fails like a failed --output copy:
    artifact_failed, outcome partial, with the capture in partial_result.
    """
    result = payload["result"]
    try:
        data, size, dimensions = load_capture(payload)
    except ImageError as error:
        failure = ContractError("artifact_failed", "Screenshot was captured but could not be read for the image.",
                                context={"reason": str(error)}, outcome="partial", partial_result=result)
        return dict(payload, ok=False, result=None, error=failure.payload()), None
    if data is None:
        result["image"] = {"included": False, "reason": "too_large", "bytes": size, "dimensions": dimensions,
                           "limit_bytes": MAX_IMAGE_BYTES, "limit_side": MAX_IMAGE_SIDE}
        return payload, None
    result["image"] = {"included": True, "mime_type": "image/png", "bytes": size, "dimensions": dimensions}
    return payload, {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}


def envelope_error(request_id, operation, session, code, message, *, outcome="not_started", context=None):
    error = ContractError(code, message, context=context, outcome=outcome)
    return with_refs(response(request_id, operation, session=None if operation == "doctor" else session, error=error))


class Call:
    """One tools/call: a slot, one call process, its envelope and the response."""

    def __init__(self, server, key, rpc_id, operation, spec, local, *, structured, modern):
        self.server, self.key, self.rpc_id = server, key, rpc_id
        self.operation, self.spec, self.local = operation, spec, local
        self.structured, self.modern = structured, modern
        self.image = None
        self.request_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.interrupted = False
        self.process = None
        self.thread = threading.Thread(target=self.run, name="mcp-call", daemon=True)

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            self.interrupt()

    def interrupt(self):
        # Caller holds self.lock. One SIGINT: a second could interrupt the cancel itself.
        if self.process is not None and not self.interrupted and self.process.poll() is None:
            self.interrupted = True
            try:
                self.process.send_signal(signal.SIGINT)
            except OSError:
                pass

    def run(self):
        payload = None
        acquired = False
        try:
            while not self.cancelled.is_set():
                if self.server.slots.acquire(timeout=.05):
                    acquired = True
                    break
            if acquired and not self.cancelled.is_set():
                payload = self.execute()
        except Exception as error:
            log(f"internal diagnostic: {type(error).__name__}")
            if self.process is not None and self.process.poll() is None:
                self.process.kill()  # Never leave a call running that nothing waits for.
                self.process.wait()
            payload = envelope_error(self.request_id, self.operation, self.spec["session"], "internal_error",
                                     "MCP call failure.", outcome="unknown")
        finally:
            if acquired:
                self.server.slots.release()
        try:
            if payload is not None and not self.cancelled.is_set():
                self.server.reply_tool(self, payload)
        finally:
            self.server.finished(self)

    def execute(self):
        spec = dict(self.spec, caller_cwd=self.server.cwd, parent_pid=os.getpid())
        command = self.server.command + [self.request_id]
        with self.lock:
            if self.cancelled.is_set():
                return None
            # Started from this thread, which outlives the process: the parent-death
            # signal (SIGINT) fires only when the server itself dies.
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, cwd=self.server.cwd, start_new_session=True)
        # communicate() writes the spec and closes stdin, and keeps it across timeouts.
        # (Python 3.12's communicate() fails on a stdin already closed by hand.)
        data = json.dumps(spec, ensure_ascii=True).encode()
        timeout = self.spec["timeout"] if self.spec["timeout"] is not None else OPERATIONS[self.operation][0]
        grace = START_CANCEL_GRACE if self.operation == "session.start" else CANCEL_GRACE
        deadline = time.monotonic() + timeout + OUTER_SLACK
        expired, cancel_started = False, None
        while True:
            now = time.monotonic()
            if self.cancelled.is_set() and cancel_started is None:
                cancel_started = now
            if cancel_started is None and now >= deadline:
                expired = True
                cancel_started = now
                with self.lock:
                    self.interrupt()
            limit = deadline if cancel_started is None else cancel_started + grace
            try:
                stdout, stderr = self.process.communicate(data, timeout=max(.01, min(.1, limit - now)))
                break
            except subprocess.TimeoutExpired:
                data = None  # Retries must not pass input again.
                if cancel_started is not None and time.monotonic() >= cancel_started + grace:
                    self.process.kill()
        for line in stderr.decode("utf-8", "replace").splitlines()[:20]:
            log("call: " + line[:300])
        if expired:
            return envelope_error(self.request_id, self.operation, self.spec["session"], "timeout",
                                  "The call process did not finish in its budget plus cleanup; it was interrupted.",
                                  outcome="unknown", context={"phase": "mcp_call"})
        try:
            payload = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
            if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
                raise ValueError
        except (ValueError, IndexError, UnicodeDecodeError):
            return envelope_error(self.request_id, self.operation, self.spec["session"], "internal_error",
                                  "The call process ended without a result.", outcome="unknown",
                                  context={"reason": "no_result", "exit_status": self.process.returncode})
        if self.operation == "screenshot" and payload["ok"] and isinstance(payload.get("result"), dict):
            if self.local.get("include_image", True):
                payload, self.image = attach_image(payload)
            else:
                payload["result"]["image"] = {"included": False, "reason": "not_requested"}
        return payload


class Outbox:
    """Responses queued for one writer thread, so a client that stops reading stdout
    never blocks the stdin reader: cancellations and EOF are still read and acted on.

    A client that lets more than MAX_OUTBOUND bytes pile up, or leaves one write
    blocked for STALL_SECONDS, is treated as gone (a disconnect), as is a failed write.
    """
    def __init__(self, output, gone):
        self.output, self.gone = output, gone
        self.queue = deque()
        self.size = 0
        self.writing_since = None
        self.broken = False
        self.closed = False
        self.condition = threading.Condition()
        self.thread = threading.Thread(target=self.run, name="mcp-stdout", daemon=True)
        self.thread.start()

    def put(self, line):
        with self.condition:
            if self.broken:
                return
            if self.size + len(line) > MAX_OUTBOUND:
                self.fail("the client is not reading responses")
                return
            self.queue.append(line)
            self.size += len(line)
            self.condition.notify_all()
        self.check()

    def fail(self, reason):
        # Caller holds self.condition.
        if not self.broken:
            self.broken = True
            log(f"{reason}; treating it as a disconnect")
            self.queue.clear()
            self.size = 0
            self.condition.notify_all()
        self.gone.set()

    def check(self, now=None):
        """Treat a write blocked for STALL_SECONDS as a disconnect; True when stalled."""
        with self.condition:
            started = self.writing_since
            if started is not None and (now or time.monotonic()) - started >= STALL_SECONDS:
                self.fail("a response write has been blocked too long")
            return self.broken

    def run(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if not self.queue:
                    return
                line = self.queue[0]
                self.writing_since = time.monotonic()
            try:
                self.output.write(line)
                self.output.flush()
            except (OSError, ValueError):
                with self.condition:
                    self.writing_since = None
                    self.fail("stdout is closed")
                return
            with self.condition:
                self.writing_since = None
                if self.queue and self.queue[0] is line:
                    self.queue.popleft()
                    self.size -= len(line)
                self.condition.notify_all()

    def flush(self, timeout):
        """Wait until everything queued is written (or the client is gone); True when empty."""
        until = time.monotonic() + timeout
        with self.condition:
            while self.queue and not self.broken:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, .1))
            return not self.queue

    def close(self, timeout):
        self.flush(timeout)
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class Server:
    def __init__(self, output, *, cwd, defaults, command=None):
        self.cwd = cwd
        self.defaults = defaults
        self.command = command or [sys.executable, "-P", "-m", "agent_desktop.mcp_call"]
        self.lock = threading.Lock()
        self.calls = {}
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.legacy_version = None
        self.closing = False
        self.gone = threading.Event()  # Set at stdin EOF or when the client stops reading stdout.
        self.outbox = Outbox(output, self.gone)

    # Output ------------------------------------------------------------------

    def send(self, message):
        self.outbox.put(json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n")

    def error(self, rpc_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": rpc_id, "error": error})

    def result(self, rpc_id, body, modern):
        if modern:
            body = {"resultType": "complete"} | body | {"_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
        self.send({"jsonrpc": "2.0", "id": rpc_id, "result": body})

    # Input -------------------------------------------------------------------

    def handle_line(self, line):
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            self.error(None, PARSE_ERROR, "Parse error")
            return
        if not isinstance(message, dict):
            # Includes JSON-RPC batches, which MCP no longer allows.
            self.error(None, INVALID_REQUEST, "Invalid Request: expected one JSON object")
            return
        self.handle_message(message)

    def handle_message(self, message):
        has_id = "id" in message
        rpc_id = message.get("id")
        method = message.get("method")
        if "method" not in message and ("result" in message or "error" in message or not has_id):
            return  # A response from the client; this server sends no requests.
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or (has_id and not valid_id(rpc_id)):
            if has_id:
                self.error(rpc_id if valid_id(rpc_id) else None, INVALID_REQUEST, "Invalid Request")
            return
        params = message.get("params", {})
        if not has_id:
            if isinstance(params, dict):
                self.notification(method, params)
            return
        with self.lock:
            duplicate = id_key(rpc_id) in self.calls
        if duplicate:
            # Before any other response: a reply with this id would look like the running call's.
            self.error(rpc_id, INVALID_REQUEST, "Invalid Request: request id already in progress")
            return
        try:
            if not isinstance(params, dict):
                raise RPCError(INVALID_PARAMS, "Invalid params: expected an object")
            self.request(rpc_id, method, params)
        except RPCError as error:
            self.error(rpc_id, error.code, error.message, error.data)
        except Exception as error:
            log(f"internal diagnostic: {type(error).__name__}")
            self.error(rpc_id, INTERNAL_ERROR, "Internal error")

    def notification(self, method, params):
        if method == "notifications/cancelled":
            target = params.get("requestId")
            if valid_id(target):
                with self.lock:
                    call = self.calls.get(id_key(target))
                if call is not None:
                    call.cancel()
        # notifications/initialized and anything else need no action.

    def era(self, method, params):
        """(modern, features version) for a request, or an RPCError."""
        meta = params.get("_meta")
        if isinstance(meta, dict) and VERSION_KEY in meta:
            version = meta[VERSION_KEY]
            if not isinstance(version, str):
                raise RPCError(INVALID_PARAMS, f"Invalid params: _meta {VERSION_KEY} must be a string")
            if version in LEGACY_VERSIONS:
                pass  # A legacy version is served under its initialize handshake.
            elif version not in MODERN_VERSIONS:
                raise RPCError(UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                               {"supported": list(MODERN_VERSIONS + LEGACY_VERSIONS), "requested": version})
            elif not isinstance(meta.get(CAPABILITIES_KEY), dict):
                raise RPCError(INVALID_PARAMS, f"Invalid params: _meta {CAPABILITIES_KEY} is required")
            else:
                return True, version
        if self.legacy_version is not None or method == "ping":
            return False, self.legacy_version or LEGACY_VERSIONS[0]
        raise RPCError(INVALID_PARAMS, f"Invalid params: send initialize first, or {VERSION_KEY} in _meta "
                       f"(supported: {', '.join(MODERN_VERSIONS + LEGACY_VERSIONS)})")

    def request(self, rpc_id, method, params):
        if method == "initialize":
            self.initialize(rpc_id, params)
            return
        modern, version = self.era(method, params)
        if method == "ping":
            self.result(rpc_id, {}, modern)
        elif method == "server/discover":
            if not modern:
                raise RPCError(METHOD_NOT_FOUND, "Method not found")
            self.result(rpc_id, {"supportedVersions": list(MODERN_VERSIONS + LEGACY_VERSIONS),
                                 "capabilities": CAPABILITIES, "instructions": INSTRUCTIONS,
                                 "ttlMs": LIST_TTL_MS, "cacheScope": "public"}, modern)
        elif method == "tools/list":
            if params.get("cursor") is not None:
                raise RPCError(INVALID_PARAMS, "Invalid params: unknown cursor")
            body = {"tools": definitions(version)}
            if modern:
                body |= {"ttlMs": LIST_TTL_MS, "cacheScope": "public"}
            self.result(rpc_id, body, modern)
        elif method == "tools/call":
            self.call(rpc_id, params, modern, version)
        else:
            raise RPCError(METHOD_NOT_FOUND, "Method not found")

    def initialize(self, rpc_id, params):
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise RPCError(INVALID_PARAMS, "Invalid params: protocolVersion must be a string")
        if self.legacy_version is not None:
            raise RPCError(INVALID_REQUEST, "Invalid Request: already initialized")
        # Legacy negotiation: echo a supported version, else offer the latest legacy one.
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        self.legacy_version = version
        info = SERVER_INFO if version >= "2025-06-18" else {"name": SERVER_INFO["name"], "version": __version__}
        self.result(rpc_id, {"protocolVersion": version, "capabilities": CAPABILITIES,
                             "serverInfo": info, "instructions": INSTRUCTIONS}, False)

    def call(self, rpc_id, params, modern, version):
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOLS:
            raise RPCError(INVALID_PARAMS, "Unknown tool" if isinstance(name, str) else "Invalid params: name")
        if not isinstance(arguments, dict):
            raise RPCError(INVALID_PARAMS, "Invalid params: arguments must be an object")
        key = id_key(rpc_id)
        structured = modern or version >= "2025-06-18"  # structuredContent arrived in 2025-06-18.
        operation = TOOLS[name][0]
        try:
            operation, spec, local = call_spec(name, arguments, self.defaults)
        except SchemaError as error:
            # Input validation is a tool execution error the model can correct.
            session = arguments.get("session", self.defaults["session"])
            session = session if isinstance(session, str) and NAME.fullmatch(session) else self.defaults["session"]
            payload = envelope_error(uuid.uuid4().hex, operation, session, "invalid_arguments",
                                     "Invalid or missing argument; see the tool's input schema.",
                                     context={"field": error.field, "reason": error.reason})
            self.send({"jsonrpc": "2.0", "id": rpc_id, "result": self.tool_body(payload, None, structured, modern)})
            return
        with self.lock:
            if key in self.calls:
                raise RPCError(INVALID_REQUEST, "Invalid Request: request id already in progress")
            if self.closing:
                return
            if len(self.calls) >= MAX_CALLS:
                # Each waiting call holds its arguments and a thread: refuse rather than queue without bound.
                raise RPCError(INTERNAL_ERROR, f"Server busy: {MAX_CALLS} tool calls are already in flight; "
                               "retry after one finishes", {"reason": "too_many_calls", "limit": MAX_CALLS})
            call = Call(self, key, rpc_id, operation, spec, local, structured=structured, modern=modern)
            self.calls[key] = call
            # Started under the lock, so shutdown never sees (and joins) an unstarted call.
            try:
                call.thread.start()
            except RuntimeError:
                del self.calls[key]
                raise RPCError(INTERNAL_ERROR, "Internal error: the call could not be started") from None

    def tool_body(self, payload, image, structured, modern):
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                                       separators=(",", ":"))}]
        if image is not None:
            content.append(image)
        body = {"content": content, "isError": not payload["ok"]}
        if structured:
            body["structuredContent"] = payload
        if modern:
            body = {"resultType": "complete"} | body | {"_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
        return body

    def reply_tool(self, call, payload):
        self.send({"jsonrpc": "2.0", "id": call.rpc_id,
                   "result": self.tool_body(payload, call.image, call.structured, call.modern)})

    def finished(self, call):
        with self.lock:
            if self.calls.get(call.key) is call:
                del self.calls[call.key]

    # Disconnect ----------------------------------------------------------------

    def shutdown(self):
        """Client gone: interrupt every call except session_stop, which finishes; keep sessions."""
        with self.lock:
            self.closing = True
            calls = list(self.calls.values())
        for call in calls:
            if call.operation != "session.stop":
                call.cancel()
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        for call in calls:
            call.thread.join(max(0, deadline - time.monotonic()))
        self.outbox.close(FLUSH_SECONDS)


class Lines:
    """Newline-delimited messages from READ(size) -> bytes (b"" at EOF).

    The server passes an unbuffered os.read on stdin, not sys.stdin: a daemon
    thread blocked in a buffered read would abort the interpreter at exit.
    """
    def __init__(self, read):
        self.read_chunk = read
        self.buffer = b""
        self.eof = False

    def read(self):
        """One message (bytes), b"" for an oversized one, None at EOF."""
        oversized = False
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                line, self.buffer = self.buffer[:end + 1], self.buffer[end + 1:]
                return b"" if oversized or len(line) > MAX_LINE else line
            if len(self.buffer) > MAX_LINE:
                oversized, self.buffer = True, b""  # Drop it and skip to its newline.
            if self.eof:
                line, self.buffer = self.buffer, b""
                if oversized:
                    return b""
                return line or None
            data = self.read_chunk(65536)
            if not data:
                self.eof = True
            self.buffer += data


class Writer:
    """Unbuffered writes to a file descriptor (stdout), for the same reason as Lines."""
    def __init__(self, fd):
        self.fd = fd

    def write(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def flush(self):
        pass


def options(argv):
    from .cli import Parser, HelpRequested
    parser = Parser(prog="agent-desktop mcp", description="Serve the agent-desktop commands as MCP tools over stdio "
                    "(newline-delimited JSON-RPC). Relative paths resolve against this process's working directory.")
    parser.add_argument("--session", default="default", metavar="NAME", help="default session for every tool")
    parser.add_argument("--dependency-root", default=".local/dependencies",
                        help="default dependency root for doctor and session_start")
    parser.add_argument("--artifacts", default=".agent-desktop/artifacts", help="default artifact root for session_start")
    try:
        values = parser.parse_args(argv)
    except HelpRequested as help_result:
        sys.stdout.write(help_result.help_text)
        return None
    if not NAME.fullmatch(values.session):
        raise ContractError("invalid_arguments", "Invalid --session name.")
    for value in (values.dependency_root, values.artifacts):
        if not value or "\0" in value:
            raise ContractError("invalid_arguments", "Invalid path.")
    return values


def serve(argv):
    try:
        values = options(argv)
    except ContractError as error:
        log(f"{error.code}: {error.message}")
        return 2
    if values is None:
        return 0
    cwd = os.getcwd()
    defaults = {"session": values.session,
                "dependency_root": os.path.normpath(os.path.join(cwd, values.dependency_root)),
                "artifacts": os.path.normpath(os.path.join(cwd, values.artifacts))}
    server = Server(Writer(sys.stdout.fileno()), cwd=cwd, defaults=defaults)
    lines = Lines(lambda size: os.read(sys.stdin.fileno(), size))
    reader = threading.Thread(target=read_loop, args=(server, lines), name="mcp-stdin", daemon=True)

    def stop(_signum, _frame):
        raise Stop
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    # Messages are read and handled on the reader thread, so a signal can only
    # interrupt this wait, never a half-handled message.
    try:
        reader.start()
        while not server.gone.wait(.5):
            server.outbox.check()
    except (KeyboardInterrupt, Stop):
        pass
    finally:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(number, signal.SIG_IGN)
        server.shutdown()
    return 0


def read_loop(server, lines):
    try:
        while not server.gone.is_set():
            line = lines.read()
            if line is None:
                break
            if line == b"":
                server.error(None, INVALID_REQUEST, "Invalid Request: message too large")
                continue
            server.handle_line(line)
    except (OSError, ValueError):
        pass
    finally:
        server.gone.set()
