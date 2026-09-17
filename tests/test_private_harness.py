"""Boundary tests; real KWin compatibility is recorded separately in evidence."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("private_harness", Path(__file__).resolve().parents[1] / "tools/private_harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class HarnessBoundaries(unittest.TestCase):
    def test_environment_does_not_inherit_caller_endpoints(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"DISPLAY": ":666", "WAYLAND_SOCKET": "9", "AT_SPI_BUS_ADDRESS": "host",
                                     "DBUS_SESSION_BUS_ADDRESS": "host", "PYTHONPATH": "/host", "LD_PRELOAD": "/host",
                                     "HOME": "/host", "KDE_APPLICATIONS_AS_SCOPE": "1",
                                     "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1"}):
            env = harness.clean_env("/private/runtime", "a" * 32)
        for key in ("DISPLAY", "WAYLAND_SOCKET", "AT_SPI_BUS_ADDRESS", "PYTHONPATH", "LD_PRELOAD", "KDE_APPLICATIONS_AS_SCOPE",
                    "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS"):
            self.assertNotIn(key, env)
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/private/runtime/bus")
        self.assertEqual(env["HOME"], "/private/runtime/home")

    def test_screenshot_environment_is_private_kwin_only_with_or_without_plugin(self):
        key = "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS"
        for enabled in (False, True):
            for plugin in (False, True):
                with self.subTest(enabled=enabled, plugin=plugin), tempfile.TemporaryDirectory() as root:
                    root = Path(root)
                    manifest = root / "manifest.json"
                    manifest.write_text(json.dumps({
                        "generation": "a" * 32, "runtime": str(root), "cwd": str(root),
                        "fixture_binary": "/fixture", "probe": ["/probe"], "inject": "none",
                        "screenshot": {"enabled": enabled}, "eis_fault_plugin": plugin}))
                    worker = harness.Worker(manifest)
                    worker.selector.close()
                    worker.selector = Mock()
                    worker.latest = {"state": 0}
                    child = Mock(pid=os.getpid(), returncode=0)
                    child.poll.return_value = 0
                    try:
                        with patch.object(harness.subprocess, "Popen", return_value=child) as popen, \
                                patch.object(harness, "command"), \
                                patch.object(Path, "is_socket", return_value=True), \
                                patch.object(harness.os, "chmod"), \
                                patch.object(harness.socket, "socket"):
                            worker.run()
                        environments = {call.args[0][0]: call.kwargs["env"] for call in popen.call_args_list}
                        kwin = environments.pop("/usr/bin/kwin_wayland")
                        self.assertEqual(kwin.get(key), "1" if enabled else None)
                        self.assertEqual(kwin.get("HARNESS_EIS_FAULT_PLUGIN"), "1" if plugin else None)
                        self.assertEqual(kwin.get("QT_PLUGIN_PATH"), str(root / "plugins") if plugin else None)
                        for env in [worker.env, *environments.values()]:
                            self.assertNotIn(key, env)
                            self.assertNotIn("HARNESS_EIS_FAULT_PLUGIN", env)
                            self.assertNotIn("QT_PLUGIN_PATH", env)
                    finally:
                        worker.event_log.close()
                        for stream in worker.streams:
                            stream.close()

    def test_screenshot_opt_in_is_recorded_before_launch(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as root:
                args = types.SimpleNamespace(artifacts=root, build_root=root, probe=[], inject="none",
                                             eis_fault_plugin=None, screenshot=enabled)
                with patch.object(harness, "build", side_effect=harness.Failure("test_stop", "Before launch")), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(harness.run(args), 1)
                manifest, = Path(root).glob("*/manifest.json")
                data = json.loads(manifest.read_text())
                self.assertEqual(data["screenshot"], {
                    "enabled": enabled, "kwin_only_environment": True,
                    "environment": {"KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1"} if enabled else {}})
                self.assertTrue(data["cleanup"]["never_launched"])

    def test_screenshot_cli_requires_explicit_opt_in(self):
        for flags, enabled in (([], False), (["--screenshot"], True)):
            with self.subTest(flags=flags), patch("sys.argv", ["private_harness.py", "run", *flags]), \
                    patch.object(harness, "run", return_value=0) as run, patch.object(harness.os, "umask"):
                self.assertEqual(harness.main(), 0)
                self.assertIs(run.call_args.args[0].screenshot, enabled)

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
