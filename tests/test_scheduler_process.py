"""Actual GLib/Unix sockets and independent CLI processes, no desktop adapters."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.protocol import CancelRequest, Decoder, cancel_from_wire, encode

GEN = "a" * 32
WINDOW = GEN + ":2a63a414-1509-460a-bff9-b7c1103ba8d5"


class SchedulerProcessTests(unittest.TestCase):
    metrics = {}

    def measured(self, name, seconds):
        self.metrics.setdefault(name, []).append(seconds)
        self.assertLess(seconds, .1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adq-")
        self.root = Path(self.temp.name)
        self.events = self.root / "events"
        src = Path(__file__).resolve().parents[1] / "src"
        self.env = os.environ | {"XDG_RUNTIME_DIR": self.temp.name, "PYTHONPATH": str(src), "PYTHONWARNINGS": "ignore"}
        self.err = (self.root / "worker.err").open("w")
        self.worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("scheduler_worker_fixture.py")),
                                        "default", GEN, str(self.events)], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=self.err)
        self.sockets = []
        self.clients = []
        self.wait(lambda: (self.root / "agent-desktop/current/default.json").exists())

    def tearDown(self):
        for client in self.clients:
            if client.poll() is None:
                client.kill()
            client.communicate(timeout=3)
        for sock in self.sockets:
            sock.close()
        self.worker.terminate()
        self.worker.wait(timeout=3)
        self.err.close()
        self.assertEqual((self.root / "worker.err").read_text(), "")
        self.temp.cleanup()

    def wait(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            result = predicate()
            if result:
                return result
            if self.worker.poll() is not None:
                self.fail((self.root / "worker.err").read_text())
            time.sleep(.002)
        self.fail("Timed out waiting for fixture event")

    def records(self, kind=None, ident=None):
        if not self.events.exists():
            return []
        lines = self.events.read_text().splitlines()
        values = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (kind is None or value["event"] == kind) and (ident is None or value["id"] == ident):
                values.append(value)
        return values

    def connect(self, *, priority=False):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(3)
        sock.connect(str(self.root / "agent-desktop/g" / GEN / ("priority.sock" if priority else "control.sock")))
        self.sockets.append(sock)
        return sock

    def send(self, operation="type", text="slow", timeout=3, *, priority=False):
        args = {"window": WINDOW, "text": text} if operation == "type" else {}
        req = make_request(operation, arguments=args, caller_cwd="/", expected_generation=GEN,
                           timeout_seconds=timeout)
        sock = self.connect(priority=priority)
        sock.sendall(encode(req.payload()))
        return req, sock

    def receive(self, sock):
        decoder = Decoder()
        while True:
            data = sock.recv(65536)
            self.assertTrue(data)
            value = decoder.feed(data)
            if value is not None:
                return value

    def cancel(self, target, generation=GEN):
        import uuid
        sock = self.connect(priority=True)
        request = CancelRequest(uuid.uuid4().hex, "default", generation, target)
        sock.sendall(encode(request.payload()))
        return self.receive(sock)

    def cli(self, *args):
        proc = subprocess.Popen([sys.executable, "-m", "agent_desktop", "--json", *args],
                                env=self.env, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.clients.append(proc)
        return proc

    def test_strict_cancel_and_endpoint_routing(self):
        import uuid
        first, sock = self.send()
        self.wait(lambda: self.records("start", first.request_id))
        good = CancelRequest(uuid.uuid4().hex, "default", GEN, first.request_id).payload()
        for field, bad in (("schema_version", True), ("target_request_id", "bad"),
                           ("session", []), ("expected_generation", None)):
            with self.assertRaises(ContractError):
                cancel_from_wire(good | {field: bad})
        for priority, payload in ((False, good), (True, first.payload()),
                                  (True, good | {"extra": True})):
            control = self.connect(priority=priority)
            control.sendall(encode(payload))
            self.assertEqual(self.receive(control)["error"]["code"], "protocol_error")
        self.assertFalse(self.records("cancel", first.request_id))
        self.cancel(first.request_id)
        self.assertEqual(self.receive(sock)["error"]["code"], "cancelled")

    def test_serialization_queued_expiry_worker_timeout_and_retained_handle(self):
        first, sock = self.send(timeout=.2)
        self.wait(lambda: self.records("start", first.request_id))
        second, queued = self.send(text="quick", timeout=.03)
        result = self.receive(queued)
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertEqual(result["error"]["outcome"], "not_started")
        self.assertFalse(self.records("start", second.request_id))
        result = self.receive(sock)
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertEqual(result["error"]["partial_result"]["app"]["application_id"], "retained")
        admitted = self.records("admitted", first.request_id)[0]["at"]
        cancelled = self.records("cancel", first.request_id)[0]["at"]
        self.measured("worker_timeout_detection", cancelled - (admitted + .2))
        third, later = self.send(text="quick")
        self.assertTrue(self.receive(later)["ok"])
        self.assertGreater(self.records("start", third.request_id)[0]["at"], self.records("cleanup", first.request_id)[0]["at"])

    def test_sigint_and_disconnect_cancel_only_corresponding_request(self):
        cli = self.cli("type", "--window", WINDOW, "slow")
        start = self.wait(lambda: self.records("start"))[0]
        queued, sock = self.send(text="quick")
        before = time.monotonic()
        cli.send_signal(signal.SIGINT)
        out, err = cli.communicate(timeout=2)
        self.assertEqual(cli.returncode, 130)
        self.assertEqual(err, "")
        result = json.loads(out)
        self.assertEqual(result["request_id"], start["id"])
        self.assertEqual(result["error"]["outcome"], "unknown")
        cancelled = self.wait(lambda: self.records("cancel", start["id"]))[0]
        self.measured("sigint_or_disconnect_to_cancel", cancelled["at"] - before)
        self.assertTrue(self.receive(sock)["ok"])
        self.assertFalse(self.records("cancel", queued.request_id))
        gone, sock = self.send()
        self.wait(lambda: self.records("start", gone.request_id))
        before = time.monotonic()
        sock.close()
        cancelled = self.wait(lambda: self.records("cancel", gone.request_id))[0]
        self.measured("sigint_or_disconnect_to_cancel", cancelled["at"] - before)
        self.wait(lambda: self.records("cleanup", gone.request_id))

    def test_reserved_control_under_ordinary_saturation_and_wrong_generation(self):
        first, sock = self.send()
        self.wait(lambda: self.records("start", first.request_id))
        for _ in range(31):
            self.connect().sendall(b"\x00")
        result = self.cancel(first.request_id, "b" * 32)
        self.assertEqual(result["error"]["code"], "generation_mismatch")
        self.assertFalse(self.records("cancel", first.request_id))
        before = time.monotonic()
        result = self.cancel(first.request_id)
        self.assertTrue(result["result"]["cancel_requested"])
        self.measured("saturated_control_to_cancel", self.records("cancel", first.request_id)[0]["at"] - before)
        self.assertEqual(self.receive(sock)["error"]["code"], "cancelled")
        self.assertFalse(self.cancel(first.request_id)["result"]["cancel_requested"])

    def test_sustained_controls_do_not_starve_work_expiry_or_child_reaping(self):
        first, sock = self.send(text="child", timeout=.15)
        child = self.wait(lambda: self.records("child", first.request_id))[0]
        deadline = time.monotonic() + .35
        def flood(malformed):
            count = 0
            while time.monotonic() < deadline:
                with socket.socket(socket.AF_UNIX) as control:
                    control.settimeout(1)
                    control.connect(str(self.root / "agent-desktop/g" / GEN / "priority.sock"))
                    import uuid
                    payload = CancelRequest(uuid.uuid4().hex, "default", GEN, "e" * 32).payload()
                    if malformed:
                        payload["extra"] = True
                    control.sendall(encode(payload))
                    decoder = Decoder()
                    while True:
                        data = control.recv(4096)
                        if not data or decoder.feed(data) is not None:
                            break
                    count += 1
            return count
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(flood, i % 2) for i in range(4)]
            result = self.receive(sock)
            next_req, next_sock = self.send(text="quick")
            self.assertTrue(self.receive(next_sock)["ok"])
            counts = [future.result() for future in futures]
        self.assertGreater(sum(counts), 20)
        self.assertEqual(result["error"]["code"], "timeout")
        admitted = self.records("admitted", first.request_id)[0]["at"]
        cancelled = self.records("cancel", first.request_id)[0]["at"]
        self.measured("flood_timeout_detection", cancelled - (admitted + .15))
        self.assertFalse(Path(f"/proc/{child['pid']}").exists(), "Owned child must be reaped")
        self.assertTrue(self.records("done", next_req.request_id))

    def test_reset_and_stop_cleanup_survive_cli_exit(self):
        first, sock = self.send()
        self.wait(lambda: self.records("start", first.request_id))
        reset = self.cli("input", "reset")
        reset_start = self.wait(lambda: [r for r in self.records("control_admitted") if r["operation"] == "input.reset"])[0]
        reset.kill()
        reset.communicate(timeout=2)
        self.wait(lambda: self.records("done", reset_start["id"]))
        self.assertEqual(self.receive(sock)["error"]["code"], "cancelled")
        stop = self.cli("session", "stop")
        stop_start = self.wait(lambda: [r for r in self.records("control_admitted") if r["operation"] == "session.stop"])[0]
        stop.kill()
        stop.communicate(timeout=2)
        self.wait(lambda: self.records("done", stop_start["id"]))
        _, rejected = self.send(text="quick")
        self.assertEqual(self.receive(rejected)["error"]["code"], "session_unavailable")

if __name__ == "__main__":
    unittest.main()
