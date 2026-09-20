"""Explicit Python-only task injection. Never importable through production CLI."""
import json
import os
from pathlib import Path
import sys
import time
from agent_desktop.worker import run

EVENTS = Path(sys.argv[3])


def event(kind, request, **fields):
    with EVENTS.open("a") as stream:
        stream.write(json.dumps({"event": kind, "id": request.request_id,
                                 "operation": request.operation,
                                 "at": time.monotonic(), **fields}) + "\n")


class Task:
    cleanup_seconds = .2
    def __init__(self, request, context):
        self.request, self.context = request, context
        self.started = None
        self.cancelled = None
        self.child = None
        self.mode = request.arguments.get("text", "quick")
        self.duration = .02 if self.mode == "quick" else 2
        if request.operation in {"input.reset", "session.stop"}:
            self.duration = .08
        self.released = False

    def step(self, now):
        if self.started is None:
            self.started = now
            event("start", self.request)
            self.context.effects({"app": {"generation": self.request.expected_generation,
                                          "application_id": "retained"}})
            if self.mode == "child":
                self.child = self.context.owner.children.start([
                    sys.executable, "-c", "import os,time; os.write(1,b'x'*1000000); time.sleep(30)"])
                event("child", self.request, pid=self.child.process.pid)
        if now - self.started >= self.duration:
            event("done", self.request)
            return {"fixture": True, "desktop_ready": False}
        return None

    def request_cancel(self, reason):
        self.cancelled = time.monotonic()
        if self.child:
            self.child.abort()
        event("cancel", self.request, reason=reason)
        event("release_attempt", self.request)

    def cleanup(self, now):
        if now - self.cancelled < .04:
            return False
        if self.child and self.child.returncode is None:
            return False
        if not self.released:
            self.released = True
            event("cleanup", self.request)
        return True


def observe(record):
    with EVENTS.open("a") as stream:
        stream.write(json.dumps({"event": record["event"], "id": record["request_id"],
                                 "operation": record["operation"], "at": time.monotonic(),
                                 "outcome": record["outcome"]}) + "\n")


run(sys.argv[1], sys.argv[2], factory=Task,
    capabilities={"input.reset", "session.stop"}, observer=observe)
