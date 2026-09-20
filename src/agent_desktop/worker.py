"""Internal foreground transport worker. No production desktop readiness yet."""
from __future__ import annotations

import argparse
import signal
import sys
import os
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from .contracts import ContractError, dispatch
from .runtime import Endpoint
from .transport import Server
from .scheduler import Scheduler, UnsupportedTask
from .children import Children


def unsupported(request, admission):
    dispatch(request)


def run(name, generation, *, handler=None, factory=UnsupportedTask, capabilities=(), observer=None,
        artifacts=None, store=None, managed=False):
    # Internal Python injection is for tests and future owners, never a CLI plugin.
    from .artifacts import Store
    from .records import Records, diagnostic
    owned_store = artifacts is not None
    if artifacts is not None and store is not None:
        raise ContractError("invalid_arguments", "Supply a store or an artifact root, not both.")
    if artifacts is not None:
        store = Store(artifacts, name, generation, create=not managed,
                      disposable=[os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")])
    if store is not None and (store.session != name or store.generation != generation):
        raise ContractError("generation_mismatch", "Artifact store identity differs from worker.")
    records = Records(store) if store is not None else None
    endpoint = server = children = None
    try:
        from gi.repository import GLib
        endpoint = Endpoint(name, generation, managed=managed)
        children = Children()
        def observe(record):
            if records is not None:
                records.observe(record)
            if observer is not None:
                observer(record)
        scheduler = Scheduler(factory=records.factory(factory) if records else factory,
                              capabilities=capabilities, observer=observe, children=children)
        def dispatch_request(request, admission):
            if records is not None:
                records.attach(request, admission)
                # Test-only raw handlers bypass scheduler admission. Gate ordinary
                # test effects, while controls still run before best-effort records.
                if handler is not None and request.operation not in {"input.reset", "session.stop"}:
                    try:
                        records._ensure(records.live[request.request_id])
                    except Exception:
                        raise ContractError("artifact_failed", "Request admission could not be preserved.") from None
            if managed and request.operation == "session.status":
                admission.complete(result={"state": "starting", "desktop_ready": False,
                                           "worker_pid": os.getpid()})
            else:
                (handler or scheduler.submit)(request, admission)
        server = Server(endpoint, GLib, dispatch_request,
                        cancel=scheduler.cancel, after_io=scheduler.tick)
        if store is not None:
            store.worker_identity(managed=managed)
            if not managed:
                store.generation_update(state="running")
    except BaseException:
        if server is not None:
            server.close()
        if children is not None:
            children.close()
        if endpoint is not None:
            endpoint.close()
        if store is not None:
            try:
                store.generation_update(state="failed", failure="session_failed", cleanup="uncertain" if managed else "complete")
            except Exception:
                diagnostic()
            if owned_store:
                store.close()
        raise
    loop = GLib.MainLoop()
    sources = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (loop.quit(), False)[1])
               for sig in (signal.SIGTERM, signal.SIGINT)]
    loop_failed = False
    try:
        with ExitStack() as output:
            if store is not None:
                # Python helper diagnostics have a durable private sink; adapters
                # receive separately allocated handles for their subprocess output.
                stream = output.enter_context(store.open_log("worker"))
                output.enter_context(redirect_stdout(stream))
                output.enter_context(redirect_stderr(stream))
            loop.run()
    except BaseException:
        loop_failed = True
        raise
    finally:
        # Fired unix signal sources remove themselves. Find before removal.
        context = GLib.MainContext.default()
        for source in sources:
            if context.find_source_by_id(source) is not None:
                GLib.source_remove(source)
        server.close()
        children.close()
        if store is not None:
            try:
                # Infrastructure loop exit is not proof of production cgroup cleanup.
                store.generation_update(state="failed" if loop_failed else "stopped",
                                        failure="session_failed" if loop_failed else None, cleanup="uncertain")
            except Exception:
                diagnostic()
            if owned_store:
                store.close()


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ContractError("invalid_arguments", "Invalid worker arguments.")
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--artifacts", required=True, help="absolute durable artifact root")
    try:
        args = parser.parse_args(argv)
        run(args.session, args.generation, artifacts=args.artifacts, managed=args.managed)
        return 0
    except ImportError:
        print("agent-desktop worker: prerequisite_missing: distribution PyGObject is required.", file=sys.stderr)
        return 3
    except ContractError as error:
        print(f"agent-desktop worker: {error.code}: {error.message}", file=sys.stderr)
        from .contracts import EXIT_CODES
        return EXIT_CODES[error.code]
    except Exception as error:
        print(f"agent-desktop worker: internal_error ({type(error).__name__}).", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
