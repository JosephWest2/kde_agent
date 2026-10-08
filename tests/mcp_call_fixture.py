"""A stand-in for agent_desktop.mcp_call in protocol tests: PLAN EVENTS REQUEST_ID.

PLAN is a JSON file: {"mode": "reply" | "hold" | "silent" | "crash", "payload": {...}}.
reply prints PAYLOAD (default: an ok envelope echoing the call spec); hold waits
for SIGINT and then replies cancelled; silent exits 0 with no output; crash writes
a diagnostic and exits 70. Each step is appended to EVENTS as one JSON line.
"""
import json
from pathlib import Path
import signal
import sys
import time

plan = json.loads(Path(sys.argv[1]).read_text())
events = Path(sys.argv[2])
request_id = sys.argv[3]
spec = json.loads(sys.stdin.read())


def event(kind, **fields):
    with events.open("a") as stream:
        stream.write(json.dumps({"event": kind, "request_id": request_id, "operation": spec["operation"],
                                 "at": time.monotonic(), **fields}) + "\n")


def envelope(ok, result=None, error=None):
    return {"schema_version": 1, "request_id": request_id, "operation": spec["operation"], "ok": ok,
            "session": {"name": spec["session"], "generation": None}, "result": result if ok else None,
            "error": error}


event("start", spec=spec)
mode = plan["mode"]
if mode == "hold":
    interrupted = []
    signal.signal(signal.SIGINT, lambda *_: interrupted.append(time.monotonic()))
    until = time.monotonic() + plan.get("seconds", 10)
    while not interrupted and time.monotonic() < until:
        time.sleep(.01)
    event("sigint" if interrupted else "done")
    print(json.dumps(envelope(False, error={"code": "cancelled", "message": "Request interrupted.", "context": {},
                                            "outcome": "unknown", "partial_result": None})))
elif mode == "reply":
    print(json.dumps(plan.get("payload") or envelope(True, {"spec": spec})))
elif mode == "crash":
    print("agent-desktop internal diagnostic: RuntimeError", file=sys.stderr)
    sys.exit(70)
