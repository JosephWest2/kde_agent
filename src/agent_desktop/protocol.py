"""Strict, bounded JSON frames; no socket or native dependencies."""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from .contracts import (ARGUMENTS, EXIT_CODES, GENERATION, NAME, Request,
                        ContractError, make_request)

MAX_FRAME = 1024 * 1024
MAX_DEPTH = 32
WIRE_OPERATIONS = frozenset(ARGUMENTS) - {"doctor", "session.start"}
REQUEST_FIELDS = set(Request.__dataclass_fields__)
RESPONSE_FIELDS = {"schema_version", "request_id", "operation", "ok", "session", "result", "error"}


def malformed():
    raise ContractError("protocol_error", "Invalid protocol message.")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            malformed()
        result[key] = value
    return result


def _depth(value, level=0):
    if isinstance(value, float) and not math.isfinite(value):
        malformed()
    if level > MAX_DEPTH:
        malformed()
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                malformed()
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


def decode(data):
    if not 0 < len(data) <= MAX_FRAME:
        malformed()
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: malformed())
        if not isinstance(value, dict):
            malformed()
        _depth(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        malformed()


def encode(value):
    try:
        _depth(value)
        data = json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, OverflowError):
        malformed()
    if not isinstance(value, dict) or not 0 < len(data) <= MAX_FRAME:
        malformed()
    return struct.pack("!I", len(data)) + data


class Decoder:
    """One frame only. Callers cap every recv; never allocate a claimed body."""
    def __init__(self):
        self.buffer = bytearray()
        self.length = None

    def feed(self, data):
        self.buffer.extend(data)
        if self.length is None and len(self.buffer) >= 4:
            self.length = struct.unpack("!I", self.buffer[:4])[0]
            if not 0 < self.length <= MAX_FRAME:
                malformed()
        if self.length is not None:
            total = 4 + self.length
            if len(self.buffer) > total:
                malformed()
            if len(self.buffer) == total:
                return decode(bytes(self.buffer[4:]))
        return None


def request_from_wire(value):
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        malformed()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        malformed()
    for key in ("request_id", "operation", "session", "expected_generation", "caller_cwd"):
        if not isinstance(value[key], str):
            malformed()
    if value["operation"] not in WIRE_OPERATIONS:
        malformed()
    if not NAME.fullmatch(value["session"]) or not GENERATION.fullmatch(value["expected_generation"]):
        malformed()
    if type(value["timeout_seconds"]) not in (int, float):
        malformed()
    args = value["arguments"]
    if not isinstance(args, dict) or set(args) - ARGUMENTS[value["operation"]]:
        malformed()
    for key in ("app", "window"):
        if key in args and not isinstance(args[key], dict):
            malformed()
    for key in ("x", "y"):
        if key in args and type(args[key]) is not int:
            malformed()
    if "hold" in args and type(args["hold"]) not in (int, float):
        malformed()
    if "env" in args and not isinstance(args["env"], dict):
        malformed()
    from .paths import validate_wire
    validate_wire(value["operation"], value["caller_cwd"], args)
    return make_request(value["operation"], arguments=args, caller_cwd=value["caller_cwd"],
                        session=value["session"], expected_generation=value["expected_generation"],
                        timeout_seconds=value["timeout_seconds"], request_id=value["request_id"])


def validate_response(value, request, generation):
    if not isinstance(value, dict) or set(value) != RESPONSE_FIELDS:
        malformed()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        malformed()
    if (value["request_id"] != request.request_id or value["operation"] != request.operation
            or value["session"] != {"name": request.session, "generation": generation}
            or type(value["ok"]) is not bool):
        malformed()
    if value["ok"]:
        if value["error"] is not None or not isinstance(value["result"], dict):
            malformed()
    else:
        error = value["error"]
        if value["result"] is not None or not isinstance(error, dict) or set(error) != {
                "code", "message", "context", "outcome", "partial_result"}:
            malformed()
        if (not isinstance(error["code"], str) or error["code"] not in EXIT_CODES
                or not isinstance(error["message"], str) or not isinstance(error["context"], dict)
                or not isinstance(error["outcome"], str) or error["outcome"] not in {"not_started", "partial", "unknown"}
                or (error["partial_result"] is not None and not isinstance(error["partial_result"], dict))):
            malformed()
    return value


@dataclass(frozen=True)
class CancelRequest:
    request_id: str
    session: str
    expected_generation: str
    target_request_id: str
    schema_version: int = 1
    operation: str = "request.cancel"
    timeout_seconds: float = .1

    def payload(self):
        return {key: getattr(self, key) for key in (
            "schema_version", "request_id", "operation", "session",
            "expected_generation", "target_request_id")}


def cancel_from_wire(value):
    fields = {"schema_version", "request_id", "operation", "session",
              "expected_generation", "target_request_id"}
    if not isinstance(value, dict) or set(value) != fields:
        malformed()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        malformed()
    if value["operation"] != "request.cancel":
        malformed()
    for key in ("request_id", "expected_generation", "target_request_id"):
        if not isinstance(value[key], str) or not GENERATION.fullmatch(value[key]):
            malformed()
    if not isinstance(value["session"], str) or not NAME.fullmatch(value["session"]):
        malformed()
    return CancelRequest(value["request_id"], value["session"],
                         value["expected_generation"], value["target_request_id"])
