"""MCP tool definitions: one tool per CLI command, JSON schemas that mirror its flags.

The schemas check JSON types, ranges and names before a call starts. Everything
else (refs, key names, text, paths, environment names) is validated by the same
contracts.make_request the CLI uses, inside the call process, so both front ends
reject the same requests with the same errors.
"""
from __future__ import annotations

from .contracts import DRAG_DURATION, LOG_SOURCES, MAX_SCROLL_STEPS, MAX_TAIL, MODIFIER_NAMES, OPERATIONS, WAIT_CONDITIONS

SESSION = {"type": "string", "description": "Session name: 1-64 letters, digits, '_' or '-' "
           "(default: the server's --session, normally \"default\")."}
GENERATION = {"type": "string", "description": "Expected session generation (32 lowercase hex characters). "
              "Omitted means the session's current generation."}
WINDOW = {"type": "string", "description": "Window ref GENERATION:UUID: the `ref` of a window handle in a result."}
APP = {"type": "string", "description": "Application ref GENERATION:ID: `result.application.ref` from launch."}
COORDINATE = {"type": "integer", "minimum": 0,
              "description": "Pixels: client-area coordinates with `window`, else screen coordinates (1280x720)."}
BUTTON = {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)."}
MODIFIERS = {"type": "array", "items": {"type": "string", "enum": list(MODIFIER_NAMES)}, "uniqueItems": True,
             "maxItems": len(MODIFIER_NAMES),
             "description": "Keys held around the pointer input, pressed in this order (needs `window`)."}
POINT = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2,
         "description": "[x, y] in client-area pixels."}
DEPENDENCY_ROOT = {"type": "string", "description": "Dependency root (default: the server's --dependency-root). "
                   "A relative path is resolved against the MCP server's working directory."}


def timeout(operation):
    default, maximum, _ = OPERATIONS[operation]
    return {"type": "number", "exclusiveMinimum": 0, "maximum": maximum,
            "description": f"Work budget in seconds (default {default:g}, at most {maximum:g})."}


READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
INPUT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

# name: (operation, title, description, properties, required, annotations)
TOOLS = {
    "doctor": ("doctor", "Check prerequisites",
               "Check the host prerequisites for a private desktop (KWin, libei, kdotool, Python bindings, the "
               "user service manager) without starting one. Failures include repair instructions.",
               {"dependency_root": DEPENDENCY_ROOT}, [], READ_ONLY),
    "session_start": ("session.start", "Start desktop session",
                      "Start (or reuse) a private headless KWin desktop session, 1280x720, and wait until window "
                      "query, input and screenshots work. Sessions persist until session_stop, also after this MCP "
                      "server exits. Returns the session generation.",
                      {"artifacts": {"type": "string", "description": "Artifact root for logs, records and screenshots "
                                     "(default: the server's --artifacts). A relative path is resolved against the MCP "
                                     "server's working directory."},
                       "dependency_root": DEPENDENCY_ROOT,
                       "mode": {"type": "string", "enum": ["headless"], "description": "Only headless is supported."}},
                      [], {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                           "openWorldHint": False}),
    "session_status": ("session.status", "Session status", "Report a session's state and health.",
                       {}, [], READ_ONLY),
    "session_stop": ("session.stop", "Stop desktop session",
                     "Stop a session: releases held input, ends its applications and desktop. Idempotent; artifacts "
                     "are kept.", {}, [], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                                           "openWorldHint": False}),
    "launch": ("launch", "Launch application",
               "Launch a program inside the private desktop (never on the user's own). No shell: argv[0] is the "
               "program, a bare name uses PATH, a path with '/' resolves against cwd. Returns "
               "result.application.ref and log paths; with wait_window also its windows.",
               {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                         "description": "Program and its arguments."},
                "cwd": {"type": "string", "description": "Application working directory (default: the MCP server's)."},
                "env": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "Extra environment variables for the application."},
                "wait_window": {"type": "boolean", "description": "Wait for the application's first window."}},
               ["argv"], {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                          "openWorldHint": False}),
    "windows": ("windows", "List windows",
                "List the desktop's windows (or one application's) with refs, titles, geometry and which is active.",
                {"app": APP}, [], READ_ONLY),
    "focus": ("focus", "Focus window",
              "Activate a window (exactly one of window or app) and wait until KWin reports it active. Needed before "
              "key and type.", {"window": WINDOW, "app": APP}, [], INPUT),
    "wait": ("wait", "Wait for condition",
             "Poll until a condition holds: window/exit take app; focus/title/gone take window; title needs match "
             "(a substring, or a Python regex with regex: true). Fails with timeout otherwise.",
             {"for": {"type": "string", "enum": list(WAIT_CONDITIONS)}, "app": APP, "window": WINDOW,
              "match": {"type": "string", "description": "Title text (1-256 characters) for title waits."},
              "regex": {"type": "boolean", "description": "Treat match as a Python re pattern."}},
             ["for"], READ_ONLY),
    "key": ("key", "Press key chord",
            "Press a key chord such as ctrl+shift+t in a focused window, held for hold seconds. Held keys are "
            "released on cancellation, timeout, focus loss or session stop.",
            {"window": WINDOW, "chord": {"type": "string", "description": "Key names joined by '+', e.g. ctrl+s."},
             "hold": {"type": "number", "exclusiveMinimum": 0, "maximum": 2,
                      "description": "Seconds to hold the chord (default 0.05, at most 2)."}},
            ["window", "chord"], INPUT),
    "type": ("type", "Type text",
             "Type literal ASCII text (newline and tab included) into a focused window, about 10ms per character.",
             {"window": WINDOW, "text": {"type": "string", "description": "Text to type; may be empty."}},
             ["window", "text"], INPUT),
    "click": ("click", "Click", "Click at a point. With window, coordinates are client-area pixels and the window "
              "must be focused; without, screen pixels.",
              {"window": WINDOW, "x": COORDINATE, "y": COORDINATE, "button": BUTTON,
               "count": {"type": "integer", "minimum": 1, "maximum": 3, "description": "1-3 (2 = double click)."},
               "modifiers": MODIFIERS}, ["x", "y"], INPUT),
    "move": ("move", "Move pointer", "Move the pointer (hover) without pressing anything.",
             {"window": WINDOW, "x": COORDINATE, "y": COORDINATE}, ["x", "y"], INPUT),
    "scroll": ("scroll", "Scroll", "Turn the wheel at a point: dy positive scrolls down, dx positive scrolls right; "
               "not both zero.",
               {"window": WINDOW, "x": COORDINATE, "y": COORDINATE,
                "dx": {"type": "integer", "minimum": -MAX_SCROLL_STEPS, "maximum": MAX_SCROLL_STEPS},
                "dy": {"type": "integer", "minimum": -MAX_SCROLL_STEPS, "maximum": MAX_SCROLL_STEPS},
                "modifiers": MODIFIERS}, ["x", "y"], INPUT),
    "drag": ("drag", "Drag", "Press a button at from, move in a straight line to to over duration ms, release.",
             {"window": WINDOW, "from": POINT, "to": POINT, "button": BUTTON,
              "duration": {"type": "integer", "minimum": DRAG_DURATION[0], "maximum": DRAG_DURATION[1],
                           "description": f"Milliseconds (default {DRAG_DURATION[2]})."},
              "modifiers": MODIFIERS}, ["window", "from", "to"], INPUT),
    "screenshot": ("screenshot", "Screenshot",
                   "Capture the screen, or a window's client area, as PNG. The image is returned as image content; "
                   "result.path is the stored PNG, result.image says whether it was included.",
                   {"window": WINDOW,
                    "output": {"type": "string", "description": "Also copy the PNG to this file or existing directory."},
                    "include_image": {"type": "boolean", "description": "Return the PNG as image content (default true)."}},
                   [], READ_ONLY),
    "logs": ("logs", "Read logs", "Return the last lines of application and session logs, with their paths.",
             {"app": APP, "source": {"type": "string", "enum": list(LOG_SOURCES)},
              "tail": {"type": "integer", "minimum": 0, "maximum": MAX_TAIL}}, [], READ_ONLY),
    "close": ("close", "Close gracefully", "Ask a window or a whole application to close, and report whether the "
              "application exited. Never escalates to a signal.", {"window": WINDOW, "app": APP}, [],
              {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}),
    "kill": ("kill", "Kill application", "Terminate an owned application (TERM, then KILL).", {"app": APP}, ["app"],
             {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}),
}

# Wrapper-only arguments; everything else maps onto contracts.ARGUMENTS.
RENAMED = {"for": "condition"}
LOCAL = {"include_image"}


def input_schema(name):
    operation, _, _, properties, required, _ = TOOLS[name]
    common = {"timeout": timeout(operation)}
    if operation != "doctor":
        common = {"session": SESSION, "generation": GENERATION} | common
    schema = {"type": "object", "properties": properties | common, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


def definitions(version):
    """The tools/list entries for a protocol version (older ones lack title and annotations)."""
    tools = []
    for name, (_, title, description, _, _, annotations) in TOOLS.items():
        tool = {"name": name, "description": description, "inputSchema": input_schema(name)}
        if version >= "2025-03-26":
            tool["annotations"] = annotations | {"title": title}
        if version >= "2025-06-18":
            tool["title"] = title
        tools.append(tool)
    return tools


class SchemaError(Exception):
    def __init__(self, field, reason):
        super().__init__(reason)
        self.field, self.reason = field, reason


TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def validate(schema, value, field):
    """Check VALUE against the small JSON Schema subset used above; return it normalized.

    Integral numbers such as 3.0 are integers, as in JSON Schema. Errors name the
    field and a reason, never the value.
    """
    kind = schema.get("type")
    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if type(value) is not int:
            raise SchemaError(field, "type")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(field, "type")
    elif kind is not None and not isinstance(value, TYPES[kind]):
        raise SchemaError(field, "type")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(field, "enum")
    if kind in ("integer", "number"):
        if ("minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]
                or "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]):
            raise SchemaError(field, "range")
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise SchemaError(field, "items")
        items = schema.get("items")
        if items is not None:
            value = [validate(items, item, f"{field}[{index}]") for index, item in enumerate(value)]
        if schema.get("uniqueItems") and len(set(map(repr, value))) != len(value):
            raise SchemaError(field, "duplicate_items")
    if kind == "object":
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        result = {}
        for key, item in value.items():
            path = key if not field else f"{field}.{key}"
            if key in properties:
                result[key] = validate(properties[key], item, path)
            elif extra is False:
                raise SchemaError(path if isinstance(key, str) and len(key) <= 64 else field or "arguments",
                                  "unknown_property")
            elif isinstance(extra, dict):
                result[key] = validate(extra, item, field or "arguments")
            else:
                result[key] = item
        for key in schema.get("required", ()):
            if key not in value:
                raise SchemaError(key if not field else f"{field}.{key}", "required")
        value = result
    return value


def call_spec(name, arguments, defaults):
    """Validated tool arguments -> (operation, call spec for agent_desktop.mcp_call, wrapper options).

    DEFAULTS holds the server's session, dependency_root and artifacts. Raises SchemaError.
    """
    operation = TOOLS[name][0]
    values = validate(input_schema(name), arguments, "")
    local = {key: values.pop(key) for key in LOCAL if key in values}
    spec = {"operation": operation, "session": values.pop("session", defaults["session"]),
            "generation": values.pop("generation", None), "timeout": values.pop("timeout", None)}
    if operation == "doctor":
        spec["session"] = "default"
    args = {RENAMED.get(key, key): value for key, value in values.items()}
    if operation in ("doctor", "session.start"):
        args.setdefault("dependency_root", defaults["dependency_root"])
    if operation == "session.start":
        args.setdefault("artifacts", defaults["artifacts"])
    spec["arguments"] = args
    return operation, spec, local
