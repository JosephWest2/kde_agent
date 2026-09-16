import copy
import importlib.util
import json
import time
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "tools/kdotool_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
ID = "{12345678-1234-1234-1234-123456789abc}"


class WindowContractTests(unittest.TestCase):
    def setUp(self):
        self.value = {"schema_version": 1, "request_id": "request", "active_uuid": ID,
                      "windows": [{"uuid": ID, "pid": 42, "title": "", "class": None,
                                   "client": {"x": 3.5, "y": 4, "width": 640, "height": 360},
                                   "frame": None, "active": True}]}

    def test_preserves_real_content_origin_and_null_metadata(self):
        self.assertEqual(probe.snapshot(self.value, "request"), self.value)
        self.assertEqual(self.value["windows"][0]["client"]["x"], 3.5)

    def test_rejects_duplicate_ids_missing_metadata_and_invalid_bounds(self):
        cases = []
        value = copy.deepcopy(self.value); value["windows"] *= 2; cases.append(value)
        value = copy.deepcopy(self.value); del value["windows"][0]["class"]; cases.append(value)
        value = copy.deepcopy(self.value); value["windows"][0]["client"]["x"] = float("nan"); cases.append(value)
        value = copy.deepcopy(self.value); value["windows"][0]["client"]["width"] = True; cases.append(value)
        value = copy.deepcopy(self.value); value["windows"][0]["active"] = False; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(probe.Failure):
                probe.snapshot(value, "request")

    def test_encodes_dynamic_text_as_ascii_json_data(self):
        value = {"echo": '"; throw Error(); //\n` ${injected} 雪\u2028\\'}
        declaration = probe.encoded(value)
        self.assertTrue(declaration.isascii())
        self.assertEqual(json.loads(declaration[len("const request = "):-2]), value)
        with self.assertRaises(ValueError):
            probe.encoded({"x": float("nan")})

    def test_private_context_rejects_missing_identity_before_desktop_connection(self):
        with self.assertRaises(probe.Failure) as error:
            probe.private_context({})
        self.assertEqual(error.exception.code, "private_context")

    def test_private_context_rejects_redirected_bus_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            generation = "a" * 32
            (runtime / "owner.json").write_text(json.dumps({"generation": generation}))
            env = probe.harness.clean_env(runtime, generation)
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/wrong/bus"
            with self.assertRaises(probe.Failure) as error:
                probe.private_context(env)
            self.assertEqual(error.exception.code, "private_context")

    def test_successful_activation_exit_does_not_establish_focus(self):
        instance = object.__new__(probe.Probe)
        instance.deadline = time.monotonic() + 1
        instance.query = lambda deadline: {"windows": [{"uuid": ID}], "active_uuid": None}
        calls = []
        instance.execute = lambda *args, **kwargs: calls.append(kwargs)
        original = probe.FOCUS_SECONDS
        probe.FOCUS_SECONDS = .015
        try:
            with self.assertRaises(probe.Failure) as error:
                instance.focus(ID)
        finally:
            probe.FOCUS_SECONDS = original
        self.assertEqual(error.exception.code, "timeout")
        self.assertEqual(calls[0]["native"], ["windowactivate", ID])

    def test_vanished_target_rejects_before_activation(self):
        instance = object.__new__(probe.Probe)
        instance.deadline = time.monotonic() + 1
        instance.query = lambda deadline: {"windows": [], "active_uuid": None}
        instance.execute = lambda *a, **kw: self.fail("activation of vanished target")
        with self.assertRaises(probe.Failure) as error:
            instance.focus(ID)
        self.assertEqual(error.exception.code, "target_missing")

    def test_late_matching_focus_snapshot_cannot_succeed(self):
        instance = object.__new__(probe.Probe)
        instance.deadline = time.monotonic() + 1
        count = 0
        def query(deadline):
            nonlocal count
            count += 1
            if count > 1:
                time.sleep(max(0, deadline - time.monotonic()) + .001)
            return {"windows": [{"uuid": ID}], "active_uuid": ID}
        instance.query = query
        instance.execute = lambda *a, **kw: None
        original = probe.FOCUS_SECONDS
        probe.FOCUS_SECONDS = .01
        try:
            with self.assertRaises(probe.Failure) as error:
                instance.focus(ID)
        finally:
            probe.FOCUS_SECONDS = original
        self.assertEqual(error.exception.code, "timeout")


if __name__ == "__main__":
    unittest.main()
