"""Pure request validation and versioned public responses, independent of argparse."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
import uuid

SCHEMA_VERSION = 1
EXIT_CODES = {
    "invalid_arguments": 2,
    "prerequisite_missing": 3, "prerequisite_incompatible": 3,
    "session_not_found": 4, "session_failed": 4, "session_unavailable": 4,
    "session_conflict": 4, "generation_mismatch": 4,
    "unsupported_operation": 5, "unsupported_input": 5,
    "target_not_found": 6, "target_ambiguous": 6, "target_lost": 6,
    "application_exited": 7, "timeout": 8,
    "input_failed": 9, "input_unavailable": 9, "input_uncertain": 9,
    "capture_failed": 10, "protocol_error": 11, "transport_error": 11,
    "completion_unknown": 11, "artifact_failed": 12,
    "internal_error": 70, "cancelled": 130,
}
# operation: (default work seconds, maximum work seconds, implementation issue)
OPERATIONS = {
    "doctor": (120, 120, 35), "session.start": (30, 30, 20),
    "session.status": (3, 3, 20), "session.stop": (15, 15, 21),
    "launch": (10, 60, 22), "windows": (.5, .5, 23),
    "focus": (2, 2, 24), "wait": (10, 60, 24),
    "key": (3, 3, 28), "type": (3, 3, 28), "click": (3, 3, 29),
    "input.reset": (3, 3, 31), "screenshot": (3, 3, 33),
    "logs": (3, 3, 34), "close": (5, 60, 25), "kill": (5, 15, 26),
}
GENERATION = re.compile(r"[0-9a-f]{32}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
APP_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
WINDOW_ID = re.compile(r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\})\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ContractError(Exception):
    """Messages/context are controlled public text, never arbitrary exceptions."""

    def __init__(self, code, message, *, context=None, outcome="not_started", partial_result=None):
        if code not in EXIT_CODES or outcome not in {"not_started", "partial", "unknown"}:
            raise ValueError("Invalid error contract")
        self.code = code
        self.message = message
        self.context = context or {}
        self.outcome = outcome
        self.partial_result = partial_result
        super().__init__(message)

    def payload(self):
        return dict(code=self.code, message=self.message, context=self.context,
                    outcome=self.outcome, partial_result=self.partial_result)


def invalid(field):
    raise ContractError("invalid_arguments", "Invalid or missing argument; use --help.",
                        context={"field": field})


def text(value, field, *, empty=False):
    if not isinstance(value, str) or "\0" in value or (not empty and not value):
        invalid(field)
    return value


def finite_seconds(value, field, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        invalid(field)
    try:
        number = float(value)
    except (ValueError, OverflowError):
        invalid(field)
    if not math.isfinite(number) or not 0 < number <= maximum:
        invalid(field)
    return number


def handle(value, kind):
    """Validate CLI strings or JSON objects; preserve KWin's native UUID spelling."""
    key = "application_id" if kind == "app" else "window_id"
    if isinstance(value, str):
        generation, separator, local_id = value.partition(":")
        if not separator:
            invalid(kind)
    elif isinstance(value, dict) and set(value) == {"generation", key}:
        generation, local_id = value["generation"], value[key]
    else:
        invalid(kind)
    pattern = APP_ID if kind == "app" else WINDOW_ID
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        invalid(kind)
    if not isinstance(local_id, str) or not pattern.fullmatch(local_id):
        invalid(kind)
    return {"generation": generation, key: local_id}


def environment(value):
    """Last explicit override wins; no inherited environment is inspected."""
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = []
        for item in value:
            text(item, "env")
            key, separator, val = item.partition("=")
            if not separator:
                invalid("env")
            entries.append((key, val))
    else:
        invalid("env")
    result = {}
    for key, val in entries:
        if not isinstance(key, str) or not ENV_NAME.fullmatch(key):
            invalid("env")
        result[key] = text(val, "env", empty=True)
    from .environment import check_overrides
    check_overrides(result)
    return result


@dataclass(frozen=True)
class Request:
    schema_version: int
    request_id: str
    operation: str
    session: str | None
    expected_generation: str | None
    arguments: dict
    timeout_seconds: float
    caller_cwd: str

    def payload(self):
        return asdict(self)


# Only documented fields cross the request boundary.
ARGUMENTS = {
    "doctor": {"dependency_root"}, "session.start": {"mode", "artifacts"},
    "session.status": set(), "session.stop": set(),
    "launch": {"cwd", "env", "wait_window", "argv"},
    "windows": {"app"}, "focus": {"app", "window"},
    "wait": {"condition", "app", "window"}, "key": {"window", "chord", "hold"},
    "type": {"window", "text"}, "click": {"window", "x", "y", "button"},
    "input.reset": set(), "screenshot": {"output"}, "logs": {"app", "source"},
    "close": {"app", "window"}, "kill": {"app"},
}


def make_request(operation, *, arguments, caller_cwd, session="default",
                 expected_generation=None, timeout_seconds=None, request_id=None):
    """Validate request data without looking up sessions or performing effects.

    #15 must additionally enforce framing/version and live generation ownership.
    """
    if not isinstance(operation, str) or operation not in OPERATIONS:
        invalid("operation")
    if not isinstance(arguments, dict) or set(arguments) - ARGUMENTS[operation]:
        invalid("arguments")
    args = dict(arguments)
    if operation == "doctor":
        if session not in (None, "default") or expected_generation is not None:
            invalid("session")
        session = None
    elif not isinstance(session, str) or not NAME.fullmatch(session):
        invalid("session")
    if expected_generation is not None and (not isinstance(expected_generation, str) or not GENERATION.fullmatch(expected_generation)):
        invalid("generation")
    default, maximum, _ = OPERATIONS[operation]
    timeout = finite_seconds(default if timeout_seconds is None else timeout_seconds, "timeout", maximum)
    for kind in ("app", "window"):
        if args.get(kind) is not None:
            args[kind] = handle(args[kind], kind)
            generation = args[kind]["generation"]
            if expected_generation is not None and expected_generation != generation:
                raise ContractError("generation_mismatch", "Target and expected generations disagree.")
            expected_generation = generation
        else:
            args.pop(kind, None)
    if operation in {"focus", "close"} and sum(k in args for k in ("app", "window")) != 1:
        invalid("target")
    if operation in {"key", "type", "click"} and "window" not in args:
        invalid("window")
    if operation == "kill" and "app" not in args:
        invalid("app")
    if operation == "wait":
        condition = args.get("condition")
        if condition not in ("window", "focus", "exit"):
            invalid("for")
        target = "window" if condition == "focus" else "app"
        if target not in args or ("app" if target == "window" else "window") in args:
            invalid("target")
    if operation == "doctor":
        args["dependency_root"] = text(args.get("dependency_root", ".local/dependencies"), "dependency-root")
    if operation == "session.start":
        mode = text(args.get("mode", "headless"), "mode")
        if mode != "headless":
            raise ContractError("unsupported_operation", "Only headless mode is defined.")
        args.update(mode=mode, artifacts=text(args.get("artifacts", ".agent-desktop/artifacts"), "artifacts"))
    if operation == "launch":
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv:
            invalid("argv")
        args["argv"] = [text(v, "argv", empty=i > 0) for i, v in enumerate(argv)]
        args["env"] = environment(args.get("env", []))
        if args.get("cwd") is not None:
            args["cwd"] = text(args["cwd"], "cwd")
        else:
            args["cwd"] = None
        args.setdefault("wait_window", False)
        if not isinstance(args["wait_window"], bool):
            invalid("wait-window")
    if operation == "key":
        args["chord"] = text(args.get("chord"), "chord")
        args["hold"] = finite_seconds(args.get("hold", .05), "hold", 2)
    if operation == "type":
        args["text"] = text(args.get("text"), "text", empty=True)
    if operation == "click":
        for axis in ("x", "y"):
            value = args.get(axis)
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                invalid(axis)
            if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
                invalid(axis)
            try:
                value = int(value)
            except ValueError:
                invalid(axis)
            if value < 0:
                invalid(axis)
            args[axis] = value
        args.setdefault("button", "left")
        if args["button"] not in ("left", "middle", "right"):
            invalid("button")
    if operation == "screenshot" and args.get("output") is not None:
        args["output"] = text(args["output"], "output")
    if operation == "logs":
        args.setdefault("source", "all")
        if args["source"] not in ("all", "worker", "compositor", "application"):
            invalid("source")
    caller_cwd = text(caller_cwd, "caller_cwd")
    if not caller_cwd.startswith("/"):
        invalid("caller_cwd")
    request_id = uuid.uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or not GENERATION.fullmatch(request_id):
        invalid("request_id")
    return Request(SCHEMA_VERSION, request_id, operation, session, expected_generation,
                   args, timeout, caller_cwd)


def response(request_id, operation, *, session=None, generation=None, result=None, error=None):
    return dict(schema_version=SCHEMA_VERSION, request_id=request_id, operation=operation,
                ok=error is None,
                session=None if session is None else dict(name=session, generation=generation),
                result=result if error is None else None,
                error=None if error is None else error.payload())


def dispatch(request):
    """The production implementation seam; never simulate successful desktop work."""
    issue = OPERATIONS[request.operation][2]
    message = "Operation is not implemented yet."
    if request.operation == "doctor":
        message += " For M1 prerequisites, run tools/dependencies.py report from the source checkout."
    raise ContractError("unsupported_operation", message, context={
        "implementation_issue": issue, "expected_generation": request.expected_generation,
    })
