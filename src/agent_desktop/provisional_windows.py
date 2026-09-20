"""Fixed M1 structured metadata validation; provisional until #35."""
import json
import math
import re
import uuid
from .provisional_input import Failure

def encoded(data):
    return "const request = " + json.dumps(data, ensure_ascii=True, allow_nan=False) + ";\n"


def window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F-]{36}|\{[0-9a-fA-F-]{36}\})", value):
        raise Failure("invalid_result", "Window UUID must be a string")
    try:
        if str(uuid.UUID(value.strip("{}"))) != value.strip("{}").lower():
            raise ValueError()
    except ValueError:
        raise Failure("invalid_result", "Invalid window UUID")
    return value


def snapshot(value, request_id):
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("request_id") != request_id:
        raise Failure("invalid_result", "Snapshot identity/schema mismatch")
    active = value.get("active_uuid")
    if active is not None:
        window_id(active)
    if "active_uuid" not in value or not isinstance(value.get("windows"), list):
        raise Failure("invalid_result", "Missing active identity/windows")
    seen = set()
    for row in value["windows"]:
        if not isinstance(row, dict) or set(row) != {"uuid", "pid", "title", "class", "client", "frame", "active"}:
            raise Failure("invalid_result", "Incomplete window metadata")
        ident = window_id(row["uuid"])
        if ident in seen:
            raise Failure("invalid_result", "Duplicate UUID")
        seen.add(ident)
        if row["pid"] is not None and (type(row["pid"]) is not int or row["pid"] <= 0):
            raise Failure("invalid_result", "Invalid reported PID")
        for key in ("title", "class"):
            if row[key] is not None and not isinstance(row[key], str):
                raise Failure("invalid_result", "Invalid text metadata")
        for key in ("client", "frame"):
            bounds = row[key]
            if bounds is None:
                continue
            if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}:
                raise Failure("invalid_result", "Invalid geometry fields")
            for name, number in bounds.items():
                if type(number) not in (int, float) or not math.isfinite(number) or (name in ("width", "height") and number <= 0):
                    raise Failure("invalid_result", "Invalid geometry value")
        if type(row["active"]) is not bool or row["active"] != (ident == active):
            raise Failure("invalid_result", "Inconsistent focus metadata")
    if active is not None and active not in seen:
        raise Failure("invalid_result", "Active UUID absent from snapshot")
    return value

