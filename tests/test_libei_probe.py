import importlib.util
import errno
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("libei_probe", ROOT / "tools/libei_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NoCalls:
    def __getattr__(self, name):
        raise AssertionError("Unexpected native input call: " + name)


class InputSafetyTests(unittest.TestCase):
    def state(self, resumed=True, uncertain=False, resetting=False):
        value = object.__new__(probe.Input)
        value.connected = True
        value.uncertain = uncertain
        value.resetting = resetting
        value.devices = {17: {"resumed": resumed, "emulating": resumed}}
        value.held = {}
        value.lib = NoCalls()
        return value

    def test_announced_paused_uncertain_and_resetting_devices_are_gated(self):
        for args in ({"resumed": False}, {"uncertain": True}, {"resetting": True}):
            value = self.state(**args)
            with self.subTest(args=args), self.assertRaises(probe.Failure) as caught:
                value.press(["W"])
            self.assertEqual(caught.exception.code, "input_unavailable")

    def test_entire_invalid_chord_rejected_before_native_calls(self):
        for names in (["SHIFT", "bad"], ["W", "W"], []):
            with self.subTest(names=names), self.assertRaises(probe.Failure) as caught:
                self.state().press(names)
            self.assertEqual(caught.exception.code, "unsupported_input")

    def test_disconnected_device_cannot_send(self):
        value = self.state()
        value.connected = False
        with self.assertRaises(probe.Failure):
            value.press(["W"])

    def test_release_on_paused_device_preserves_uncertainty_and_ledger(self):
        value = self.state(resumed=False)
        value.held = {17: [42, 17]}
        value.epoch = 1
        events = []
        value.owner = type("Owner", (), {"log": lambda self, *a, **kw: events.append((a, kw))})()
        value.release()
        self.assertTrue(value.uncertain)
        self.assertEqual(value.held, {17: [42, 17]})
        self.assertEqual(events[0][0], ("release_uncertain",))

    def test_real_installed_header_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = probe.binding.audit(directory)
        self.assertTrue(receipt["passed"])
        self.assertTrue(receipt["dispatch_restype_is_void"])
        self.assertEqual(len(receipt["declarations"]), 24)

    def test_unknown_native_build_rejected_before_loading(self):
        with patch.object(probe.binding, "SUPPORTED_LIBRARY_SHA256", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "failed-setup FD ownership"):
                probe.binding.load()

    def test_installed_negative_setup_keeps_fd_until_caller_closes(self):
        library = probe.binding.load()
        with tempfile.TemporaryFile() as regular:
            fd = os.dup(regular.fileno())
            context = library.ei_new_sender(None)
            try:
                os.set_blocking(fd, False)
                self.assertEqual(library.ei_setup_backend_fd(context, fd), -errno.EPERM)
                os.fstat(fd)  # Actual 1.6.0 failure left this descriptor open.
                os.close(fd)
            finally:
                library.ei_unref(context)

    def test_failed_setup_closes_immediately_without_reused_or_unrelated_fd_damage(self):
        # Exercise Input.setup with the real library. The log callback reuses the
        # just-closed number BEFORE dispose, exposing a late/double close.
        probe.binding.load()
        before = len(os.listdir("/proc/self/fd"))
        with tempfile.TemporaryFile() as regular:
            unrelated = os.open("/dev/null", os.O_RDONLY)
            try:
                for _ in range(8):
                    fd = os.dup(regular.fileno())
                    records = []
                    def log(event, **fields):
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                        os.dup2(unrelated, fd)
                        records.append(fields)
                    owner = SimpleNamespace(generation="0" * 32, log=log)
                    value = probe.Input(owner)
                    try:
                        with self.assertRaises(probe.Failure) as caught:
                            value.setup(fd)
                        self.assertEqual(caught.exception.code, "input_setup")
                        self.assertEqual(records[0]["result"], -errno.EPERM)
                        self.assertFalse(value.ready())
                        self.assertIsNone(value.watch)
                    finally:
                        value.dispose()
                    self.assertEqual(os.fstat(fd).st_rdev, os.fstat(unrelated).st_rdev)
                    os.close(fd)
                    os.fstat(unrelated)
            finally:
                os.close(unrelated)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_successful_setup_transfers_fd_once_to_installed_library(self):
        library = probe.binding.load()
        left, right = socket.socketpair()
        fd = left.detach()
        context = library.ei_new_sender(None)
        try:
            os.set_blocking(fd, False)
            self.assertEqual(library.ei_setup_backend_fd(context, fd), 0)
            os.fstat(fd)
        finally:
            library.ei_unref(context)
            right.close()
        with self.assertRaises(OSError) as closed:
            os.fstat(fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)


if __name__ == "__main__":
    unittest.main()
