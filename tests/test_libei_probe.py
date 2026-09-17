import importlib.util
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
