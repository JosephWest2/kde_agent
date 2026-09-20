from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("dependencies", Path(__file__).resolve().parents[1] / "tools/dependencies.py")
deps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deps)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = deps.Runner(self.root, 20)

    def test_inherited_environment_and_python_startup_excluded(self):
        marker = self.root / "startup-ran"
        (self.root / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()")
        with patch.dict(os.environ, {"SECRET_TEST_TOKEN": "private-sentinel", "DISPLAY": ":999", "DBUS_SESSION_BUS_ADDRESS": "host-sentinel", "PYTHONPATH": str(self.root), "LD_PRELOAD": "secret-library", "CARGO_BUILD_RUSTFLAGS": "secret-flags"}):
            output = self.runner.run([sys.executable, "-I", "-c", "import os,json; print(json.dumps(dict(os.environ)))"])
        for forbidden in ("private-sentinel", "host-sentinel", "secret-library", "secret-flags", "PYTHONPATH", "DISPLAY"):
            self.assertNotIn(forbidden, output)
        self.assertFalse(marker.exists())

    def test_missing_tool_and_binding_actionable(self):
        with self.assertRaises(deps.Failure) as caught:
            self.runner.run([str(self.root / "absent")])
        self.assertEqual(caught.exception.error["code"], "missing_tool")
        with self.assertRaises(deps.Failure) as caught:
            self.runner.run([sys.executable, "-I", "-c", "import definitely_missing_native_binding"])
        self.assertEqual(caught.exception.error["code"], "command_failed")
        self.assertTrue(caught.exception.error["repair"])

    def test_timeout_kills_descendant_retaining_output(self):
        pidfile = self.root / "child.pid"
        code = "import os,time; pid=os.fork(); " + f"open({str(pidfile)!r},'w').write(str(pid)) if pid else None; time.sleep(30)"
        start = time.monotonic()
        with self.assertRaises(deps.Failure) as caught:
            self.runner.run([sys.executable, "-I", "-c", code], seconds=0.2)
        self.assertEqual(caught.exception.error["code"], "timeout")
        self.assertLess(time.monotonic() - start, 2.5)
        child = int(pidfile.read_text())
        for _ in range(50):
            state = Path(f"/proc/{child}/stat")
            try:
                process_state = state.read_text().split(") ", 1)[1][0]
            except (FileNotFoundError, ProcessLookupError):
                break  # The kernel can reap it between opening and reading stat.
            if process_state == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail("Timed-out descendant is still running")

    def candidate(self, body):
        (self.root / "bin").mkdir()
        executable = self.root / "bin/kdotool"
        executable.write_text("#!/usr/bin/python3\n" + body)
        executable.chmod(0o700)

    def test_private_bus_reaped_on_success_and_failure(self):
        self.candidate("import sys\nprint(sys.argv[-1] + ' finished')\n")
        real_popen = subprocess.Popen
        daemons = []
        def record(argv, **kwargs):
            process = real_popen(argv, **kwargs)
            if argv[0] == "/usr/bin/dbus-daemon":
                daemons.append(process)
                self.assertNotIn("DISPLAY", kwargs["env"])
                self.assertTrue(kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/tmp/kde-check-"))
            return process
        with patch.object(deps.subprocess, "Popen", side_effect=record):
            self.assertTrue(deps.check_kdotool(self.runner, self.root)["daemon_reaped"])
            (self.root / "bin/kdotool").write_text("#!/usr/bin/python3\nraise SystemExit(7)\n")
            with self.assertRaises(deps.Failure):
                deps.check_kdotool(self.runner, self.root)
        self.assertEqual(len(daemons), 2)
        self.assertTrue(all(process.poll() is not None for process in daemons))

    def test_receipt_rejects_missing_fields_and_binary_tampering(self):
        data = {"kdotool": {"revision": "abc", "release": "0.3.0", "patches": []}}
        state = {"revision": "abc", "cargo_lock_sha256": "lock", "clean_checkout": True}
        self.candidate("print('kdotool 0.3.0')\n")
        receipt = state | {"binary_sha256": deps.digest(self.root / "bin/kdotool"), "toolchain": {"rustc": "1", "cargo": "1"}, "build_command": "command", "resolved_cargo": {"lock_packages": [{"name": "kdotool", "version": "0.3.0"}], "resolved_nodes": [{"id": "kdotool", "dependencies": [], "deps": [], "features": []}]}, "patches": [], "release": "0.3.0"}
        receipt_path = self.root / "build.json"
        with patch.object(deps, "source_state", return_value=state):
            receipt_path.write_text(json.dumps(receipt))
            self.assertTrue(deps.verify_build(self.runner, self.root, data)["provenance_verified"])
            receipt_path.write_text(json.dumps({"revision": "abc"}))
            with self.assertRaises(deps.Failure):
                deps.verify_build(self.runner, self.root, data)
            receipt_path.write_text(json.dumps(receipt))
            (self.root / "bin/kdotool").write_text("tampered")
            with self.assertRaises(deps.Failure) as caught:
                deps.verify_build(self.runner, self.root, data)
            self.assertEqual(caught.exception.error["code"], "provenance_mismatch")

    def test_cli_malformed_nested_receipts_return_actionable_json(self):
        receipt = json.loads((deps.PROJECT / "evidence/issue-9/environment.json").read_text())["kdotool"]
        receipt_path = self.root / "build.json"
        mutations = [
            ("toolchain", None), ("toolchain", []), ("toolchain", "old-format"),
            ("toolchain", {"rustc": None, "cargo": "1"}),
            ("resolved_cargo", None), ("resolved_cargo", []), ("resolved_cargo", "old-format"),
            ("resolved_cargo", {"lock_packages": None, "resolved_nodes": []}),
            ("resolved_cargo", {"lock_packages": ["invalid"], "resolved_nodes": [None]}),
            ("resolved_cargo", {"lock_packages": [{"name": [], "version": "1"}], "resolved_nodes": [{}]}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                receipt_path.write_text(json.dumps(receipt | {field: value}))
                result = subprocess.run([sys.executable, "-I", str(deps.PROJECT / "tools/dependencies.py"), "check-kdotool", "--root", str(self.root)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 1)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["errors"][0]["code"], "provenance_mismatch")
                self.assertIn("fresh --root", payload["errors"][0]["repair"])
                self.assertEqual(result.stderr, b"")

    def test_commit_and_lock_mismatches(self):
        source = self.root / "kdotool-source"
        source.mkdir()
        (source / "Cargo.lock").write_text("lock")
        data = {"kdotool": {"revision": "expected", "cargo_lock_sha256": "expected-lock"}}
        with patch.object(self.runner, "run", return_value="other"):
            with self.assertRaises(deps.Failure):
                deps.source_state(self.runner, self.root, data)
        with patch.object(self.runner, "run", side_effect=["expected", ""]):
            with self.assertRaises(deps.Failure):
                deps.source_state(self.runner, self.root, data)

    def test_failed_build_does_not_write_success_receipt(self):
        (self.root / "kdotool-source").mkdir()
        with patch.object(deps, "source_state", return_value={}), patch.object(deps, "rust_tools", return_value=({"cargo": "/missing-cargo", "rustc": "/missing-rustc"}, {})):
            with self.assertRaises(deps.Failure):
                deps.build(self.runner, self.root, {})
        self.assertFalse((self.root / "build.json").exists())


    def test_user_git_configuration_is_not_read(self):
        hostile_home = self.root / "host-home"
        hostile_home.mkdir()
        (hostile_home / ".gitconfig").write_text('[user]\n    name = credential-sentinel\n')
        with patch.dict(os.environ, {"HOME": str(hostile_home), "GIT_CONFIG_GLOBAL": str(hostile_home / ".gitconfig")}):
            output = self.runner.run(["/usr/bin/git", "config", "--list"], cwd=self.root)
        self.assertNotIn("credential-sentinel", output)

    def test_project_cargo_configuration_rejected_before_build(self):
        (self.root / "kdotool-source").mkdir()
        cargo_home = self.root / "cargo-home"
        cargo_home.mkdir()
        (cargo_home / "config.toml").write_text('[build]\nrustflags = ["credential-sentinel"]\n')
        with patch.object(deps, "source_state", return_value={}), patch.object(deps, "rust_tools", return_value=({"cargo": "/should-not-run", "rustc": "/should-not-run"}, {})):
            with self.assertRaises(deps.Failure) as caught:
                deps.build(self.runner, self.root, {})
        self.assertEqual(caught.exception.error["code"], "unexpected_config")
        self.assertNotIn("credential-sentinel", str(caught.exception))
        self.assertFalse((self.root / "build.json").exists())

    def test_ancestor_cargo_configuration_rejected_before_build(self):
        (self.root / "kdotool-source").mkdir()
        (self.root / ".cargo").mkdir()
        (self.root / ".cargo/config.toml").write_text('[build]\nrustflags = ["sentinel"]\n')
        controlled = self.root / "controlled"
        controlled.mkdir()
        with patch.object(deps, "source_state", return_value={}), patch.object(deps, "rust_tools", return_value=({"cargo": "/should-not-run", "rustc": "/should-not-run"}, {})), patch.object(deps.tempfile, "TemporaryDirectory", return_value=nullcontext(str(controlled))):
            with self.assertRaises(deps.Failure) as caught:
                deps.build(self.runner, self.root, {})
        self.assertEqual(caught.exception.error["code"], "unexpected_config")
        self.assertFalse((self.root / "build.json").exists())

    def test_cli_json_failure_from_another_directory(self):
        result = subprocess.run([sys.executable, "-I", str(deps.PROJECT / "tools/dependencies.py"), "report", "--root", "missing-root"], cwd=self.root, capture_output=True, timeout=125)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("missing_build", {error["code"] for error in payload["errors"]})
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
