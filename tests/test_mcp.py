"""The stdio MCP server: protocol handling, schemas, error mapping, images and cancellation.

In-process tests drive agent_desktop.mcp.Server with a stand-in call process
(mcp_call_fixture.py). The process tests run the real `agent-desktop mcp` and its
real call processes against the scheduler fixture worker over real sockets, and
check that cancel, disconnect and server death reach the worker as a correlated
request.cancel.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
import zlib

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
sys.path.insert(0, str(SRC))
from agent_desktop import cli, mcp, mcp_call  # noqa: E402
from agent_desktop.mcp_tools import TOOLS, SchemaError, call_spec, definitions, input_schema, validate  # noqa: E402
from agent_desktop.contracts import OPERATIONS, SUPPORTED_OPERATIONS  # noqa: E402

GEN = "a" * 32
WINDOW = GEN + ":2a63a414-1509-460a-bff9-b7c1103ba8d5"
LEGACY = "2025-11-25"
MODERN_META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
               "io.modelcontextprotocol/clientCapabilities": {}}
DEFAULTS = {"session": "default", "dependency_root": "/deps", "artifacts": "/artifacts"}


def png(width, height, noise=False):
    rows = b"".join(b"\0" + (os.urandom(width * 3) if noise else bytes(width * 3)) for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (mcp.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Output:
    """A thread-safe stdout stand-in that collects JSON-RPC messages."""
    def __init__(self):
        self.messages = []
        self.condition = threading.Condition()

    def write(self, data):
        self.assert_line(data)
        with self.condition:
            self.messages.append(json.loads(data))
            self.condition.notify_all()

    def assert_line(self, data):
        if not data.endswith(b"\n") or b"\n" in data[:-1]:
            raise AssertionError("each message must be exactly one line")

    def flush(self):
        pass

    def wait(self, predicate, timeout=10):
        with self.condition:
            if not self.condition.wait_for(lambda: any(predicate(m) for m in self.messages), timeout):
                raise AssertionError(f"no matching message in {self.messages}")
            return next(m for m in self.messages if predicate(m))


class BlockingOutput(Output):
    """A client that stops reading stdout: writes block while `open` is clear."""
    def __init__(self):
        super().__init__()
        self.open = threading.Event()
        self.open.set()

    def write(self, data):
        self.open.wait()
        super().write(data)


def run_briefly(target, timeout=2):
    """Run TARGET on a daemon thread; True if it returned within TIMEOUT (it may still be blocked)."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adm-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.json"
        self.events = self.root / "events"
        self.mode("reply")
        self.output = Output()
        command = [sys.executable, str(TESTS / "mcp_call_fixture.py"), str(self.plan), str(self.events)]
        self.server = mcp.Server(self.output, cwd=str(self.root), defaults=DEFAULTS, command=command)
        self.addCleanup(self.server.shutdown)
        # The session's artifact root, which the server otherwise reads from its lifecycle record.
        self.artifacts = self.root / "artifacts"
        roots = patch.object(mcp, "capture_root", lambda session, generation: self.artifacts, create=True)
        roots.start()
        self.addCleanup(roots.stop)

    def mode(self, mode, **plan):
        self.plan.write_text(json.dumps({"mode": mode, **plan}))

    def records(self, kind=None):
        if not self.events.exists():
            return []
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        return [row for row in rows if kind is None or row["event"] == kind]

    def wait_records(self, kind, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.records(kind):
                return self.records(kind)
            time.sleep(.01)
        self.fail(f"no {kind} record")

    def line(self, value):
        self.server.handle_line(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.flush(self.server)

    def flush(self, server):
        if hasattr(server, "outbox"):  # Written by the outbox thread.
            self.assertTrue(server.outbox.flush(5), "immediate responses are written")

    def request(self, rpc_id, method, params=None, wait=True):
        message = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            message["params"] = params
        self.line(message)
        if wait:
            return self.output.wait(lambda m: m.get("id") == rpc_id)

    def initialize(self, version=LEGACY):
        reply = self.request(0, "initialize", {"protocolVersion": version, "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}})
        self.line({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, rpc_id, name, arguments=None, *, meta=None, wait=True):
        params = {"name": name, "arguments": {} if arguments is None else arguments}
        if meta is not None:
            params["_meta"] = meta
        return self.request(rpc_id, "tools/call", params, wait=wait)

    # Protocol -------------------------------------------------------------

    def test_malformed_input_gets_jsonrpc_errors(self):
        self.initialize()
        self.line(b"{not json\n")
        self.assertEqual(self.output.messages[-1], {"jsonrpc": "2.0", "id": None,
                                                    "error": {"code": -32700, "message": "Parse error"}})
        for value, code, rpc_id in (([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], -32600, None),
                                    ("text", -32600, None),
                                    ({"id": 7, "method": "ping"}, -32600, 7),
                                    ({"jsonrpc": "2.0", "id": None, "method": "ping"}, -32600, None),
                                    ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, -32600, None),
                                    ({"jsonrpc": "2.0", "id": 8, "method": 3}, -32600, 8),
                                    ({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": [1]}, -32602, 9),
                                    ({"jsonrpc": "2.0", "id": 10, "method": "resources/list"}, -32601, 10)):
            with self.subTest(value=value):
                count = len(self.output.messages)
                self.line(value)
                self.assertEqual(len(self.output.messages), count + 1)
                reply = self.output.messages[-1]
                self.assertEqual((reply["id"], reply["error"]["code"]), (rpc_id, code))
        count = len(self.output.messages)
        for notification in ({"jsonrpc": "2.0", "method": "notifications/unknown"},
                             {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": None}},
                             {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": "bad"},
                             {"jsonrpc": "2.0", "id": 99, "result": {}}, b"   \n"):
            self.line(notification)
        self.assertEqual(len(self.output.messages), count, "notifications and responses are never answered")

    def test_oversized_message_is_rejected_and_the_stream_continues(self):
        for size in (mcp.MAX_LINE, mcp.MAX_LINE + 1, 3 * mcp.MAX_LINE):
            with self.subTest(size=size):
                stream = io.BytesIO(b"x" * size + b"\n" + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n' + b"tail")
                lines = mcp.Lines(stream.read)
                self.assertEqual(lines.read(), b"")
                self.assertEqual(json.loads(lines.read())["id"], 1)
                self.assertEqual(lines.read(), b"tail")
                self.assertIsNone(lines.read())
        lines = mcp.Lines(io.BytesIO(b"x" * (mcp.MAX_LINE - 1) + b"\n").read)
        self.assertEqual(len(lines.read()), mcp.MAX_LINE)

    def test_legacy_initialize_negotiates_a_version(self):
        reply = self.initialize("2025-06-18")
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "agent-desktop")
        self.assertNotIn("resultType", reply["result"])
        self.assertEqual(self.request(1, "initialize", {"protocolVersion": LEGACY})["error"]["code"], -32600)
        self.assertEqual(self.request(2, "ping")["result"], {})
        output = Output()
        other = mcp.Server(output, cwd="/", defaults=DEFAULTS)
        self.addCleanup(other.shutdown)
        other.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "1999-01-01", "capabilities": {}}}).encode())
        self.assertTrue(other.outbox.flush(5))
        self.assertEqual(output.messages[-1]["result"]["protocolVersion"], LEGACY)
        other.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}).encode())
        self.assertTrue(other.outbox.flush(5))
        self.assertEqual(output.messages[-1]["error"]["code"], -32602)

    def test_requests_before_initialize_need_modern_metadata(self):
        self.assertEqual(self.request(1, "tools/list")["error"]["code"], -32602)
        self.assertEqual(self.request(2, "ping")["result"], {})
        tools = self.request(3, "tools/list", {"_meta": MODERN_META})["result"]
        self.assertEqual(tools["resultType"], "complete")
        self.assertEqual(tools["cacheScope"], "public")
        self.assertGreater(tools["ttlMs"], 0)
        self.assertEqual(tools["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "agent-desktop")
        self.assertEqual(len(tools["tools"]), len(TOOLS))

    def test_modern_discover_and_version_errors(self):
        result = self.request(1, "server/discover", {"_meta": MODERN_META})["result"]
        self.assertEqual(result["supportedVersions"][0], "2026-07-28")
        self.assertIn(LEGACY, result["supportedVersions"])
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertTrue(result["instructions"])
        error = self.request(2, "tools/list", {"_meta": MODERN_META | {
            "io.modelcontextprotocol/protocolVersion": "2099-01-01"}})["error"]
        self.assertEqual(error["code"], -32022)
        self.assertEqual(error["data"]["requested"], "2099-01-01")
        self.assertIn("2026-07-28", error["data"]["supported"])
        missing = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        self.assertEqual(self.request(3, "tools/list", {"_meta": missing})["error"]["code"], -32602)
        bad = MODERN_META | {"io.modelcontextprotocol/protocolVersion": 5}
        self.assertEqual(self.request(4, "tools/list", {"_meta": bad})["error"]["code"], -32602)
        self.assertEqual(self.request(5, "server/discover")["error"]["code"], -32602)
        self.assertEqual(self.request(6, "tools/list", {"_meta": MODERN_META, "cursor": "x"})["error"]["code"], -32602)

    def test_tool_list_mirrors_the_cli_commands(self):
        self.initialize()
        tools = self.request(1, "tools/list")["result"]["tools"]
        operations = [TOOLS[tool["name"]][0] for tool in tools]
        self.assertEqual(sorted(operations), sorted(SUPPORTED_OPERATIONS))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertRegex(tool["name"], r"^[a-z_]{1,64}$")
                self.assertTrue(tool["title"] and tool["description"] and tool["annotations"])
                operation = TOOLS[tool["name"]][0]
                self.assertEqual(schema["properties"]["timeout"]["maximum"], OPERATIONS[operation][1])
                self.assertEqual("session" in schema["properties"], operation != "doctor")
        old = definitions("2024-11-05")
        self.assertFalse(any("title" in tool or "annotations" in tool for tool in old))

    def test_unknown_tool_and_bad_call_params_are_protocol_errors(self):
        self.initialize()
        self.assertEqual(self.request(1, "tools/call", {"name": "nope"})["error"], {"code": -32602, "message": "Unknown tool"})
        self.assertEqual(self.request(2, "tools/call", {"arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.request(3, "tools/call", {"name": "windows", "arguments": [1]})["error"]["code"], -32602)
        self.assertFalse(self.records())

    # Schemas and argument mapping -------------------------------------------

    def test_schema_validation_is_a_tool_error_naming_only_the_field(self):
        self.initialize()
        secret = "SECRET-VALUE-123"
        cases = (("click", {"x": 1}, "y", "required"), ("click", {"x": -1, "y": 0}, "x", "range"),
                 ("click", {"x": "1", "y": 0}, "x", "type"), ("click", {"x": 1, "y": 0, "button": secret}, "button", "enum"),
                 ("type", {"window": WINDOW, "text": "a", secret: 1}, secret, "unknown_property"),
                 ("key", {"window": WINDOW, "chord": "a", "hold": 0}, "hold", "range"),
                 ("drag", {"window": WINDOW, "from": [1], "to": [2, 3]}, "from", "items"),
                 ("click", {"x": 1, "y": 1, "modifiers": ["ctrl", "ctrl"]}, "modifiers", "duplicate_items"),
                 ("launch", {"argv": ["a"], "env": {"K": 1}}, "env", "type"),
                 ("windows", {"timeout": True}, "timeout", "type"),
                 ("windows", {"timeout": 0.6}, "timeout", "range"))
        for index, (name, arguments, field, reason) in enumerate(cases, 1):
            with self.subTest(name=name, field=field):
                result = self.call(index, name, arguments)["result"]
                self.assertIs(result["isError"], True)
                payload = result["structuredContent"]
                self.assertEqual(json.loads(result["content"][0]["text"]), payload)
                self.assertEqual(payload["operation"], TOOLS[name][0])
                self.assertEqual(payload["error"]["code"], "invalid_arguments")
                self.assertEqual(payload["error"]["outcome"], "not_started")
                self.assertEqual(payload["error"]["context"], {"field": field, "reason": reason})
                self.assertIsNone(payload["error"]["partial_result"])
                if field != secret:
                    self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(self.records(), "no call process starts for invalid arguments")

    def test_validate_normalizes_integral_numbers(self):
        self.assertEqual(validate(input_schema("click"), {"x": 3.0, "y": 4}, ""), {"x": 3, "y": 4})
        with self.assertRaises(SchemaError):
            validate(input_schema("click"), {"x": 3.5, "y": 4}, "")
        with self.assertRaises(SchemaError):
            validate(input_schema("click"), {"x": True, "y": 4}, "")

    def test_call_spec_maps_cli_flags_and_server_defaults(self):
        _, spec, local = call_spec("wait", {"for": "title", "window": WINDOW, "match": "x", "timeout": 5}, DEFAULTS)
        self.assertEqual(spec, {"operation": "wait", "session": "default", "generation": None, "timeout": 5,
                                "arguments": {"condition": "title", "window": WINDOW, "match": "x"}})
        _, spec, _ = call_spec("session_start", {"session": "work"}, DEFAULTS)
        self.assertEqual(spec["arguments"], {"dependency_root": "/deps", "artifacts": "/artifacts"})
        self.assertEqual(spec["session"], "work")
        _, spec, _ = call_spec("doctor", {}, DEFAULTS | {"session": "other"})
        self.assertEqual((spec["session"], spec["arguments"]), ("default", {"dependency_root": "/deps"}))
        _, spec, local = call_spec("screenshot", {"include_image": False, "output": "x.png"}, DEFAULTS)
        self.assertEqual((spec["arguments"], local), ({"output": "x.png"}, {"include_image": False}))
        _, spec, _ = call_spec("launch", {"argv": ["/bin/true"], "env": {"A": "b"}, "wait_window": True}, DEFAULTS)
        self.assertEqual(spec["arguments"], {"argv": ["/bin/true"], "env": {"A": "b"}, "wait_window": True})

    def test_every_tool_spec_is_accepted_by_the_cli_contract(self):
        from agent_desktop.contracts import make_request
        samples = {"launch": {"argv": ["/bin/true"]}, "focus": {"window": WINDOW},
                   "wait": {"for": "focus", "window": WINDOW}, "key": {"window": WINDOW, "chord": "a"},
                   "type": {"window": WINDOW, "text": ""}, "click": {"x": 1, "y": 2},
                   "move": {"x": 1, "y": 2}, "scroll": {"x": 1, "y": 2, "dy": 1},
                   "drag": {"window": WINDOW, "from": [1, 2], "to": [3, 4], "modifiers": ["ctrl"]},
                   "close": {"window": WINDOW}, "kill": {"app": GEN + ":app-1"}}
        for name in TOOLS:
            with self.subTest(name=name):
                _, spec, _ = call_spec(name, samples.get(name, {}), DEFAULTS)
                make_request(spec["operation"], arguments=spec["arguments"], caller_cwd="/",
                             session=spec["session"], expected_generation=spec["generation"],
                             timeout_seconds=spec["timeout"])

    # Calls ------------------------------------------------------------------

    def test_results_keep_the_envelope_as_text_and_structured_content(self):
        self.initialize()
        result = self.call(1, "windows", {"session": "work"})["result"]
        self.assertIs(result["isError"], False)
        payload = result["structuredContent"]
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        self.assertEqual(payload["result"]["spec"]["session"], "work")
        self.assertEqual(payload["result"]["spec"]["caller_cwd"], str(self.root))
        self.assertEqual(payload["result"]["spec"]["parent_pid"], os.getpid())

    def test_cli_errors_map_to_tool_errors_with_outcome_and_partial_result(self):
        self.initialize()
        error = {"code": "timeout", "message": "Window wait expired.", "context": {"phase": "window_wait"},
                 "outcome": "partial", "partial_result": {"application": {"ref": GEN + ":app-1"}}}
        payload = {"schema_version": 1, "request_id": "f" * 32, "operation": "launch", "ok": False,
                   "session": {"name": "default", "generation": GEN}, "result": None, "error": error}
        self.mode("reply", payload=payload)
        result = self.call(1, "launch", {"argv": ["/bin/true"]})["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"], payload)
        self.mode("crash")
        with patch.object(mcp, "log") as log:
            result = self.call(2, "windows")["result"]
        log.assert_called_once_with("call: agent-desktop internal diagnostic: RuntimeError")
        self.assertIs(result["isError"], True)
        self.assertEqual(result["structuredContent"]["error"]["code"], "internal_error")
        self.assertEqual(result["structuredContent"]["error"]["outcome"], "unknown")
        self.assertEqual(result["structuredContent"]["error"]["context"], {"reason": "no_result", "exit_status": 70})
        self.mode("silent")
        self.assertEqual(self.call(3, "windows")["result"]["structuredContent"]["error"]["code"], "internal_error")

    def test_older_versions_get_no_structured_content_and_modern_gets_result_type(self):
        self.initialize("2025-03-26")
        result = self.call(1, "windows")["result"]
        self.assertNotIn("structuredContent", result)
        self.assertNotIn("resultType", result)
        modern = self.call(2, "windows", meta=MODERN_META)["result"]
        self.assertEqual(modern["resultType"], "complete")
        self.assertIn("structuredContent", modern)

    def test_cancelled_call_is_interrupted_and_not_answered(self):
        self.initialize()
        self.mode("hold")
        self.call("k", "key", {"window": WINDOW, "chord": "w", "hold": 2}, wait=False)
        self.wait_records("start")
        self.line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "k", "reason": "test"}})
        self.wait_records("sigint", timeout=3)
        self.assertEqual(self.request(1, "ping")["result"], {})
        time.sleep(.3)
        self.assertFalse([m for m in self.output.messages if m.get("id") == "k"])
        # A cancel for an unknown or finished request is ignored.
        self.line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "k"}})

    def test_duplicate_in_flight_id_is_rejected(self):
        self.initialize()
        self.mode("hold", seconds=1)
        self.call(1, "windows", wait=False)
        self.wait_records("start")
        self.assertEqual(self.call(1, "windows")["error"]["code"], -32600)

    def test_disconnect_interrupts_calls_but_lets_session_stop_finish(self):
        self.initialize()
        self.mode("hold", seconds=1)
        self.call(1, "key", {"window": WINDOW, "chord": "w"}, wait=False)
        self.call(2, "session_stop", wait=False)
        until = time.monotonic() + 10
        while len(self.records("start")) < 2 and time.monotonic() < until:
            time.sleep(.01)
        started = time.monotonic()
        self.server.shutdown()
        events = {row["operation"]: row["event"] for row in self.records() if row["event"] != "start"}
        self.assertEqual(events, {"key": "sigint", "session.stop": "done"})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([m["id"] for m in self.output.messages if m.get("id") in (1, 2)], [2])
        self.assertEqual(self.call(3, "windows", wait=False), None)
        time.sleep(.2)
        self.assertFalse([m for m in self.output.messages if m.get("id") == 3], "no calls start after disconnect")

    def test_concurrent_calls_run_in_parallel_up_to_the_limit(self):
        self.initialize()
        self.mode("hold", seconds=.5)
        with patch.object(mcp, "MAX_CONCURRENT", 2):
            self.server.slots = threading.BoundedSemaphore(2)
            for index in range(3):
                self.call(index + 1, "windows", wait=False)
            for index in range(3):
                self.output.wait(lambda m, i=index + 1: m.get("id") == i)
        starts = sorted(row["at"] for row in self.records("start"))
        self.assertLess(starts[1] - starts[0], .4)
        self.assertGreater(starts[2] - starts[0], .4)

    def test_calls_beyond_the_in_flight_bound_are_refused_at_once(self):
        self.initialize()
        self.mode("hold", seconds=5)
        with patch.object(mcp, "MAX_CALLS", 3, create=True), patch.object(mcp, "MAX_CONCURRENT", 1):
            self.server.slots = threading.BoundedSemaphore(1)  # One runs, two wait for the slot.
            for index in range(3):
                self.call(index + 1, "windows", wait=False)
            self.call(4, "windows", wait=False)
            refused = self.output.wait(lambda m: m.get("id") == 4, timeout=2)
            self.assertEqual(refused["error"]["code"], -32603)
            self.assertEqual(refused["error"]["data"], {"reason": "too_many_calls", "limit": 3})
            # Notifications are still processed: cancelling a waiting call frees a place.
            self.line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
            until = time.monotonic() + 5
            while len(self.server.calls) > 2 and time.monotonic() < until:
                time.sleep(.01)
            self.call(5, "windows", wait=False)
            self.assertFalse([m for m in self.output.messages if m.get("id") == 5], "accepted, not refused")
            for rpc_id in (1, 2, 5):
                self.line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rpc_id}})

    def test_a_client_that_stops_reading_stdout_cannot_block_cancel_or_eof(self):
        output = BlockingOutput()
        server = mcp.Server(output, cwd=str(self.root), defaults=DEFAULTS, command=self.server.command)
        self.addCleanup(server.shutdown)
        self.addCleanup(output.open.set)

        def handle(message):
            server.handle_line(json.dumps(message).encode())
        handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": LEGACY}})
        self.mode("hold", seconds=10)
        handle({"jsonrpc": "2.0", "id": "h", "method": "tools/call",
                "params": {"name": "key", "arguments": {"window": WINDOW, "chord": "w"}}})
        self.wait_records("start")
        output.open.clear()  # The client stops reading.
        # One reader thread, as in serve(): a response it cannot write must not stop it.
        stream = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "h"}})))
        self.assertTrue(run_briefly(lambda: mcp.read_loop(server, mcp.Lines(stream.read))),
                        "the reader reached EOF while stdout was blocked")
        self.assertTrue(server.gone.is_set())
        self.wait_records("sigint")  # The held key's call was cancelled.

    def test_a_stuck_or_overflowing_stdout_counts_as_a_disconnect(self):
        output = BlockingOutput()
        server = mcp.Server(output, cwd=str(self.root), defaults=DEFAULTS, command=self.server.command)
        self.addCleanup(server.shutdown)
        self.addCleanup(output.open.set)
        output.open.clear()
        ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        self.assertTrue(run_briefly(lambda: server.handle_line(ping)))
        until = time.monotonic() + 5
        while server.outbox.writing_since is None and time.monotonic() < until:
            time.sleep(.01)
        self.assertFalse(server.outbox.check())
        self.assertTrue(server.outbox.check(time.monotonic() + mcp.STALL_SECONDS))
        self.assertTrue(server.gone.is_set())
        output = BlockingOutput()
        server = mcp.Server(output, cwd=str(self.root), defaults=DEFAULTS, command=self.server.command)
        self.addCleanup(server.shutdown)
        self.addCleanup(output.open.set)
        output.open.clear()
        with patch.object(mcp, "MAX_OUTBOUND", 3 * len(ping)):
            for _ in range(4):
                server.handle_line(ping)
        self.assertTrue(server.gone.is_set())
        self.assertTrue(server.outbox.broken)

    def test_shutdown_while_a_call_registers_joins_only_started_calls(self):
        self.initialize()
        self.mode("hold", seconds=5)
        errors, stoppers = [], []
        original = threading.Thread.start

        def start(thread):
            if thread.name == "mcp-call":
                def stop():
                    try:
                        self.server.shutdown()
                    except Exception as error:
                        errors.append(error)
                stopper = threading.Thread(target=stop, daemon=True)
                original(stopper)
                stopper.join(.3)  # A signal landing between registration and start.
                stoppers.append(stopper)
            original(thread)
        with patch.object(threading.Thread, "start", start):
            self.call(1, "windows", wait=False)
        stoppers[0].join(10)
        self.assertEqual(errors, [])

    def test_a_call_thread_that_cannot_start_is_rolled_back(self):
        self.initialize()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            reply = self.call(1, "windows")
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertEqual(self.server.calls, {})

    def test_unexpected_failures_are_internal_errors_and_the_reader_survives(self):
        self.initialize()
        with patch.object(self.server, "request", side_effect=ValueError("boom")), patch.object(mcp, "log"):
            reply = self.request(1, "tools/list")
        self.assertEqual(reply["error"], {"code": -32603, "message": "Internal error"})
        self.assertEqual(self.request(2, "ping")["result"], {})

    def test_a_call_that_fails_unexpectedly_kills_its_process(self):
        self.initialize()
        self.mode("hold", seconds=10)
        processes = []

        def broken(process, *args, **kwargs):
            processes.append(process)
            raise ValueError("boom")
        with patch.object(subprocess.Popen, "communicate", broken), patch.object(mcp, "log"):
            reply = self.call(1, "windows")
        self.assertEqual(reply["result"]["structuredContent"]["error"]["code"], "internal_error")
        self.assertIsNotNone(processes[0].poll(), "the call process was killed and reaped")
        for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr):
            stream.close()

    def test_a_duplicate_id_is_refused_before_any_other_response(self):
        self.initialize()
        self.mode("hold", seconds=5)
        self.call(1, "windows", wait=False)
        self.wait_records("start")
        for message in ({"name": "windows", "arguments": {"bogus": 1}},  # schema error: a tool result otherwise
                        {"name": "no_such_tool"}):
            count = len(self.output.messages)
            self.request(1, "tools/call", message, wait=False)
            self.assertEqual(self.output.messages[count:], [{"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32600, "message": "Invalid Request: request id already in progress"}}])
        self.assertEqual(self.request(1, "ping", wait=False), None)
        self.assertEqual(self.output.messages[-1]["error"]["code"], -32600)
        self.line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})

    def test_annotations_match_what_each_tool_changes(self):
        tools = {tool["name"]: tool["annotations"] for tool in mcp.definitions(LEGACY)}
        for name in ("windows", "wait", "logs", "session_status", "doctor"):
            self.assertIs(tools[name]["readOnlyHint"], True, name)
        for name in ("screenshot", "key", "type", "click", "scroll", "drag", "close", "kill", "session_stop"):
            self.assertEqual((tools[name]["readOnlyHint"], tools[name]["destructiveHint"]), (False, True), name)
        for name in ("focus", "move", "launch", "session_start"):
            self.assertEqual((tools[name]["readOnlyHint"], tools[name]["destructiveHint"]), (False, False), name)
        for name, (_, _, _, properties, _, annotations) in TOOLS.items():
            if annotations.get("readOnlyHint"):
                self.assertNotIn("output", properties, f"{name} writes a file")

    # Images -----------------------------------------------------------------

    def screenshot_payload(self, data, dimensions):
        """A capture at the worker's layout: ARTIFACTS/generations/GEN/captures/ID/image.png, owner-private."""
        folder = self.artifacts / "generations" / GEN / "captures" / "capture-1"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.artifacts, *folder.relative_to(self.artifacts).parents):
            (self.artifacts / directory).chmod(0o700)
        path = folder / "image.png"
        path.unlink(missing_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        result = {"capture_id": "capture-1", "path": str(path), "png_sha256": hashlib.sha256(data).hexdigest(),
                  "png_bytes": len(data), "dimensions": dimensions}
        return {"schema_version": 1, "request_id": "f" * 32, "operation": "screenshot", "ok": True,
                "session": {"name": "default", "generation": GEN}, "result": result, "error": None}

    def test_screenshot_returns_a_png_image_block_and_keeps_the_path(self):
        self.initialize()
        data = png(64, 36)
        self.mode("reply", payload=self.screenshot_payload(data, [64, 36]))
        result = self.call(1, "screenshot")["result"]
        self.assertIs(result["isError"], False)
        text, image = result["content"]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(image["data"]), data)
        payload = result["structuredContent"]
        self.assertEqual(json.loads(text["text"]), payload)
        self.assertEqual(payload["result"]["path"], str(self.artifacts / "generations" / GEN / "captures" / "capture-1"
                                                        / "image.png"))
        self.assertEqual(payload["result"]["image"], {"included": True, "mime_type": "image/png",
                                                      "bytes": len(data), "dimensions": [64, 36]})
        result = self.call(2, "screenshot", {"include_image": False})["result"]
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["structuredContent"]["result"]["image"], {"included": False, "reason": "not_requested"})

    def test_image_size_limits_leave_the_image_out(self):
        data = png(300, 200, noise=True)
        payload = self.screenshot_payload(data, [300, 200])
        with patch.object(mcp, "MAX_IMAGE_BYTES", len(data) - 1):
            checked, image = mcp.attach_image(json.loads(json.dumps(payload)))
        self.assertIsNone(image)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["result"]["image"], {"included": False, "reason": "too_large", "bytes": len(data),
                                                      "dimensions": None, "limit_bytes": len(data) - 1,
                                                      "limit_side": mcp.MAX_IMAGE_SIDE})
        with patch.object(mcp, "MAX_IMAGE_SIDE", 299):
            checked, image = mcp.attach_image(json.loads(json.dumps(payload)))
        self.assertIsNone(image)
        self.assertEqual(checked["result"]["image"]["reason"], "too_large")
        self.assertEqual(checked["result"]["image"]["dimensions"], [300, 200])
        self.assertEqual(checked["result"]["path"], payload["result"]["path"])
        checked, image = mcp.attach_image(json.loads(json.dumps(payload)))
        self.assertTrue(checked["result"]["image"]["included"])

    def attach(self, payload):
        """attach_image, which must not block: (checked payload, image)."""
        outcome = []
        self.assertTrue(run_briefly(lambda: outcome.append(mcp.attach_image(payload))), "attach_image blocked")
        return outcome[0]

    def assert_refused(self, payload, reason):
        checked, image = self.attach(payload)
        self.assertIsNone(image)
        self.assertEqual((checked["ok"], checked["error"]["code"], checked["error"]["context"]),
                         (False, "artifact_failed", {"reason": reason}))
        self.assertEqual(checked["error"]["partial_result"]["capture_id"], "capture-1")

    def test_a_fifo_at_the_capture_path_is_refused_without_blocking(self):
        payload = self.screenshot_payload(png(8, 8), [8, 8])
        path = Path(payload["result"]["path"])
        path.unlink()
        os.mkfifo(path, 0o600)
        try:
            self.assert_refused(payload, "not_regular_file")
        finally:
            try:  # Release a reader blocked on the FIFO.
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def test_captures_are_read_only_inside_the_generation_without_symlinks(self):
        data = png(8, 8)
        payload = self.screenshot_payload(data, [8, 8])
        outside = self.root / "outside.png"
        outside.write_bytes(data)
        outside.chmod(0o600)
        for path in (str(outside), str(self.artifacts / "generations" / ("b" * 32) / "image.png"),
                     str(self.artifacts / "generations" / GEN / ".." / GEN / "captures" / "capture-1" / "image.png"),
                     "relative/image.png"):
            with self.subTest(path=path):
                self.assert_refused(dict(payload, result=dict(payload["result"], path=path)), "outside_artifacts")
        # A symlinked directory on the way, not just at the last component.
        folder = Path(payload["result"]["path"]).parent
        moved = folder.with_name("real")
        folder.rename(moved)
        folder.symlink_to(moved)
        self.assert_refused(payload, "unreadable")
        folder.unlink()
        moved.rename(folder)
        folder.chmod(0o755)  # Not owner-private, as the artifact store requires.
        self.assert_refused(payload, "unreadable")
        folder.chmod(0o700)
        self.assertTrue(self.attach(payload)[0]["ok"])

    def test_a_capture_without_digest_or_dimensions_is_never_sent(self):
        data = png(8, 8)
        for field, value in (("png_sha256", None), ("png_sha256", "ABC"), ("png_sha256", 7), ("dimensions", None),
                             ("dimensions", [8]), ("dimensions", ["8", "8"]), ("dimensions", [8, 0])):
            with self.subTest(field=field, value=value):
                payload = self.screenshot_payload(data, [8, 8])
                if value is None:
                    del payload["result"][field]
                else:
                    payload["result"][field] = value
                self.assert_refused(payload, "result_incomplete")

    def test_unreadable_capture_is_artifact_failed_with_the_capture_kept(self):
        data = png(8, 8)
        for change, reason in ((lambda p: p["result"].update(png_sha256="0" * 64), "digest_mismatch"),
                               (lambda p: p["result"].update(dimensions=[9, 8]), "dimensions_mismatch"),
                               (lambda p: Path(p["result"]["path"]).write_bytes(b"GIF89a" + bytes(40)), "not_png"),
                               (lambda p: Path(p["result"]["path"]).unlink(), "unreadable")):
            with self.subTest(reason=reason):
                payload = self.screenshot_payload(data, [8, 8])
                change(payload)
                checked, image = mcp.attach_image(payload)
                self.assertIsNone(image)
                self.assertFalse(checked["ok"])
                self.assertEqual(checked["error"]["code"], "artifact_failed")
                self.assertEqual(checked["error"]["outcome"], "partial")
                self.assertEqual(checked["error"]["context"], {"reason": reason})
                self.assertEqual(checked["error"]["partial_result"]["capture_id"], "capture-1")


class EntryPointTests(unittest.TestCase):
    def run_cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, "-m", "agent_desktop", *args], env=os.environ | {"PYTHONPATH": str(SRC)},
                              capture_output=True, text=True, timeout=10, **kwargs)

    def test_cli_lists_mcp_and_refuses_it_in_json_mode(self):
        self.assertIn("mcp", self.run_cli("--help").stdout)
        result = self.run_cli("--json", "mcp")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_arguments")
        help_text = self.run_cli("mcp", "--help")
        self.assertEqual(help_text.returncode, 0)
        self.assertIn("--dependency-root", help_text.stdout)
        bad = self.run_cli("mcp", "--session", "bad name", stdin=subprocess.DEVNULL)
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))

    def test_server_exits_at_stdin_eof(self):
        result = self.run_cli("mcp", input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(result.stderr, "")

    def test_call_process_reads_arguments_from_stdin_and_runs_the_cli_dispatcher(self):
        with tempfile.TemporaryDirectory(prefix="adm-") as runtime:
            spec = {"operation": "type", "arguments": {"window": WINDOW, "text": "SECRET-TEXT"}, "session": "default",
                    "generation": None, "timeout": None, "caller_cwd": "/"}
            result = subprocess.run([sys.executable, "-P", "-m", "agent_desktop.mcp_call", "c" * 32],
                                    env=os.environ | {"PYTHONPATH": str(SRC), "XDG_RUNTIME_DIR": runtime},
                                    input=json.dumps(spec), capture_output=True, text=True, timeout=10)
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 4)
        self.assertEqual((payload["request_id"], payload["operation"]), ("c" * 32, "type"))
        self.assertEqual(payload["error"]["code"], "session_not_found")
        self.assertNotIn("SECRET-TEXT", result.stdout + result.stderr)

    def test_call_process_validates_like_the_cli(self):
        for spec, code in (({"operation": "key", "arguments": {"window": WINDOW, "chord": "a", "hold": 5}}, "invalid_arguments"),
                           ({"operation": "wait", "arguments": {"condition": "title", "window": WINDOW, "match": "(",
                                                                "regex": True}}, "invalid_arguments"),
                           ("not an object", "protocol_error")):
            with self.subTest(spec=spec):
                full = spec if isinstance(spec, str) else spec | {"caller_cwd": "/"}
                result = subprocess.run([sys.executable, "-P", "-m", "agent_desktop.mcp_call", "c" * 32],
                                        env=os.environ | {"PYTHONPATH": str(SRC)}, input=json.dumps(full),
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], code)

    def test_call_process_honors_only_the_first_sigint(self):
        # Killing a multithreaded server can deliver the parent-death SIGINT more than
        # once. Only the first may interrupt: a second KeyboardInterrupt would abort the
        # correlated priority cancel that the first one started.
        script = textwrap.dedent("""
            import os, signal, time
            from agent_desktop import mcp_call
            signal.signal(signal.SIGINT, mcp_call.interrupt_once)
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(5)
            except KeyboardInterrupt:
                print("first interrupted")
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(.05)
            print("second ignored")
        """)
        result = subprocess.run([sys.executable, "-c", script], env=os.environ | {"PYTHONPATH": str(SRC)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "first interrupted\nsecond ignored\n", ""))

    def test_call_process_whose_parent_is_gone_does_nothing(self):
        spec = {"operation": "windows", "arguments": {}, "caller_cwd": "/", "parent_pid": 1}
        result = subprocess.run([sys.executable, "-P", "-m", "agent_desktop.mcp_call", "c" * 32],
                                env=os.environ | {"PYTHONPATH": str(SRC)}, input=json.dumps(spec),
                                capture_output=True, text=True, timeout=10)
        payload = json.loads(result.stdout)
        self.assertEqual((result.returncode, payload["error"]["code"], payload["error"]["outcome"]),
                         (130, "cancelled", "not_started"))


def reap(process):
    """Kill a test's child if it is still running and close its pipes (a cleanup)."""
    if process.poll() is None:
        process.kill()
    process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class ProcessTests(unittest.TestCase):
    """The real server and call processes against a fixture worker on real sockets."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adm-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / "events"
        self.env = os.environ | {"XDG_RUNTIME_DIR": self.temp.name, "PYTHONPATH": str(SRC), "PYTHONWARNINGS": "ignore"}
        self.err = (self.root / "worker.err").open("w")
        self.worker = subprocess.Popen([sys.executable, str(TESTS / "mcp_worker_fixture.py"), "default", GEN,
                                        str(self.events)], env=self.env, stdout=subprocess.DEVNULL, stderr=self.err)
        self.addCleanup(self.stop_worker)  # Runs even if setUp or tearDown fails.
        self.wait(lambda: (self.root / "agent-desktop/current/default.json").exists())
        self.server = subprocess.Popen([sys.executable, "-m", "agent_desktop", "mcp"], env=self.env, cwd="/",
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(reap, self.server)
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(self.receive()["result"]["protocolVersion"], LEGACY)

    def tearDown(self):
        if self.server.poll() is None:
            self.server.kill()
        if not self.server.stdin.closed:
            self.server.stdin.close()
        self.server.wait(timeout=10)
        self.server.stdout.close()
        self.server.stderr.close()
        self.stop_worker()
        self.assertEqual((self.root / "worker.err").read_text(), "")

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
        self.err.close()

    def send(self, message):
        self.server.stdin.write((json.dumps(message) + "\n").encode())
        self.server.stdin.flush()

    def receive(self):
        return json.loads(self.server.stdout.readline())

    def wait(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(.005)
        self.fail("timed out; worker events: " + (self.events.read_text() if self.events.exists() else ""))

    def records(self, kind, ident=None):
        if not self.events.exists():
            return []
        rows = []
        for line in self.events.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row["event"] == kind and (ident is None or row["id"] == ident):
                rows.append(row)
        return rows

    def slow_type(self, rpc_id):
        self.send({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                   "params": {"name": "type", "arguments": {"window": WINDOW, "text": "slow"}}})
        return self.wait(lambda: self.records("start"))[0]

    def assert_correlated_cancel(self, start, before):
        cancel = self.wait(lambda: self.records("request.cancel", start["id"]))[0]
        cancelled = self.wait(lambda: self.records("cancel", start["id"]))[0]
        self.assertLess(cancel["at"] - before, 1.0)
        self.assertLess(cancelled["at"] - before, 1.0)
        self.wait(lambda: self.records("cleanup", start["id"]))
        self.assertFalse(self.records("done", start["id"]), "the task must not run to completion")

    def test_end_to_end_call_returns_the_worker_result(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "type", "arguments": {"window": WINDOW, "text": "quick"}}})
        result = self.receive()["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(result["structuredContent"]["session"], {"name": "default", "generation": GEN})
        self.assertEqual(result["structuredContent"]["result"], {"fixture": True, "desktop_ready": False})

    def test_notifications_cancelled_sends_the_correlated_cancel(self):
        start = self.slow_type("hold")
        before = time.monotonic()
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "hold"}})
        self.assert_correlated_cancel(start, before)
        self.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(self.receive(), {"jsonrpc": "2.0", "id": 2, "result": {}}, "no response for the cancelled call")

    def test_client_disconnect_sends_the_correlated_cancel_and_exits(self):
        start = self.slow_type(1)
        before = time.monotonic()
        self.server.stdin.close()
        self.assert_correlated_cancel(start, before)
        self.assertEqual(self.server.wait(timeout=5), 0)
        self.assertEqual(self.server.stdout.read(), b"")

    def test_sigterm_sends_the_correlated_cancel(self):
        start = self.slow_type(1)
        before = time.monotonic()
        self.server.send_signal(signal.SIGTERM)
        self.assert_correlated_cancel(start, before)
        self.assertEqual(self.server.wait(timeout=5), 0)

    def test_server_death_sends_the_correlated_cancel(self):
        start = self.slow_type(1)
        before = time.monotonic()
        self.server.kill()
        self.assert_correlated_cancel(start, before)


    def test_repeated_sigint_still_sends_the_correlated_cancel(self):
        # Repeats (see test_call_process_honors_only_the_first_sigint) neither abort the
        # cancel nor kill the process once it is done.
        request_id = "c" * 32
        call = subprocess.Popen([sys.executable, "-P", "-m", "agent_desktop.mcp_call", request_id], env=self.env,
                                cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(reap, call)
        call.stdin.write(json.dumps({"operation": "type", "arguments": {"window": WINDOW, "text": "slow"},
                                     "session": "default", "generation": None, "timeout": None,
                                     "caller_cwd": "/", "parent_pid": os.getpid()}).encode())
        call.stdin.close()
        start = self.wait(lambda: self.records("start"))[0]
        self.assertEqual(start["id"], request_id)
        before = time.monotonic()
        until = time.monotonic() + .05
        while time.monotonic() < until:  # Repeats land at different points of the cancel.
            call.send_signal(signal.SIGINT)
            time.sleep(.0002)
        stdout = call.stdout.read()
        call.wait(timeout=10)
        call.stderr.close()
        call.stdout.close()
        self.assertEqual(call.returncode, 130)
        self.assertEqual(json.loads(stdout)["error"]["code"], "cancelled")
        self.assert_correlated_cancel(start, before)


if __name__ == "__main__":
    unittest.main()
