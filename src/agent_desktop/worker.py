"""Generation-owned GLib worker with provisional M1 readiness and live health."""
from __future__ import annotations

import argparse
import signal
import sys
import os
import time
import math
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from .contracts import ContractError, dispatch
from .runtime import Endpoint
from .transport import Server
from .scheduler import Scheduler, UnsupportedTask
from .children import Children


def unsupported(request, admission):
    dispatch(request)


def run(name, generation, *, handler=None, factory=UnsupportedTask, capabilities=(), observer=None,
        artifacts=None, store=None, managed=False, desktop=False, desktop_observer=None,
        kdotool=None, startup_deadline=None, readiness_factory=None, shutdown_hooks=None):
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
    foundation = readiness = applications = None
    foundation_error = None
    shutdown = None
    stop_waiters = []
    quit_after = None
    from .watchdog import Watchdog
    watchdog = Watchdog()
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
        if desktop:
            if not managed or store is None:
                raise ContractError('invalid_arguments', 'Private desktop requires an owned service and artifacts.')
            from .desktop import Desktop
            foundation = Desktop(endpoint.path.parent, children, store)
            if startup_deadline is not None:
                foundation.deadline = min(foundation.deadline, startup_deadline)
            if factory is UnsupportedTask:
                from .app_processes import Registry
                from .applications import LaunchTask
                def launch_health():
                    foundation.tick()
                    if readiness is None or readiness.state != 'ready':
                        raise ContractError('session_unavailable', 'Desktop is not ready for launch.')
                    readiness.tick()
                    if readiness.state != 'ready':
                        raise ContractError('session_unavailable', 'Desktop health changed before launch.')
                def production_factory(request, context):
                    if request.operation == 'windows':
                        from .windows import WindowsTask
                        return WindowsTask(request, context, readiness.adapter, launch_health)
                    if request.operation == 'launch':
                        return LaunchTask(request, context, applications, foundation, records, healthy=launch_health)
                    return UnsupportedTask(request, context)
                scheduler.factory = records.factory(production_factory)

        def shutdown_record(value):
            if store is not None:
                from .lifecycle import atomic
                atomic(store.path / 'shutdown.json', value | {'generation': generation, 'session': name})

        def begin_stop(deadline=None):
            nonlocal shutdown
            if shutdown is None:
                from .shutdown import Shutdown
                shutdown = Shutdown(scheduler, min(time.monotonic() + 1.8, deadline or float('inf')),
                                    observe=shutdown_record, **(shutdown_hooks or {}))
            return shutdown

        def tick():
            nonlocal foundation_error, readiness, quit_after, applications
            if shutdown is not None:
                if (shutdown.phase == 'cancel' and readiness is not None
                        and getattr(readiness, 'state', None) != 'ready'
                        and hasattr(readiness, 'cleanup_query')):
                    try:
                        if not readiness.cleanup_query(shutdown.phase_end) and time.monotonic() < shutdown.phase_end:
                            children.poll()
                            return
                    except Exception:
                        if time.monotonic() < shutdown.phase_end:
                            return
                shutdown.tick()
                if shutdown.done:
                    for admission in stop_waiters:
                        if not admission.terminal:
                            admission.complete(result={'state': 'stopping', 'desktop_ready': False,
                                                       'graceful': shutdown.snapshot()})
                    if quit_after is None:
                        quit_after = time.monotonic() + .05
                    if time.monotonic() >= quit_after:
                        loop.quit()
                return
            try:
                if foundation is not None:
                    foundation.tick()
                    if readiness is None and foundation.phase == 'constructed' and (kdotool or readiness_factory):
                        from .readiness import Readiness
                        readiness = (readiness_factory or Readiness)(foundation, generation, kdotool, foundation.deadline)
                    if readiness is not None:
                        previous = readiness.state
                        readiness.tick()
                        if readiness.state == 'ready' and factory is UnsupportedTask and applications is None:
                            from .lifecycle import read_metadata
                            metadata = read_metadata(endpoint.runtime, name, generation)
                            applications = Registry(generation, metadata['cgroup'], store, children)
                            if hasattr(readiness, 'adapter'):
                                readiness.adapter.registry = applications
                        if previous != readiness.state:
                            store.generation_update(state=readiness.state)
                    if desktop_observer is not None:
                        desktop_observer(foundation)
                children.poll()
                if applications is not None:
                    applications.tick()
                scheduler.tick()
                watchdog.tick()
            except Exception as error:
                # GLib otherwise reports callback exceptions and leaves the
                # worker running. A failed owner must exit the service instead.
                foundation_error = error
                if readiness is not None and readiness.error is None:
                    try:
                        readiness.fail(error)
                    except Exception:
                        pass
                if store is not None:
                    try:
                        from .lifecycle import atomic, retained_failure
                        first = retained_failure({'generation': generation,
                            'configuration': {'artifacts': str(store.root)}})
                        if first is None:
                            atomic(store.path / 'startup-failure.json', {
                                'generation': generation, 'code': getattr(error, 'code', 'session_failed'),
                                'message': getattr(error, 'message', 'Private desktop owner failed.'),
                                'context': getattr(error, 'context', {})})
                        code = first['code'] if first is not None else getattr(error, 'code', 'session_failed')
                    except Exception:
                        code = getattr(error, 'code', 'session_failed')
                    try:
                        # Nonblocking best effort before any shutdown hook can
                        # stall. The independent finalizer also reads the earlier
                        # diagnostic if this aggregate update is unavailable.
                        store.generation_update(state='failed', failure=code, cleanup='uncertain')
                    except Exception:
                        pass
                begin_stop()
        def escalate_query_cleanup(reason):
            nonlocal foundation_error
            foundation_error = ContractError('session_failed', 'Owned operation cleanup could not be confirmed.')
            if readiness is not None:
                try:
                    readiness.fail(foundation_error)
                except Exception:
                    pass
            begin_stop()
        scheduler.escalate = escalate_query_cleanup

        def dispatch_request(request, admission):
            if managed and request.operation == 'session.stop':
                from .ownership import generation_lock, identity_record, intent
                from .lifecycle import read_metadata
                runtime = endpoint.runtime
                data = read_metadata(runtime, name, generation)
                deadline = admission.deadline - .2
                if len(stop_waiters) >= 32:
                    raise ContractError('session_unavailable', 'Shutdown waiters are full.')
                try:
                    with generation_lock(runtime, generation) as root:
                        control = identity_record(root, 'service-control.json', data)
                        if request.request_id == control.get('request_id'):
                            relay = identity_record(root, 'relay-deadline.json', data)
                            if (relay.get('request_id') != request.request_id or type(relay.get('deadline')) not in (int, float)
                                or not math.isfinite(relay['deadline'])):
                                raise ContractError('protocol_error', 'Invalid stop relay deadline.')
                            deadline = min(deadline, relay['deadline'] - .2)
                        else:
                            intent(runtime, data, 'admitted_request', request.request_id)
                except (ContractError, OSError):
                    # Missing intent never invents success; cleanup still belongs
                    # to the owner and finalizer after a valid stop admission.
                    begin_stop(deadline)
                    raise
                stop_waiters.append(admission)
                begin_stop(deadline)
                if records is not None:
                    records.attach(request, admission)
                return
            if shutdown is not None:
                raise ContractError('session_unavailable', 'Session shutdown is in progress.')
            tick()  # Current owner health is observed before any work admission.
            if foundation_error is not None:
                raise ContractError('session_unavailable', 'Essential desktop health failed.',
                                    context=getattr(foundation_error, 'context', {}))
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
                admission.complete(result=({"state": "starting", "desktop_ready": False}
                                           if readiness is None else readiness.snapshot()) | {"worker_pid": os.getpid(),
                    "supported_operations": ["launch", "windows"] if applications is not None and readiness is not None and readiness.state == "ready" else []})
            else:
                if desktop and kdotool and (readiness is None or readiness.state != 'ready'):
                    raise ContractError('session_unavailable', 'Desktop capabilities are not ready.')
                (handler or scheduler.submit)(request, admission)
        server = Server(endpoint, GLib, dispatch_request,
                        cancel=scheduler.cancel, after_io=tick)
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
    sources = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (begin_stop(), False)[1])
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
            if foundation_error is not None:
                raise foundation_error
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
        if readiness is not None:
            readiness.close()
        children.close()
        if applications is not None:
            applications.close()
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
    parser.add_argument("--kdotool", help=argparse.SUPPRESS)
    parser.add_argument("--startup-deadline", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--artifacts", required=True, help="absolute durable artifact root")
    try:
        args = parser.parse_args(argv)
        run(args.session, args.generation, artifacts=args.artifacts, managed=args.managed,
            desktop=args.managed, kdotool=args.kdotool, startup_deadline=args.startup_deadline)
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
