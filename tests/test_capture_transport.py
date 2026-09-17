"""Local transport ownership/deadline tests; no desktop or D-Bus server needed."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import dbus
import dbus.lowlevel


SPEC = importlib.util.spec_from_file_location(
    "capture_probe", Path(__file__).resolve().parents[1] / "tools/capture_probe.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class NativeFdOwnership(unittest.TestCase):
    def test_message_append_duplicates_fd_and_message_destruction_releases_last_writer(self):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(read_fd, False)
        inode = os.readlink(f"/proc/self/fd/{read_fd}")
        wrapper = message = None
        try:
            wrapper = dbus.types.UnixFd(write_fd)
            message = dbus.lowlevel.MethodCallMessage(
                "org.kde.KWin.ScreenShot2", "/org/kde/KWin/ScreenShot2",
                "org.kde.KWin.ScreenShot2", "CaptureScreen")
            message.append("Virtual-1", dbus.Dictionary({}, signature="sv"), wrapper, signature="sa{sv}h")
            self.assertEqual(len(capture.pipe_inventory(inode)), 4)
            os.close(write_fd)
            write_fd = None
            os.close(wrapper.take())
            wrapper = None
            self.assertEqual(sorted(row["access"] for row in capture.pipe_inventory(inode)),
                             [os.O_RDONLY, os.O_WRONLY])
            with self.assertRaises(BlockingIOError):
                os.read(read_fd, 1)
            # No bus was used: the native message itself owns the remaining copy.
            message = None
            self.assertEqual(os.read(read_fd, 1), b"")
            self.assertEqual(capture.pipe_inventory(inode), [{"fd": read_fd, "access": os.O_RDONLY}])
        finally:
            message = None
            if wrapper is not None:
                os.close(wrapper.take())
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)

    def test_call_setup_failure_closes_original_wrapper_reader_and_bus(self):
        with tempfile.TemporaryDirectory() as root:
            artifacts = Path(root)
            folder = artifacts / "capture-test"
            folder.mkdir()
            config = folder / "request.json"
            config.write_text(json.dumps({"generation": "test", "request_id": folder.name,
                                          "screen": "Virtual-1", "deadline": time.monotonic() + 2}))
            bus = Mock()
            bus.call_async.side_effect = RuntimeError("injected before message queue ownership")
            original_pipe = os.pipe2
            inodes = []

            def pipe(flags):
                fds = original_pipe(flags)
                inodes.append(os.readlink(f"/proc/self/fd/{fds[0]}"))
                return fds

            context = ("test", artifacts, artifacts, {"screenshot": {"enabled": True}}, time.monotonic() + 3)
            with patch.object(capture.libei.windows, "private_context", return_value=context), \
                    patch.object(dbus.bus, "BusConnection", return_value=bus), \
                    patch.object(capture.os, "pipe2", side_effect=pipe), \
                    patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/not-used"}), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(capture.capture_child(config), 1)
            bus.close.assert_called_once_with()
            self.assertEqual(len(inodes), 1)
            self.assertEqual(capture.pipe_inventory(inodes[0]), [])
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[-1]["stage"], "error")
            self.assertIn("injected", rows[-1]["message"])
            self.assertFalse((folder / "image.partial").exists())

    def test_complete_payload_without_eof_times_out_and_closes_all_local_fds(self):
        from gi.repository import GLib

        expected_bytes = 720 * 5120
        metadata = {"type": "raw", "width": 1280, "height": 720, "stride": 5120,
                    "format": 6, "screen": "Virtual-1", "scale": 1.0}
        writers = []

        def close_writers():
            for writer in writers:
                if writer.poll() is None:
                    writer.kill()
                writer.wait(timeout=1)

        self.addCleanup(close_writers)
        with tempfile.TemporaryDirectory() as root:
            artifacts = Path(root)
            folder = artifacts / "capture-held-writer"
            folder.mkdir()
            config = folder / "request.json"
            config.write_text(json.dumps({"generation": "test", "request_id": folder.name,
                                          "screen": "Virtual-1", "deadline": time.monotonic() + .6}))
            bus, pending = Mock(), Mock()
            bus.close.side_effect = close_writers

            def call_async(destination, path, interface, method, signature, args, reply, error, **kwargs):
                message = dbus.lowlevel.MethodCallMessage(destination, path, interface, method)
                message.append(*args, signature=signature)
                # Model transport transfer using real native FD copies. Only the
                # controlled writer child retains the received writer afterward.
                received = message.get_args_list()[2]
                writer_fd = received.take()
                try:
                    writers.append(subprocess.Popen(
                        ["/usr/bin/python", "-I", "-c", """import os, sys, time
fd = int(sys.argv[1])
payload = memoryview(bytes(int(sys.argv[2])))
while payload:
    payload = payload[os.write(fd, payload):]
time.sleep(5)
""", str(writer_fd), str(expected_bytes)], pass_fds=(writer_fd,),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                finally:
                    os.close(writer_fd)
                    message = None
                GLib.idle_add(lambda: reply(metadata))
                return pending

            bus.call_async.side_effect = call_async
            context = ("test", artifacts, artifacts, {"screenshot": {"enabled": True}}, time.monotonic() + 1)
            with patch.object(capture.libei.windows, "private_context", return_value=context), \
                    patch.object(dbus.bus, "BusConnection", return_value=bus), \
                    patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/not-used"}), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(capture.capture_child(config), 1)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[-1]["code"], "capture_timeout")
            self.assertEqual(rows[-1]["bytes"], expected_bytes)
            self.assertIn("metadata", [row["stage"] for row in rows])
            for stage in ("eof", "raw-complete", "encode", "success"):
                self.assertNotIn(stage, [row["stage"] for row in rows])
            self.assertEqual(capture.pipe_inventory(rows[0]["inode"]), [])
            self.assertFalse((folder / "image.png").exists())
            self.assertFalse((folder / "image.partial").exists())
            pending.cancel.assert_called_once_with()
            bus.close.assert_called_once_with()
            self.assertEqual(len(writers), 1)
            self.assertIsNotNone(writers[0].poll())


class CaptureSupervisor(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.owner = types.SimpleNamespace(
            artifacts=Path(self.temporary.name), generation="test", deadline=time.monotonic() + 10,
            manifest={"initial_presented": {"sole_output_name": "Virtual-1"},
                      "processes": {"kwin": {"pid": os.getpid()}}},
            env={"PATH": "/usr/bin:/bin"}, fatal=None)

    def child(self, body):
        popen = subprocess.Popen
        source = """import json, pathlib, sys, time
config = json.loads(pathlib.Path(sys.argv[1]).read_text())
folder = pathlib.Path(sys.argv[1]).parent
def report(stage, **fields):
    print(json.dumps(dict(stage=stage, generation=config['generation'],
                          request_id=config['request_id'], **fields)), flush=True)
""" + body

        def controlled(argv, **kwargs):
            return popen(["/usr/bin/python", "-I", "-c", source, argv[-1]], **kwargs)

        with patch.object(capture.subprocess, "Popen", side_effect=controlled):
            value = capture.Capture(self.owner)

        def cleanup():
            if value.process.poll() is None:
                value.process.kill()
            value.process.wait(timeout=1)
            value.process.stdout.close()
            value.err.close()

        self.addCleanup(cleanup)
        return value

    def finish(self, value):
        end = time.monotonic() + 2
        while not value.done and self.owner.fatal is None and time.monotonic() < end:
            value.tick()
            time.sleep(.005)
        self.assertIsNone(self.owner.fatal)
        self.assertTrue(value.done, value.result)
        self.assertTrue(value.result["process_reaped"])
        self.assertTrue(value.process.stdout.closed)
        self.assertTrue(value.err.closed)
        self.assertEqual(json.loads((value.folder / "receipt.json").read_text())["accepted"],
                         value.result["accepted"])

    def test_late_success_is_rejected_and_both_publication_paths_removed(self):
        value = self.child("""
(folder / 'image.png').write_bytes(b'controlled-test-output')
(folder / 'image.partial').write_bytes(b'partial')
report('success', path=str(folder / 'image.png'))
""")
        value.process.wait(timeout=1)
        value.deadline = time.monotonic() - .01
        self.finish(value)
        self.assertEqual(value.result["error"], "capture_timeout")
        self.assertFalse(value.result["accepted"])
        self.assertTrue(value.result["session_stop_required"])
        self.assertFalse((value.folder / "image.png").exists())
        self.assertFalse((value.folder / "image.partial").exists())

    def test_publication_check_crossing_deadline_cannot_accept_stale_time(self):
        value = self.child("""
(folder / 'image.png').write_bytes(b'controlled-test-output')
report('success', path=str(folder / 'image.png'))
""")
        value.process.wait(timeout=1)
        original_is_file = Path.is_file
        checks = []

        def delayed_publication(path):
            if path == value.folder / "image.png":
                time.sleep(.05)
                checks.append(time.monotonic())
            return original_is_file(path)

        value.deadline = time.monotonic() + .02
        with patch.object(Path, "is_file", delayed_publication):
            self.finish(value)
        self.assertEqual(len(checks), 1)
        self.assertGreater(checks[0], value.deadline)
        self.assertFalse(value.result["accepted"])
        self.assertNotIn("accepted_at", value.result)
        self.assertEqual(value.result["error"], "capture_timeout")
        self.assertTrue(value.result["session_stop_required"])
        self.assertFalse((value.folder / "image.png").exists())

    def test_acceptance_timestamp_follows_process_and_publication_checks(self):
        value = self.child("""
(folder / 'image.png').write_bytes(b'controlled-test-output')
report('success', path=str(folder / 'image.png'))
""")
        value.process.wait(timeout=1)
        original_is_file = Path.is_file
        checks = []

        def measured_publication(path):
            answer = original_is_file(path)
            if path == value.folder / "image.png":
                checks.append(time.monotonic())
            return answer

        with patch.object(Path, "is_file", measured_publication):
            self.finish(value)
        self.assertTrue(value.result["accepted"])
        self.assertEqual(len(checks), 1)
        self.assertGreaterEqual(value.result["accepted_at"], checks[0])
        self.assertLess(value.result["accepted_at"], value.deadline)
        self.assertEqual(value.result["seconds"], value.result["accepted_at"] - value.started)

    def test_abort_kills_and_reaps_running_child(self):
        value = self.child("time.sleep(30)\n")
        (value.folder / "image.partial").write_bytes(b"partial")
        value.abort("test_cancel")
        self.finish(value)
        self.assertLess(value.result["cleanup_seconds"], capture.CLEANUP_SECONDS)
        self.assertEqual(value.result["error"], "test_cancel")
        self.assertLess(value.result["returncode"], 0)
        self.assertTrue(value.result["local_cleanup"])
        self.assertFalse((value.folder / "image.partial").exists())

    def test_wrong_generation_or_request_rejects_child_status(self):
        for key in ("generation", "request_id"):
            with self.subTest(key=key):
                value = self.child(f"config[{key!r}] = 'stale'\nreport('success', path=str(folder / 'image.png'))\n")
                self.finish(value)
                self.assertFalse(value.result["accepted"])
                self.assertEqual(value.result["error"], "capture_protocol")
                self.assertEqual(value.rows, [])

    def test_status_cap_terminates_writer_without_unbounded_buffer(self):
        value = self.child("sys.stdout.write('x' * (256 * 1024))\nsys.stdout.flush()\ntime.sleep(30)\n")
        self.finish(value)
        self.assertFalse(value.result["accepted"])
        self.assertEqual(value.result["error"], "capture_protocol")
        self.assertLessEqual(len(value.buffer), capture.MAX_STATUS)

    def test_exact_invalid_screen_is_the_safe_rejection(self):
        for suffix, expected_stop in (("InvalidScreen", False), ("OtherFailure", True)):
            with self.subTest(suffix=suffix):
                value = self.child(
                    "report('error', code='capture_dbus', dbus_error='org.kde.KWin.ScreenShot2.Error."
                    + suffix + "')\nsys.exit(1)\n")
                self.finish(value)
                self.assertEqual(value.result["session_stop_required"], expected_stop)


if __name__ == "__main__":
    unittest.main()
