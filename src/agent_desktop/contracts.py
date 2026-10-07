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
    "application_active": 7, "application_exited": 7, "timeout": 8,
    "input_failed": 9, "input_unavailable": 9, "input_uncertain": 9,
    "capture_failed": 10, "window_query_failed": 10, "protocol_error": 11, "transport_error": 11,
    "completion_unknown": 11, "artifact_failed": 12,
    "internal_error": 70, "cancelled": 130,
}
# operation: (default work seconds, maximum work seconds, implementation issue)
OPERATIONS = {
    "doctor": (120, 120, 63), "session.start": (30, 30, 20),
    "session.status": (3, 3, 20), "session.stop": (15, 15, 21),
    "launch": (10, 60, 22), "windows": (.5, .5, 23),
    "focus": (2, 2, 24), "wait": (10, 60, 24),
    "key": (3, 3, 64), "type": (3, 30, 64), "click": (3, 3, 66),
    "move": (3, 3, 79), "scroll": (3, 3, 79), "drag": (3, 3, 82),
    "screenshot": (3, 3, 64),
    "logs": (3, 3, 68), "close": (5, 60, 25), "kill": (5, 15, 26),
}
# Operations that are implemented end to end. Everything else returns
# unsupported_operation. --help, doctor and session status all read this.
SUPPORTED_OPERATIONS = ("doctor", "session.start", "session.status", "session.stop",
                        "launch", "windows", "focus", "wait", "key", "type", "click", "move", "scroll",
                        "drag", "screenshot", "logs", "close", "kill")
# Supported operations that need a ready desktop session.
DESKTOP_OPERATIONS = tuple(op for op in SUPPORTED_OPERATIONS
                           if op not in ("doctor", "session.start", "session.status", "session.stop"))
WAIT_CONDITIONS = ("window", "focus", "exit", "title", "gone")
LOG_SOURCES = ("all", "application", "worker", "compositor", "bus")
MAX_SCROLL_STEPS = 50  # Wheel notches per axis per scroll request.
MODIFIER_NAMES = ("ctrl", "shift", "alt")  # --modifiers on click, scroll and drag (left-hand keys).
DRAG_DURATION = (0, 2000, 300)  # drag --duration milliseconds: minimum, maximum, default.
MAX_TAIL = 200
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
    """Validate CLI strings or JSON objects; preserve KWin's native UUID spelling.

    Objects may carry the "ref" string that public output adds; it must match.
    """
    key = "application_id" if kind == "app" else "window_id"
    if isinstance(value, str):
        generation, separator, local_id = value.partition(":")
        if not separator:
            invalid(kind)
    elif isinstance(value, dict) and set(value) - {"ref"} == {"generation", key}:
        generation, local_id = value["generation"], value[key]
        if "ref" in value and value["ref"] != f"{generation}:{local_id}":
            invalid(kind)
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


def point(value, field):
    """A client-area point: "X,Y" (CLI) or [X, Y] (wire), nonnegative integers."""
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]{1,6}),([0-9]{1,6})", value)
        if match is None:
            invalid(field)
        return [int(match.group(1)), int(match.group(2))]
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int or item < 0 for item in value)):
        invalid(field)
    return list(value)


def modifier_names(value):
    """--modifiers as a list of distinct names in the order given: "ctrl,shift" or ["ctrl", "shift"]."""
    if value is None:
        return []
    names = value.split(",") if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)) or (isinstance(value, str) and not value):
        invalid("modifiers")
    result = []
    for name in names:
        if not isinstance(name, str) or name.lower() not in MODIFIER_NAMES or name.lower() in result:
            invalid("modifiers")
        result.append(name.lower())
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
    "doctor": {"dependency_root"}, "session.start": {"mode", "artifacts", "dependency_root"},
    "session.status": set(), "session.stop": set(),
    "launch": {"cwd", "env", "wait_window", "argv"},
    "windows": {"app"}, "focus": {"app", "window"},
    "wait": {"condition", "app", "window", "match", "regex"}, "key": {"window", "chord", "hold"},
    "type": {"window", "text"}, "click": {"window", "x", "y", "button", "count", "modifiers"},
    "move": {"window", "x", "y"}, "scroll": {"window", "x", "y", "dx", "dy", "modifiers"},
    "drag": {"window", "from", "to", "button", "duration", "modifiers"},
    "screenshot": {"output", "window"}, "logs": {"app", "source", "tail"},
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
    if operation in {"key", "type", "drag"} and "window" not in args:
        invalid("window")
    if operation == "kill" and "app" not in args:
        invalid("app")
    if operation == "wait":
        condition = args.get("condition")
        if condition not in WAIT_CONDITIONS:
            invalid("for")
        target = "app" if condition in ("window", "exit") else "window"
        if target not in args or ("app" if target == "window" else "window") in args:
            invalid("target")
        # --match/--regex belong to title waits only; absent and false are the defaults.
        if args.get("match") is None:
            args.pop("match", None)
        args.setdefault("regex", False)
        if type(args["regex"]) is not bool:
            invalid("regex")
        if condition == "title":
            if "match" not in args:
                invalid("match")
            # Bounded checks only: the worker runs this on its owner thread.
            # The CLI compiles a regex separately; the matching child is authoritative.
            from .title_regex import validate
            validate(args["match"])
        elif "match" in args or args["regex"]:
            invalid("match")
        else:
            del args["regex"]
    if operation == "doctor":
        args["dependency_root"] = text(args.get("dependency_root", ".local/dependencies"), "dependency-root")
    if operation == "session.start":
        mode = text(args.get("mode", "headless"), "mode")
        if mode != "headless":
            raise ContractError("unsupported_operation", "Only headless mode is defined.")
        args["dependency_root"] = text(args.get("dependency_root", ".local/dependencies"), "dependency-root")
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
    if operation in ("click", "move", "scroll"):
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
    if operation == "scroll":
        # Wheel notches; positive dy scrolls down and positive dx right, as a
        # wheel turned toward the user (or tilted right) does without natural scrolling.
        for axis in ("dx", "dy"):
            value = args.get(axis)
            value = 0 if value is None else value
            if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,3}", value):
                value = int(value)
            if type(value) is not int or not -MAX_SCROLL_STEPS <= value <= MAX_SCROLL_STEPS:
                invalid(axis)
            args[axis] = value
        if args["dx"] == args["dy"] == 0:
            raise ContractError("invalid_arguments", "Scroll needs a nonzero --dy or --dx.",
                                context={"field": "dy", "reason": "zero_scroll"})
    if operation in ("click", "drag"):
        args.setdefault("button", "left")
        if args["button"] not in ("left", "middle", "right"):
            invalid("button")
    if operation == "drag":
        for field in ("from", "to"):
            args[field] = point(args.get(field), field)
        if args["from"] == args["to"]:
            raise ContractError("invalid_arguments", "A drag needs --to different from --from.",
                                context={"field": "to", "reason": "zero_drag"})
        low, high, default = DRAG_DURATION
        duration = args.get("duration")
        duration = default if duration is None else duration
        if isinstance(duration, str) and re.fullmatch(r"[0-9]{1,4}", duration):
            duration = int(duration)
        if type(duration) is not int or not low <= duration <= high:
            invalid("duration")
        args["duration"] = duration
    if operation in ("click", "scroll", "drag"):
        # Keyboard input always goes to a checked window, as for key and type.
        modifiers = modifier_names(args.pop("modifiers", None))
        if modifiers and "window" not in args:
            raise ContractError("invalid_arguments", "--modifiers needs --window.",
                                context={"field": "modifiers", "reason": "modifiers_need_window"})
        if modifiers or operation == "drag":
            args["modifiers"] = modifiers
    if operation == "click":
        count = args.get("count", 1)
        if isinstance(count, str) and re.fullmatch(r"[1-3]", count):
            count = int(count)
        if type(count) is not int or not 1 <= count <= 3:
            invalid("count")
        args["count"] = count
    if operation == "screenshot" and args.get("output") is not None:
        args["output"] = text(args["output"], "output")
    if operation == "logs":
        args.setdefault("source", "all")
        if args["source"] not in LOG_SOURCES:
            invalid("source")
        tail = args.get("tail", 20)
        if isinstance(tail, str) and re.fullmatch(r"[0-9]{1,3}", tail):
            tail = int(tail)
        if type(tail) is not int or not 0 <= tail <= MAX_TAIL:
            invalid("tail")
        args["tail"] = tail
    caller_cwd = text(caller_cwd, "caller_cwd")
    if not caller_cwd.startswith("/"):
        invalid("caller_cwd")
    request_id = uuid.uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or not GENERATION.fullmatch(request_id):
        invalid("request_id")
    return Request(SCHEMA_VERSION, request_id, operation, session, expected_generation,
                   args, timeout, caller_cwd)


def with_refs(value):
    """Copy of a public payload where every app/window handle also has "ref".

    "ref" is the GENERATION:ID string that --app and --window accept.
    """
    if isinstance(value, list):
        return [with_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    copied = {key: with_refs(item) for key, item in value.items()}
    for key in ("application_id", "window_id"):
        if set(value) == {"generation", key} and all(isinstance(value[k], str) for k in value):
            copied["ref"] = f"{value['generation']}:{value[key]}"
    return copied


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
    raise ContractError("unsupported_operation", message, context={
        "implementation_issue": issue, "expected_generation": request.expected_generation,
    })
