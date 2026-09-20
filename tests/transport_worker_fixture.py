"""Fresh-process transport doubles. This module is not part of the package."""
import os
from pathlib import Path
import sys
from gi.repository import GLib
from agent_desktop.contracts import ContractError
from agent_desktop.protocol import MAX_FRAME
from agent_desktop.worker import run


def handle(request, admission):
    print("fixture worker output", flush=True)
    print("fixture worker diagnostic", file=sys.stderr, flush=True)
    if request.operation == "session.status":
        admission.complete(result={"state": "transport_test", "desktop_ready": False, "worker_pid": os.getpid()})
    elif request.operation == "type":
        mode = request.arguments["text"]
        if mode == "pending":
            admission.on_disconnect = lambda: Path(os.environ["FIXTURE_DISCONNECTED"]).write_text(request.request_id)
            return
        counter = Path(os.environ["FIXTURE_EFFECTS"])
        with counter.open("a") as stream:
            stream.write(request.request_id + "\n")
        if mode == "lost":
            admission.connection.close()
            admission.complete(result={"dispatched": True})
        elif mode == "large":
            admission.complete(result={"data": "x" * (MAX_FRAME + 1),
                                       "application": {"generation": request.expected_generation, "application_id": "retained"}})
        elif mode == "unencodable":
            admission.complete(result={"data": object()})
        elif mode == "partial":
            admission.complete(error=ContractError("timeout", "Fixture timeout.", outcome="partial",
                               partial_result={"app": {"generation": request.expected_generation, "application_id": "retained"}}))
        elif mode == "late":
            GLib.timeout_add(30, lambda: (admission.complete(result={"late": True}), False)[1])
        else:
            admission.complete(result={"dispatched": True, "application_acknowledged": False})
    else:
        admission.complete(error=ContractError("unsupported_operation", "Fixture operation is not implemented."))


run(sys.argv[1], sys.argv[2], handler=handle)
