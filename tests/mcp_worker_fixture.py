"""The scheduler fixture worker, also recording each correlated priority request.cancel.

Tests-only: lets the MCP tests tell the CLI's correlated cancel apart from a
plain disconnect (both cancel the task). Never importable through production code.
"""
import json
from pathlib import Path
import runpy
import sys
import time

from agent_desktop import transport

EVENTS = Path(sys.argv[3])
original = transport.Connection.dispatch


def dispatch(self, value):
    if isinstance(value, dict) and value.get("operation") == "request.cancel":
        with EVENTS.open("a") as stream:
            stream.write(json.dumps({"event": "request.cancel", "id": value.get("target_request_id"),
                                     "at": time.monotonic()}) + "\n")
    return original(self, value)


transport.Connection.dispatch = dispatch
runpy.run_path(str(Path(__file__).with_name("scheduler_worker_fixture.py")), run_name="__main__")
