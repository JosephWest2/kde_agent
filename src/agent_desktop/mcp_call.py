"""One MCP tool call in its own process: `python -P -m agent_desktop.mcp_call REQUEST_ID`.

The MCP server (agent_desktop.mcp) starts one of these per tools/call. It reads one
JSON request spec from stdin, runs it through the CLI's own dispatcher
(cli.execute and cli.run) and writes the one public envelope to stdout, exactly as
`agent-desktop --json` would. Arguments travel on stdin, never in argv, so typed
text and launch environment values are not visible in /proc/PID/cmdline.

SIGINT is Ctrl-C: the transport client sends the correlated priority
`request.cancel` and closes its socket, so the worker releases held input. The
server sends SIGINT to cancel a call; the parent-death signal sends it if the
server itself dies. Only the first SIGINT counts: unlike a second Ctrl-C at a
terminal, a repeat here is never a person asking to skip the cancel (see
interrupt_once).
"""
from __future__ import annotations

import json
import os
import signal
import sys

MAX_SPEC = 1 << 20  # Bytes; the worker's own frame limit.
PR_SET_PDEATHSIG = 1


def parent_death_signal():
    """Ask Linux to send SIGINT when the server thread that started us exits."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGINT), 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def interrupt_once(signum, frame):
    """SIGINT raises KeyboardInterrupt once; later ones are ignored.

    The server sends one SIGINT, but when a multithreaded server is killed Linux can
    send the parent-death signal again as this process is reparented from one
    exiting server thread to the next. A second KeyboardInterrupt would abort the
    correlated priority cancel the first one started (the CLI's double Ctrl-C).
    A Python handler, unlike SIG_IGN, is reset to the default in any program this
    process starts.
    """
    signal.signal(signal.SIGINT, lambda *_: None)
    raise KeyboardInterrupt


def prepare(request_id):
    from .contracts import ContractError, make_request
    from .paths import normalize
    data = sys.stdin.buffer.read(MAX_SPEC + 1)
    try:
        if len(data) > MAX_SPEC:
            raise ValueError
        spec = json.loads(data)
        if not isinstance(spec, dict):
            raise ValueError
    except ValueError:
        raise ContractError("protocol_error", "The MCP server sent an unreadable call.") from None
    parent = spec.get("parent_pid")
    if type(parent) is int and os.getppid() != parent:
        # The server died before the death signal was armed: act as interrupted.
        raise KeyboardInterrupt
    request = make_request(spec.get("operation"), arguments=spec.get("arguments"), caller_cwd=spec.get("caller_cwd"),
                           session=spec.get("session", "default"), expected_generation=spec.get("generation"),
                           timeout_seconds=spec.get("timeout"), request_id=request_id)
    if request.operation == "wait" and request.arguments.get("regex"):
        # The CLI compiles a --regex before sending; so does every MCP call.
        from .title_regex import compile_check
        compile_check(request.arguments["match"])
    return normalize(request), None, request.operation


def main(argv=None):
    import uuid
    from .cli import execute
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGINT, interrupt_once)
    parent_death_signal()
    request_id = argv[0] if len(argv) == 1 and len(argv[0]) == 32 else uuid.uuid4().hex
    payload, status = execute(request_id, lambda: prepare(request_id))
    # Nothing is left to cancel. Python restores the default SIGINT action while it
    # shuts down, which would let a late repeat kill the process; SIG_IGN is kept.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
