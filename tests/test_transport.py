"""Strict wire, private runtime, real GLib worker and independent CLI processes."""
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from agent_desktop.contracts import ContractError, make_request, response
from agent_desktop.protocol import (Decoder, MAX_FRAME, decode, encode,
                                    request_from_wire, validate_response)
from agent_desktop.runtime import Endpoint, Runtime, peer_owner
from agent_desktop.transport import exchange

GEN = "a" * 32
OTHER = "b" * 32
WINDOW = "2a63a414-1509-460a-bff9-b7c1103ba8d5"


def request(operation="session.status", **kwargs):
    return make_request(operation, arguments=kwargs.pop("arguments", {}), caller_cwd="/tmp",
                        expected_generation=GEN, **kwargs)


class CodecTests(unittest.TestCase):
    def test_fragmented_single_frame(self):
        frame = encode(request().payload())
        decoder = Decoder()
        for byte in frame[:-1]:
            self.assertIsNone(decoder.feed(bytes([byte])))
        self.assertEqual(request_from_wire(decoder.feed(frame[-1:])), request_from_wire(decode(frame[4:])))

    def test_strict_framing_and_json(self):
        for data in [b"", b"[]", b'{} {}', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":{"nested":1e999}}',
                     b'{"x":"\xff"}', b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}']:
            with self.subTest(data=data[:25]), self.assertRaises(ContractError):
                decode(data)
        for header in (struct.pack("!I", 0), struct.pack("!I", MAX_FRAME + 1), encode({}) + b"x"):
            with self.assertRaises(ContractError):
                Decoder().feed(header)

    def test_raw_wire_types_do_not_use_cli_coercion(self):
        base = request().payload()
        changes = {"schema_version": [True, 2, "1"], "operation": [[], {}, "doctor", "session.start"],
                   "request_id": [None, "bad"], "arguments": [None, [], {"oops": 1}],
                   "timeout_seconds": [True, "1", float("inf"), 0], "session": [None, []],
                   "expected_generation": [None, "bad"], "caller_cwd": ["relative", []]}
        for field, values in changes.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    request_from_wire(base | {field: value})
        for value in (base | {"extra": 1}, {k: v for k, v in base.items() if k != "arguments"}):
            with self.assertRaises(ContractError):
                request_from_wire(value)
        key = request("key", arguments={"window": GEN + ":" + WINDOW, "chord": "A"}).payload()
        for args in ({"window": GEN + ":" + WINDOW, "chord": "A", "hold": .05},
                     key["arguments"] | {"hold": "1"}):
            with self.assertRaises(ContractError):
                request_from_wire(key | {"arguments": args})

    def test_response_correlation_and_shapes(self):
        req = request()
        good = response(req.request_id, req.operation, session="default", generation=GEN, result={"ok": "fixture"})
        self.assertEqual(validate_response(good, req, GEN), good)
        for key, val in (("schema_version", True), ("request_id", OTHER), ("operation", "launch"),
                         ("session", {"name": "default", "generation": OTHER}), ("ok", 1),
                         ("result", None), ("error", {})):
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_response(good | {key: val}, req, GEN)
        err = response(req.request_id, req.operation, session="default", generation=GEN,
                       error=ContractError("timeout", "Test", outcome="partial", partial_result={"app": "kept"}))
        self.assertEqual(validate_response(err, req, GEN)["error"]["partial_result"], {"app": "kept"})
        for key, val in (("code", []), ("outcome", []), ("context", []), ("partial_result", [])):
            with self.assertRaises(ContractError):
                validate_response(err | {"error": err["error"] | {key: val}}, req, GEN)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adt-")
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_permissions_claims_and_graceful_reuse(self):
        endpoint = Endpoint("default", GEN)
        try:
            for path in (endpoint.runtime.root, endpoint.runtime.current, endpoint.runtime.generations, endpoint.path.parent):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(endpoint.path.stat().st_mode), 0o600)
            pointer = endpoint.runtime.current / "default.json"
            self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
            inode = endpoint.path.stat().st_ino
            for name in ("default", "other"):
                with self.assertRaises(ContractError):
                    Endpoint(name, GEN)
                self.assertEqual(endpoint.path.stat().st_ino, inode)
        finally:
            endpoint.close()
        self.assertTrue(endpoint.path.parent.is_dir())
        with self.assertRaises(ContractError):
            Endpoint("default", GEN)
        self.assertFalse(pointer.exists())
        replacement = Endpoint("default", OTHER)
        replacement.listener.close()
        replacement.listener = None
        try:
            with self.assertRaises(ContractError) as caught:
                exchange(make_request("session.status", arguments={}, caller_cwd="/tmp"))
            self.assertEqual(caught.exception.code, "session_unavailable")
            self.assertEqual(caught.exception.outcome, "not_started")
        finally:
            replacement.close()

    def test_replacement_pointer_and_socket_survive_old_cleanup(self):
        endpoint = Endpoint("default", GEN)
        pointer = endpoint.runtime.current / "default.json"
        pointer.write_text(json.dumps({"schema_version": 1, "session": "default", "generation": OTHER}))
        endpoint.path.unlink()
        other_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other_sock.bind(str(endpoint.path))
        try:
            endpoint.close()
            self.assertTrue(endpoint.path.exists())
            self.assertEqual(endpoint.runtime.read("default"), OTHER)
        finally:
            other_sock.close()

    def test_unsafe_roots_and_components(self):
        os.chmod(self.temp.name, 0o755)
        with self.assertRaises(ContractError):
            Runtime(create=True)
        os.chmod(self.temp.name, 0o700)
        root = Path(self.temp.name) / "agent-desktop"
        root.symlink_to(self.temp.name)
        with self.assertRaises(ContractError):
            Runtime(create=True)
        self.assertTrue(root.is_symlink())
        root.unlink()
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative"}), self.assertRaises(ContractError):
            Runtime(create=True)
        runtime = Runtime(create=True)
        with patch("agent_desktop.runtime.os.getuid", return_value=os.getuid() + 1), self.assertRaises(ContractError):
            Runtime()
        pointer = runtime.current / "default.json"
        pointer.symlink_to("/dev/null")
        with self.assertRaises(ContractError):
            runtime.read("default")
        self.assertTrue(pointer.is_symlink())

    def test_partial_publication_failure_and_conflict(self):
        with patch("agent_desktop.runtime.os.replace", side_effect=OSError), self.assertRaises(OSError):
            Endpoint("default", GEN)
        runtime = Runtime()
        self.assertTrue(runtime.socket_path(GEN).parent.is_dir())
        self.assertFalse(runtime.socket_path(GEN).exists())
        self.assertFalse((runtime.current / "default.json").exists())
        endpoint = Endpoint("default", OTHER)
        try:
            with self.assertRaises(ContractError):
                Endpoint("default", "c" * 32)
            self.assertEqual(runtime.read("default"), OTHER)
        finally:
            endpoint.close()

    def test_bad_pointer_peer_and_path_length(self):
        runtime = Runtime(create=True)
        pointer = runtime.current / "default.json"
        pointer.write_text('{"schema_version":true,"session":"default","generation":"' + GEN + '"}')
        pointer.chmod(0o600)
        with self.assertRaises(ContractError):
            runtime.read("default")
        with patch.object(socket.socket, "getsockopt", return_value=struct.pack("3i", 1, os.getuid()+1, 1)):
            with socket.socket(socket.AF_UNIX) as sock, self.assertRaises(ContractError):
                peer_owner(sock)
        runtime.generations = Path("/" + "x" * 100)
        with self.assertRaises(ContractError):
            runtime.socket_path(GEN)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adt-")
        self.root = Path(self.temp.name)
        self.env = os.environ | {"XDG_RUNTIME_DIR": self.temp.name, "PYTHONPATH": str(SRC),
                                 "FIXTURE_EFFECTS": str(self.root / "effects"),
                                 "FIXTURE_DISCONNECTED": str(self.root / "disconnected")}
        self.workers = []
        self.start_worker()

    def tearDown(self):
        for proc, out, err in self.workers:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=4)
            out.close()
            err.close()
        self.temp.cleanup()

    def start_worker(self, generation=GEN, *, production=False):
        out = (self.root / (generation + ".out")).open("w")
        err = (self.root / (generation + ".err")).open("w")
        cmd = [sys.executable, "-m", "agent_desktop.worker", "--session", "default", "--generation", generation] if production else [sys.executable, str(Path(__file__).with_name("transport_worker_fixture.py")), "default", generation]
        proc = subprocess.Popen(cmd, env=self.env, stdout=out, stderr=err)
        self.workers.append((proc, out, err))
        pointer = self.root / "agent-desktop/current/default.json"
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("Worker failed: " + (self.root / (generation + ".err")).read_text())
            if pointer.exists() and json.loads(pointer.read_text())["generation"] == generation:
                return proc
            time.sleep(.01)
        self.fail("Worker did not publish endpoint")

    def cli(self, *args):
        proc = subprocess.run([sys.executable, "-m", "agent_desktop", "--json", *args], env=self.env,
                              cwd="/", capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        return proc.returncode, json.loads(proc.stdout)

    def raw(self, payload, *, gen=GEN):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(str(self.root / "agent-desktop/g" / gen / "control.sock"))
        sock.sendall(encode(payload))
        decoder = Decoder()
        while True:
            data = sock.recv(65536)
            if not data:
                sock.close()
                return None
            value = decoder.feed(data)
            if value is not None:
                sock.close()
                return value

    def type_cli(self, mode, *args):
        return self.cli("type", "--window", GEN + ":" + WINDOW, mode, *args)

    def test_independent_processes_identity_output_and_production_boundary(self):
        values = [self.cli("session", "status")[1] for _ in range(2)]
        self.assertEqual({v["result"]["worker_pid"] for v in values}, {self.workers[0][0].pid})
        self.assertEqual(len({v["request_id"] for v in values}), 2)
        for value in values:
            self.assertEqual(value["session"], {"name": "default", "generation": GEN})
            self.assertFalse(value["result"]["desktop_ready"])
        readable = subprocess.run([sys.executable, "-m", "agent_desktop", "session", "status"], env=self.env,
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(readable.returncode, 0)
        self.assertEqual(json.loads(readable.stdout)["state"], "transport_test")
        self.workers[0][0].terminate()
        self.workers[0][0].wait(timeout=4)
        self.start_worker(OTHER, production=True)
        code, result = self.cli("session", "status")
        self.assertEqual(code, 5)
        self.assertEqual(result["session"]["generation"], OTHER)
        self.assertEqual(result["error"]["code"], "unsupported_operation")
        self.assertEqual(self.cli("session", "start")[0], 5)
        self.assertEqual(self.cli("session", "stop")[0], 5)
        self.assertIsNone(self.workers[-1][0].poll())

    def test_generation_mismatch_and_raw_validation_without_effects(self):
        for opts in (("session", "status", "--generation", OTHER),
                     ("type", "--window", OTHER + ":" + WINDOW, "safe")):
            code, payload = self.cli(*opts)
            self.assertEqual(code, 4)
            self.assertEqual(payload["error"]["code"], "generation_mismatch")
            self.assertIsNone(payload["session"]["generation"])
        base = request("type", arguments={"window": GEN + ":" + WINDOW, "text": "safe"}).payload()
        for payload in (base | {"session": "other"}, base | {"operation": []}, base | {"schema_version": 2},
                        base | {"expected_generation": OTHER}, base | {"arguments": {"text": "safe"}}):
            result = self.raw(payload)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["outcome"], "not_started")
        self.assertFalse((self.root / "effects").exists())
        self.workers[0][0].terminate()
        self.workers[0][0].wait(timeout=4)
        self.start_worker(OTHER)
        self.assertEqual(self.type_cli("safe")[1]["error"]["code"], "generation_mismatch")
        self.assertEqual(self.cli("session", "status")[1]["session"]["generation"], OTHER)

    def test_effects_partial_lost_and_unrepresentable_results_never_retry(self):
        for mode in ("large", "unencodable", "lost"):
            code, payload = self.type_cli(mode)
            self.assertEqual(code, 11)
            self.assertEqual(payload["error"]["code"], "completion_unknown")
            self.assertEqual(payload["error"]["outcome"], "unknown")
            if mode == "large":
                self.assertEqual(payload["error"]["partial_result"]["application"]["application_id"], "retained")
        self.assertEqual(len((self.root / "effects").read_text().splitlines()), 3)
        code, payload = self.type_cli("partial")
        self.assertEqual(code, 8)
        self.assertEqual(payload["error"]["outcome"], "partial")
        self.assertEqual(payload["error"]["partial_result"]["app"]["application_id"], "retained")
        code, payload = self.type_cli("late", "--timeout", ".01")
        self.assertEqual(code, 8)
        self.assertEqual(payload["error"]["outcome"], "unknown")
        self.assertEqual(payload["error"]["partial_result"]["application"]["application_id"], "late-app")
        self.assertEqual(payload["error"]["partial_result"]["log_paths"], {"stdout": "/fixture/late-app.out"})
        code, payload = self.type_cli("late_large", "--timeout", ".01")
        self.assertEqual(code, 11)
        self.assertEqual(payload["error"]["outcome"], "unknown")
        self.assertEqual(payload["error"]["partial_result"]["application"]["application_id"], "late-app")
        self.assertNotIn("data", payload["error"]["partial_result"])
        self.assertEqual(len((self.root / "effects").read_text().splitlines()), 6)

    def test_queued_trailing_bytes_at_receive_boundary_never_dispatch(self):
        worker = self.workers[0][0]
        for size in (65535, 65536, 65537):
            for extra in (b"!", b""):
                with self.subTest(size=size, extra=extra):
                    req = request("type", arguments={"window": GEN + ":" + WINDOW, "text": "x"})
                    payload = req.payload()
                    payload["arguments"]["text"] += "x" * (size - len(encode(payload)))
                    frame = encode(payload)
                    self.assertEqual(len(frame), size)
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.settimeout(3)
                        # Stop and observe before queuing all bytes; this avoids
                        # relying on scheduler timing for the malformed case.
                        worker.send_signal(signal.SIGSTOP)
                        _, stopped = os.waitpid(worker.pid, os.WUNTRACED)
                        self.assertTrue(os.WIFSTOPPED(stopped))
                        try:
                            sock.connect(str(self.root / "agent-desktop/g" / GEN / "control.sock"))
                            sock.sendall(frame + extra)
                        finally:
                            worker.send_signal(signal.SIGCONT)
                        decoder = Decoder()
                        while True:
                            data = sock.recv(65536)
                            self.assertTrue(data, "Expected one bounded reply")
                            result = decoder.feed(data)
                            if result is not None:
                                break
                        self.assertEqual(result["ok"], not bool(extra))
                        if extra:
                            self.assertEqual(result["error"]["code"], "protocol_error")
                            self.assertEqual(result["error"]["outcome"], "not_started")
                            effects = (self.root / "effects").read_text() if (self.root / "effects").exists() else ""
                            self.assertNotIn(req.request_id, effects)
                        else:
                            self.assertIn(req.request_id, (self.root / "effects").read_text())
        self.assertEqual(len((self.root / "effects").read_text().splitlines()), 3)

    def test_slow_peer_deadline_and_responsive_other_client(self):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(str(self.root / "agent-desktop/g" / GEN / "control.sock"))
            sock.sendall(b"\0")
            start = time.monotonic()
            self.assertEqual(self.cli("session", "status")[0], 0)
            self.assertLess(time.monotonic() - start, .8)
            time.sleep(.55)
            sock.sendall(b"\0")
            self.assertEqual(sock.recv(1), b"")
            self.assertLess(time.monotonic() - start, 1.6)

    def test_duplicate_active_id_and_disconnect_preserve_original(self):
        req = request("type", arguments={"window": GEN + ":" + WINDOW, "text": "pending"})
        with socket.socket(socket.AF_UNIX) as first:
            first.connect(str(self.root / "agent-desktop/g" / GEN / "control.sock"))
            first.sendall(encode(req.payload()))
            time.sleep(.05)
            duplicate = self.raw(req.payload())
            self.assertEqual(duplicate["error"]["code"], "protocol_error")
            self.assertEqual(self.raw(req.payload())["error"]["code"], "protocol_error")
        deadline = time.monotonic() + 1
        while not (self.root / "disconnected").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual((self.root / "disconnected").read_text(), req.request_id)
        self.assertEqual(self.raw(req.payload())["error"]["code"], "protocol_error")

    def test_cli_sigint_closes_matching_socket(self):
        cmd = [sys.executable, "-m", "agent_desktop", "--json", "type", "--window", GEN + ":" + WINDOW, "pending"]
        proc = subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.2)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=3)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["error"]["outcome"], "unknown")
        deadline = time.monotonic() + 1
        while not (self.root / "disconnected").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual((self.root / "disconnected").read_text(), json.loads(out)["request_id"])


class ClientTests(unittest.TestCase):
    def test_pinned_resolution_and_mismatched_reply_are_unknown(self):
        req = request()
        fake = unittest.mock.MagicMock()
        fake.send.side_effect = lambda data: len(data)
        bad = response(req.request_id, req.operation, session="default", generation=OTHER, result={})
        fake.recv.return_value = encode(bad)
        with patch("agent_desktop.transport.Runtime") as runtime, patch("agent_desktop.transport.socket.socket", return_value=fake), patch("agent_desktop.transport.peer_owner"):
            runtime.return_value.discover.return_value = (GEN, Path("/pinned"))
            with self.assertRaises(ContractError) as caught:
                exchange(req)
            self.assertEqual(caught.exception.code, "completion_unknown")
            runtime.return_value.discover.assert_called_once()
            fake.connect.assert_called_once_with("/pinned")
            fake.send.assert_called_once()

    def test_interrupt_before_send_is_not_started(self):
        with patch("agent_desktop.transport.Runtime", side_effect=KeyboardInterrupt), self.assertRaises(ContractError) as caught:
            exchange(request())
        self.assertEqual(caught.exception.outcome, "not_started")


if __name__ == "__main__":
    unittest.main()
