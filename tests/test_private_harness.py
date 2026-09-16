"""Boundary tests; real KWin compatibility is recorded separately in evidence."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

SPEC = importlib.util.spec_from_file_location("private_harness", Path(__file__).resolve().parents[1] / "tools/private_harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class HarnessBoundaries(unittest.TestCase):
    def test_environment_does_not_inherit_caller_endpoints(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"DISPLAY": ":666", "WAYLAND_SOCKET": "9", "AT_SPI_BUS_ADDRESS": "host",
                                     "DBUS_SESSION_BUS_ADDRESS": "host", "PYTHONPATH": "/host", "LD_PRELOAD": "/host",
                                     "HOME": "/host", "KDE_APPLICATIONS_AS_SCOPE": "1"}):
            env = harness.clean_env("/private/runtime", "a" * 32)
        for key in ("DISPLAY", "WAYLAND_SOCKET", "AT_SPI_BUS_ADDRESS", "PYTHONPATH", "LD_PRELOAD", "KDE_APPLICATIONS_AS_SCOPE"):
            self.assertNotIn(key, env)
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/private/runtime/bus")
        self.assertEqual(env["HOME"], "/private/runtime/home")

    def test_symlinked_artifact_ancestor_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "real").mkdir()
            (root / "alias").symlink_to(root / "real")
            with self.assertRaises(harness.Failure):
                harness.private_dir(root / "alias" / "child")
            self.assertFalse((root / "real" / "child").exists())

    def test_finalizer_refuses_wrong_generation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "owner.json").write_text(json.dumps({"generation": "b" * 32}))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"generation": "a" * 32, "runtime": str(runtime)}))
            with self.assertRaises(harness.Failure):
                harness.finalize(manifest)
            self.assertTrue(runtime.exists())

    def events_worker(self):
        worker = harness.Worker.__new__(harness.Worker)
        worker.data = {"generation": "a" * 32}
        worker.event_log = io.BytesIO()
        worker.event_buffer = b""
        worker.latest = worker.output = None
        worker.request = {"op": "set_state", "state": 17}
        worker.active_control_id = 7
        worker.target_revision = None
        worker.responses = []
        worker.respond = lambda ok, **data: worker.responses.append({"ok": ok, **data})
        return worker

    def feed(self, worker, *events):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb")
        worker.children = {"fixture": types.SimpleNamespace(stdout=reader)}
        with os.fdopen(write_fd, "wb") as writer:
            writer.write(b"".join(json.dumps({"generation": "a" * 32, **event}).encode() + b"\n" for event in events))
        try:
            worker.read_events()
        finally:
            reader.close()

    def test_intervening_input_and_abandoned_control_cannot_ack_current_request(self):
        worker = self.events_worker()
        self.feed(worker,
                  {"event": "committed", "revision": 2, "control_id": 6, "source": "control"},
                  {"event": "presented", "revision": 2, "control_id": 6},
                  {"event": "committed", "revision": 3, "control_id": 7, "source": "control"},
                  {"event": "presented", "revision": 4, "control_id": 0, "source": "input"})
        self.assertEqual(worker.target_revision, 3)
        self.assertEqual(worker.responses, [])
        self.feed(worker, {"event": "presented", "revision": 3, "control_id": 7})
        self.assertEqual(worker.responses, [{"ok": True, "presented": {"generation": "a" * 32,
                         "event": "presented", "revision": 3, "control_id": 7}}])

    def test_discarded_exact_revision_is_failure(self):
        worker = self.events_worker()
        self.feed(worker, {"event": "committed", "revision": 3, "control_id": 7, "source": "control"},
                  {"event": "discarded", "revision": 3, "control_id": 7})
        self.assertFalse(worker.responses[0]["ok"])
        self.assertEqual(worker.responses[0]["code"], "render_discarded")

    def test_subprocess_deadline_reaps_group(self):
        with self.assertRaises(harness.Failure) as caught:
            harness.command(["/usr/bin/python", "-I", "-c", "import time; time.sleep(10)"],
                            env={"PATH": "/usr/bin:/bin"}, timeout=0.05)
        self.assertEqual(caught.exception.code, "timeout")


if __name__ == "__main__":
    unittest.main()
