"""Public subprocess contract and reusable validation regressions."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from agent_desktop import cli
from agent_desktop.contracts import (EXIT_CODES, OPERATIONS, ContractError,
                                     dispatch, make_request, response)

GEN = "a" * 32
OTHER = "b" * 32
WINDOW = "{2a63a414-1509-460a-bff9-b7c1103ba8d5}"
REF = GEN + ":" + WINDOW
APP = GEN + ":app-1"
VALID = {
    "doctor": [], "session.start": [], "session.status": [], "session.stop": [],
    "launch": ["--", "/not/executed", "--help"], "windows": [],
    "focus": ["--window", REF], "wait": ["--for", "window", "--app", APP],
    "key": ["--window", REF, "CTRL+A"], "type": ["--window", REF, "hello"],
    "click": ["--window", REF, "--x", "0", "--y", "2"], "input.reset": [],
    "screenshot": [], "logs": [], "close": ["--window", REF], "kill": ["--app", APP],
}


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "agent_desktop", *args],
                              env=os.environ | {"PYTHONPATH": str(SRC)},
                              capture_output=True, text=True, timeout=5)

    def json_cli(self, *args, status=5):
        result = self.run_cli("--json", *args)
        self.assertEqual(result.returncode, status, result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertRegex(payload["request_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(payload["schema_version"], 1)
        return payload

    def test_every_operation_explicitly_unwired(self):
        for operation, options in VALID.items():
            with self.subTest(operation=operation):
                result = self.json_cli(*operation.split("."), *options)
                self.assertFalse(result["ok"])
                self.assertEqual(result["operation"], operation)
                self.assertEqual(result["error"]["code"], "unsupported_operation")
                self.assertEqual(result["error"]["outcome"], "not_started")
                self.assertEqual(result["error"]["context"]["implementation_issue"], OPERATIONS[operation][2])
                self.assertEqual(result["session"], None if operation == "doctor" else {"name": "default", "generation": None})

    def test_readable_default_help_version_and_error(self):
        for options in (["--help"], ["session", "--help"], ["input", "reset", "--help"]):
            result = self.run_cli(*options)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.startswith("usage:"))
            self.assertEqual(result.stderr, "")
        version = self.run_cli("--version")
        self.assertEqual(version.stdout, "agent-desktop 0.1.0\n")
        failure = self.run_cli("session", "start")
        self.assertEqual(failure.returncode, 5)
        self.assertEqual(failure.stdout, "")
        self.assertIn("unsupported_operation", failure.stderr)

    def test_json_help_version_and_nested_mode_placement(self):
        for options in (["--help"], ["session", "--help"], ["launch", "--help"], ["input", "reset", "--help"], ["--version"]):
            self.assertTrue(self.json_cli(*options, status=0)["ok"])
        for options in (["session", "--json", "status"], ["session", "status", "--json"], ["input", "--json", "reset"]):
            result = self.run_cli(*options)
            self.assertEqual(result.returncode, 5)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "unsupported_operation")

    def test_every_session_operation_accepts_generation(self):
        for operation, options in VALID.items():
            if operation == "doctor":
                continue
            with self.subTest(operation=operation):
                result = self.json_cli(*operation.split("."), "--session", "work", "--generation", GEN, *options)
                self.assertEqual(result["session"], {"name": "work", "generation": None})
                self.assertEqual(result["error"]["context"]["expected_generation"], GEN)

    def test_generation_disagreement_fails_before_dispatch(self):
        with patch.object(cli, "dispatch") as send:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["--json", "focus", "--window", REF, "--generation", OTHER])
            self.assertEqual(code, 4)
            self.assertEqual(json.loads(out.getvalue())["error"]["code"], "generation_mismatch")
            send.assert_not_called()

    def test_invalid_arguments_and_no_abbreviation(self):
        cases = [[], ["unknown"], ["session"], ["input"], ["click"],
                 ["--version", "doctor"], ["windows", "--unknown"], ["windows", "--sess", "x"],
                 ["wait", "--for", "focus", "--app", APP],
                 ["focus", "--app", APP, "--window", REF],
                 ["doctor", "--generation", GEN], ["type", "--window", REF],
                 ["click", "--window", REF, "--x", "-1", "--y", "0"]]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.json_cli(*args, status=2)["error"]["code"], "invalid_arguments")

    def test_parser_errors_never_echo_secrets_in_either_mode(self):
        secret = "PRIVATE_SENTINEL_NEVER_ECHO_89"
        cases = [[secret], ["windows", "--unknown", secret],
                 ["windows", "--timeout", secret],
                 ["launch", "--env", secret, "--", "app"],
                 ["launch", "app", secret],
                 ["click", "--window", REF, "--x", secret, "--y", "0"]]
        for mode in ([], ["--json"]):
            for args in cases:
                with self.subTest(mode=mode, args=args):
                    result = self.run_cli(*mode, *args)
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn(secret, result.stdout + result.stderr)

    def test_launch_literal_boundary_and_exact_argv(self):
        argv = ["/absent", "--", "--json", "--help", "-2", "space here", "$(not executed)", ""]
        request, _, _, mode = cli.parse_request(["launch", "--cwd", "relative", "--env", "X=a=b", "--env", "X=last", "--", *argv], GEN, "/caller")
        self.assertFalse(mode)
        self.assertEqual(request.arguments["argv"], argv)
        self.assertEqual(request.arguments["env"], {"X": "last"})
        self.assertEqual(request.arguments["cwd"], "relative")
        self.assertEqual(request.caller_cwd, "/caller")
        for args in (["launch"], ["launch", "--"], ["launch", "--cwd", "--", "app"], ["launch", "--env", "--", "app"], ["launch", "--timeout", "--", "app"]):
            self.json_cli(*args, status=2)
        result = self.run_cli("launch", "--", "app", "--json", "--help")
        self.assertEqual(result.returncode, 5)
        self.assertEqual(result.stdout, "")

    def test_unknown_mode_is_explicitly_unsupported(self):
        self.assertEqual(self.json_cli("session", "start", "--mode", "viewer")["error"]["code"], "unsupported_operation")

    def test_internal_failure_and_interrupt_are_structured_and_safe(self):
        for exc, expected in ((RuntimeError("SECRET_EXCEPTION"), 70), (KeyboardInterrupt(), 130)):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(cli, "dispatch", side_effect=exc), redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--json", "windows"])
            self.assertEqual(code, expected)
            self.assertNotIn("SECRET_EXCEPTION", out.getvalue() + err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["error"]["outcome"], "unknown")


class ContractTests(unittest.TestCase):
    def request(self, operation, arguments, **kwargs):
        return make_request(operation, arguments=arguments, caller_cwd="/caller", **kwargs)

    def test_wrapped_and_unwrapped_kwin_identity_roundtrips(self):
        for identity in (WINDOW, WINDOW[1:-1]):
            for operation, extra in (("focus", {}), ("key", {"chord": "W"}), ("type", {"text": ""}), ("click", {"x": 0, "y": 0}), ("close", {})):
                with self.subTest(operation=operation, identity=identity):
                    request = self.request(operation, dict(window=GEN + ":" + identity, **extra))
                    self.assertEqual(request.arguments["window"], {"generation": GEN, "window_id": identity})
                    self.assertEqual(request.expected_generation, GEN)
                    again = self.request(operation, request.arguments)
                    self.assertEqual(again.arguments, request.arguments)
        for identity in (WINDOW[:-1], WINDOW[1:], "arbitrary-name"):
            with self.assertRaises(ContractError):
                self.request("focus", {"window": GEN + ":" + identity})

    def test_finite_budgets_are_pure_shared_validation(self):
        for value in ("nan", "inf", "-inf", 0, -1, "1e9999", True, [], {}, 4):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.request("session.status", {}, timeout_seconds=value)
        for value in ("0.1", 3):
            self.assertEqual(self.request("session.status", {}, timeout_seconds=value).timeout_seconds, float(value))
        for hold in (0, -1, 2.001, "nan", "inf"):
            with self.assertRaises(ContractError):
                self.request("key", {"window": REF, "chord": "W", "hold": hold})

    def test_context_partial_handles_and_unknown_outcomes_survive(self):
        partial = {"application": {"generation": GEN, "application_id": "app-1"}, "logs": ["/durable/stderr.log"]}
        for code, status in EXIT_CODES.items():
            with self.subTest(code=code):
                error = ContractError(code, "Controlled error", context={"phase": "wait"}, outcome="partial", partial_result=partial)
                payload = response(GEN, "launch", session="default", generation=GEN, error=error)
                self.assertFalse(payload["ok"])
                self.assertIsNone(payload["result"])
                self.assertEqual(payload["error"]["partial_result"], partial)
                self.assertIsInstance(status, int)
        success = response(GEN, "key", result={"dispatched": True, "application_acknowledged": False})
        self.assertTrue(success["ok"])
        self.assertIsNone(success["error"])
        self.assertFalse(success["result"]["application_acknowledged"])

    def test_request_validation_rejects_wrong_wire_shapes(self):
        for operation in (None, [], {}, 1):
            with self.assertRaises(ContractError):
                self.request(operation, {})
        for arguments in ([], {"unknown": "secret"}, {"app": {"generation": GEN, "application_id": []}}):
            with self.assertRaises(ContractError):
                self.request("windows", arguments)
        for environment in (["NO_EQUALS"], {"BAD-KEY": "value"}, {"X": None}):
            with self.assertRaises(ContractError):
                self.request("launch", {"argv": ["app"], "env": environment})
        with self.assertRaises(ContractError):
            self.request("launch", {"argv": ["app", "nul\0value"]})


if __name__ == "__main__":
    unittest.main()
