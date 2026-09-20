"""Installed service stop relay and independently bounded generation finalizer."""
from __future__ import annotations

import argparse
import os
import math
from pathlib import Path
import signal
import time

from .contracts import ContractError, GENERATION, make_request
from .lifecycle import atomic, read_metadata, retained_failure
from .ownership import current, generation_lock, identity_record
from .runtime import Runtime


class CleanupTimeout(Exception):
    pass


def membership(pid):
    lines = Path('/proc', str(pid), 'cgroup').read_text().splitlines()
    return next(line[3:] for line in lines if line.startswith('0::'))


def members(cgroup):
    root = Path('/sys/fs/cgroup' + cgroup)
    return {int(pid) for group in root.rglob('cgroup.procs') for pid in group.read_text().split()}


def terminate_survivors(cgroup, deadline):
    """Never signal a raw/reusable PID or cgroup.kill (which would kill us)."""
    own = os.getpid()
    if membership(own) != cgroup:
        raise ContractError('generation_mismatch', 'Cleanup does not own the expected service cgroup.')
    observed = []
    while time.monotonic() < deadline:
        pending = members(cgroup) - {own}
        if not pending:
            return observed
        for pid in pending:
            if time.monotonic() >= deadline:
                break
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                try:
                    group = membership(pid)
                    ticks = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if group != cgroup and not group.startswith(cgroup + '/'):
                    raise ContractError('generation_mismatch', 'Cleanup member moved outside the owned cgroup.')
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                observed.append({'pid': pid, 'start_ticks': ticks, 'cgroup': group})
            finally:
                os.close(fd)
        time.sleep(.005)
    raise ContractError('session_unavailable', 'Owned descendants remain after cleanup deadline.', outcome='unknown')


def classify(data, previous, stop_intent, service_result, *, inside=False, exit_status=None):
    failed = (data['state'] == 'failed' or previous.get('first_failure') is not None
              or (stop_intent is not None and stop_intent.get('prior_state') == 'failed')
              or (service_result == 'exit-code' and exit_status == '203')
              or service_result in ('watchdog', 'start-limit-hit', 'resources', 'protocol', 'exec-condition'))
    if not stop_intent and (inside or data['state'] != 'stopped'):
        failed = True
    return 'failed' if failed else 'stopped'


def finalize(runtime, data, *, service_result=None, exit_code=None, exit_status=None,
             inside=False, failed=False):
    """Outside caller proves empty/settled first; inside caller kills survivors.

    complete means ordinary processes absent and disposable files removed. An
    inside receipt excludes only this verified hook; it never claims empty cgroup.
    Caller owns the generation lock, never waits for service state while held.
    """
    from .artifacts import Store
    from .desktop import dispose
    current(runtime, data)
    root = runtime.socket_path(data['generation']).parent
    data = read_metadata(runtime, data['session'], data['generation'])
    if failed:
        data['state'] = 'failed'
    try:
        stop_intent = identity_record(root, 'stop-intent.json', data)
        if stop_intent.get('origin') not in ('manager_request', 'admitted_request'):
            raise ContractError('protocol_error', 'Invalid stop intent origin.')
    except FileNotFoundError:
        stop_intent = None
    receipt = dict(schema_version=1, session=data['session'], generation=data['generation'],
                   service_result=service_result, exit_code=exit_code, exit_status=exit_status,
                   producer='ExecStopPost' if inside else 'manager_reconciliation',
                   excluded_finalizer_pid=os.getpid() if inside else None,
                   entire_cgroup_empty=not inside, ordinary_processes_absent=False,
                   runtime_removed=False, cleanup='uncertain', stop_intent=stop_intent,
                   started_at=time.monotonic())
    store = None
    target = None
    preserved = True
    try:
        # Artifact failures cannot gate verified process termination or disposal.
        previous = {}
        try:
            store = Store(data['configuration']['artifacts'], data['session'], data['generation'])
            previous = store.read()
        except (ContractError, OSError):
            preserved = False
        if store is not None:
            target = store.path / ('terminal.json' if inside or not (store.path / 'terminal.json').exists()
                                   else 'reconciliation.json')
        early_failure = retained_failure(data)
        if early_failure is not None:
            # The owner records this before graceful hooks. It may die or block
            # there before its finally block updates the aggregate manifest.
            previous['first_failure'] = previous.get('first_failure') or early_failure['code']
            receipt['early_failure'] = early_failure
        receipt['state'] = classify(data, previous, stop_intent, service_result, inside=inside, exit_status=exit_status)
        data['state'] = receipt['state']
        atomic(root / 'lifecycle.json', data)
        def record(complete=False):
            nonlocal preserved
            if store is None:
                return
            try:
                terminal = store.path / 'terminal.json'
                # Retain raw systemd facts when later reconciliation retries.
                if not inside and terminal.exists():
                    from .ownership import read_record
                    old = read_record(store.path, 'terminal.json')
                    if old.get('generation') == data['generation']:
                        for key in ('service_result', 'exit_code', 'exit_status'):
                            receipt[key] = old.get(key)
                atomic(target, receipt)
                store.generation_update(state=data['state'],
                    failure=(previous.get('first_failure') or 'session_failed') if data['state'] == 'failed' else None,
                    cleanup='complete' if complete else 'uncertain')
            except (ContractError, OSError):
                preserved = False
        record()
        if inside:
            receipt['terminated'] = terminate_survivors(data['cgroup'], time.monotonic() + 1)
        receipt['ordinary_processes_absent'] = True
        import stat
        for priority in (False, True):
            path = runtime.socket_path(data['generation'], priority=priority)
            try:
                info = path.lstat()
                if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                    raise ContractError('session_unavailable', 'Residual control endpoint is unsafe.')
                path.unlink()
            except FileNotFoundError:
                pass
        dispose(root)
        receipt.update(runtime_removed=True, cleanup='complete', finished_at=time.monotonic())
        record(complete=True)
        return data, preserved
    except BaseException as error:
        receipt.update(cleanup='uncertain', error=type(error).__name__, finished_at=time.monotonic())
        if store is not None:
            try:
                if target is not None:
                    atomic(target, receipt)
                store.generation_update(state=data['state'], cleanup='uncertain')
            except Exception:
                pass
        if isinstance(error, OSError):
            raise ContractError('session_unavailable', 'Private settings disposal failed.',
                                context={'cleanup': 'uncertain'}, outcome='unknown') from None
        raise
    finally:
        if store is not None:
            store.close()


def relay(runtime, data):
    from .transport import exchange
    deadline = time.monotonic() + 2
    with generation_lock(runtime, data['generation']) as root:
        current(runtime, data)
        control = identity_record(root, 'service-control.json', data)
        request_id = control.get('request_id')
        if not isinstance(request_id, str) or not GENERATION.fullmatch(request_id):
            raise ContractError('protocol_error', 'Invalid service relay identity.')
        try:
            previous = identity_record(root, 'relay-deadline.json', data)
            if (previous.get('request_id') != request_id or type(previous.get('deadline')) not in (int, float)
                    or not math.isfinite(previous['deadline'])):
                raise ContractError('protocol_error', 'Invalid service relay deadline.')
            deadline = min(deadline, previous['deadline'])
        except FileNotFoundError:
            atomic(root / 'relay-deadline.json', dict(schema_version=1, session=data['session'],
                   generation=data['generation'], request_id=request_id, deadline=deadline))
    request = make_request('session.stop', session=data['session'], expected_generation=data['generation'],
                           caller_cwd='/', arguments={}, request_id=request_id, timeout_seconds=2)
    try:
        exchange(request, deadline=deadline)
    except ContractError:
        pass  # systemd independently terminates the whole owned group next.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('stop', 'post'))
    parser.add_argument('session')
    parser.add_argument('generation')
    args = parser.parse_args(argv)
    def expired(signum, frame):
        raise CleanupTimeout('Service cleanup deadline expired.')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 2.2 if args.operation == 'stop' else 2)
    try:
        runtime = Runtime()
        data = read_metadata(runtime, args.session, args.generation)
        if membership(os.getpid()) != data['cgroup']:
            raise ContractError('generation_mismatch', 'Service helper cgroup does not match generation.')
        if args.operation == 'stop':
            relay(runtime, data)
        else:
            with generation_lock(runtime, args.generation):
                finalize(runtime, data, inside=True, service_result=os.environ.get('SERVICE_RESULT'),
                         exit_code=os.environ.get('EXIT_CODE'), exit_status=os.environ.get('EXIT_STATUS'))
        return 0
    except Exception as error:
        print('agent-desktop service cleanup: ' + type(error).__name__, flush=True)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    raise SystemExit(main())
