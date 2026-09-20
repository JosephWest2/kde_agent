"""Generation-owned service lifecycle. Public readiness is implemented in M3.3."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .contracts import ContractError, GENERATION, make_request, response
from .protocol import decode
from .runtime import Runtime, check_directory, check_file
from .transport import exchange

STOP_SECONDS = 15.0
SYSTEMD_STOP_SECONDS = 3


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ContractError('timeout', 'Lifecycle deadline expired.', outcome='unknown')
    return value


def unit_name(generation):
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ContractError('protocol_error', 'Invalid service generation.')
    return 'agent-desktop-' + generation + '.service'


def atomic(path, value):
    temporary = path.with_name('.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_metadata(runtime, name, generation):
    path = runtime.socket_path(generation).parent
    check_directory(path)
    fd = os.open(path / 'lifecycle.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        check_file(fd)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = decode(stream.read(16385))
    finally:
        os.close(fd)
    if (set(data) != {'schema_version', 'session', 'generation', 'unit', 'cgroup', 'configuration', 'submission', 'state'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or data['session'] != name or data['generation'] != generation
            or data['unit'] != unit_name(generation)
            or not isinstance(data['cgroup'], str)
            or not data['cgroup'].startswith('/') or '..' in Path(data['cgroup']).parts
            or not data['cgroup'].endswith('/app.slice/' + data['unit'])
            or data['submission'] not in ('reserved', 'uncertain', 'acknowledged')
            or data['state'] not in ('starting', 'stopped', 'failed')):
        raise ContractError('protocol_error', 'Invalid lifecycle ownership record.')
    config = data['configuration']
    if (not isinstance(config, dict) or set(config) != {'mode', 'artifacts', 'output'}
            or config['mode'] != 'headless' or config['output'] != {'width': 1280, 'height': 720, 'scale': 1}
            or not isinstance(config['artifacts'], str) or not config['artifacts'].startswith('/')
            or os.path.normpath(config['artifacts']) != config['artifacts']):
        raise ContractError('protocol_error', 'Invalid lifecycle configuration.')
    return data


class Systemd:
    def command(self, argv, deadline):
        try:
            manager_runtime = '/run/user/' + str(os.getuid())
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': manager_runtime,
                   'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + manager_runtime + '/bus'}
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=env,
                                    stderr=subprocess.DEVNULL, timeout=remaining(deadline), check=False)
        except subprocess.TimeoutExpired:
            raise ContractError('timeout', 'Service manager call timed out.', outcome='unknown') from None
        except OSError:
            raise ContractError('prerequisite_missing', 'The user service manager is unavailable.') from None
        remaining(deadline)
        if len(result.stdout) > 65536:
            raise ContractError('protocol_error', 'Service manager reply is too large.')
        if result.returncode:
            raise ContractError('session_unavailable', 'The user service manager call failed.', outcome='unknown')
        return result.stdout.decode('utf-8', errors='strict')

    def control(self, *args):
        return ['/usr/bin/systemctl', '--user', '--no-ask-password', *args]

    def cgroup(self, unit, deadline):
        base = self.command(self.control('show', 'app.slice', '-p', 'ControlGroup', '--value'), deadline).strip()
        if not base.startswith('/') or not base.endswith('/app.slice') or '..' in Path(base).parts:
            raise ContractError('session_unavailable', 'User service cgroup is unavailable.')
        return base + '/' + unit

    def start(self, data, runtime, command, deadline):
        # A clean worker environment avoids importing the user manager's desktop
        # endpoints or credentials. Desktop supplies its children private values.
        env = ['PATH=/usr/bin:/bin', 'LANG=C.UTF-8', 'XDG_RUNTIME_DIR=' + str(runtime.root.parent)]
        log = str(Path(data['configuration']['artifacts']) / 'generations' / data['generation'] / 'logs' / 'worker.log').replace('%', '%%')
        argv = ['/usr/bin/systemd-run', '--user', '--no-ask-password', '--no-block', '--quiet',
                '--service-type=exec', '--unit=' + data['unit'], '--property=Slice=app.slice',
                '--property=Restart=no', '--property=KillMode=control-group', '--property=SendSIGKILL=yes',
                '--property=TimeoutStopSec=' + str(SYSTEMD_STOP_SECONDS) + 's', '--property=UMask=0077',
                '--property=StandardOutput=append:' + log, '--property=StandardError=append:' + log,
                '--working-directory=/', '--expand-environment=no', '--', '/usr/bin/env', '-i', *env, *command]
        self.command(argv, deadline)

    def inspect(self, data, deadline):
        output = self.command(self.control('show', data['unit'], '-p', 'LoadState', '-p', 'ActiveState',
                         '-p', 'SubState', '-p', 'Job', '-p', 'ControlGroup', '-p', 'Result', '-p', 'MainPID'), deadline)
        values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        required = {'LoadState', 'ActiveState', 'SubState', 'Job', 'ControlGroup', 'Result', 'MainPID'}
        if set(values) != required or (values['ControlGroup'] and values['ControlGroup'] != data['cgroup']):
            raise ContractError('protocol_error', 'Unexpected service ownership observation.')
        cgroup = Path('/sys/fs/cgroup' + data['cgroup'])
        try:
            events = dict(line.split() for line in (cgroup / 'cgroup.events').read_text().splitlines())
            empty = events.get('populated') == '0'
        except FileNotFoundError:
            empty = not cgroup.exists()
        except OSError:
            raise ContractError('session_unavailable', 'Owned cgroup could not be inspected.') from None
        values['empty'] = empty
        return values

    def stop(self, data, deadline):
        self.command(self.control('stop', '--no-block', data['unit']), deadline)


def settled(info):
    return (info['ActiveState'] in ('inactive', 'failed') and not info['Job'] and info['empty'])


class Manager:
    def __init__(self, *, systemd=None, worker_command=None):
        self.systemd = systemd or Systemd()
        # Internal Python injection supports process fixtures, never CLI/plugin loading.
        self.worker_command = worker_command

    def _read(self, runtime, request):
        try:
            generation = runtime.read(request.session)
        except ContractError as error:
            if error.code != 'session_not_found':
                raise
            if request.expected_generation is not None and request.operation != 'session.stop':
                raise ContractError('generation_mismatch', 'Expected generation is no longer current.') from None
            return None, None
        if request.expected_generation is not None and request.expected_generation != generation:
            raise ContractError('generation_mismatch', 'Expected generation differs from current routing.',
                                context={'expected_generation': request.expected_generation, 'resolved_generation': generation})
        try:
            data = read_metadata(runtime, request.session, generation)
        except FileNotFoundError:
            data = None  # Existing standalone transport worker, not a managed service.
        except OSError:
            raise ContractError('transport_error', 'Lifecycle ownership record is unsafe or unavailable.') from None
        return generation, data

    def _write(self, runtime, data):
        atomic(runtime.socket_path(data['generation']).parent / 'lifecycle.json', data)

    def _observe(self, runtime, data, deadline):
        info = self.systemd.inspect(data, deadline)
        remaining(deadline)
        if data['submission'] == 'uncertain' and info['LoadState'] != 'not-found':
            data['submission'] = 'acknowledged'
            self._write(runtime, data)
        return info

    def _quiescent(self, data, info):
        # An absent unit cannot disprove an ambiguous late submission. Preserve
        # the reservation rather than allowing a replacement to race that start.
        return data['submission'] != 'uncertain' and settled(info)

    def _records(self, data, *, failed=False):
        from .artifacts import Store
        try:
            store = Store(data['configuration']['artifacts'], data['session'], data['generation'])
            try:
                store.generation_update(state='failed' if failed else 'stopped',
                                        failure='session_failed' if failed else None, cleanup='complete')
                terminal_state = store.read()['state']
            finally:
                store.close()
            return terminal_state
        except (ContractError, OSError):
            return None

    def _retire(self, runtime, data, *, failed=False):
        # Caller owns the name lock. Never remove a replacement pointer or claim.
        if runtime.read(data['session']) != data['generation']:
            raise ContractError('generation_mismatch', 'Cleanup generation is no longer current.')
        data['state'] = 'failed' if failed or data['state'] == 'failed' else 'stopped'
        self._write(runtime, data)
        # Service quiescence is established before unlinking residual sockets.
        import stat
        for priority in (False, True):
            path = runtime.socket_path(data['generation'], priority=priority)
            try:
                info = path.lstat()
                if info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass
        from .desktop import dispose
        try:
            dispose(runtime.socket_path(data['generation']).parent)
        except (OSError, ContractError):
            # Do not publish complete cleanup while disposable settings remain.
            # A later lifecycle reconciliation retries the same owned subtree.
            from .artifacts import Store
            try:
                store = Store(data['configuration']['artifacts'], data['session'], data['generation'])
                try:
                    store.generation_update(state=data['state'], cleanup='uncertain')
                finally:
                    store.close()
            except (OSError, ContractError):
                pass
            raise ContractError('session_unavailable', 'Private settings disposal failed.',
                                context={'cleanup': 'uncertain'}, outcome='unknown') from None
        terminal_state = self._records(data, failed=data['state'] == 'failed')
        if terminal_state == 'failed' and data['state'] != 'failed':
            # Artifact access happens only after fallback cleanup. Preserve any
            # earlier sticky failure in the routing outcome as well.
            data['state'] = 'failed'
            self._write(runtime, data)
        return terminal_state is not None

    def _stop(self, runtime, data, deadline, *, failed=False):
        stop_submitted = False
        while True:
            info = self._observe(runtime, data, deadline)
            # Preserve failure already observed before our requested termination.
            # A later timeout/signal caused by that termination is not evidence
            # of an earlier unexpected death.
            prior_exit = self._quiescent(data, info) and data['state'] == 'starting'
            if (not stop_submitted and data['state'] == 'starting'
                    and (prior_exit or info['ActiveState'] == 'failed' or info['Result'] != 'success')):
                failed = True
            if self._quiescent(data, info):
                break
            if not stop_submitted and info['LoadState'] != 'not-found':
                if runtime.read(data['session']) != data['generation']:
                    raise ContractError('generation_mismatch', 'Stop generation is no longer current.')
                # An ambiguous submission can become visible during this loop.
                # Submit stop at its first observation, including pending jobs.
                # Artifact attachment never precedes this fallback call.
                self.systemd.stop(data, deadline)
                stop_submitted = True
            time.sleep(min(.02, remaining(deadline)))
        return self._retire(runtime, data, failed=failed)

    def _ping(self, request, generation, deadline):
        remaining(deadline)
        ping = make_request('session.status', arguments={}, caller_cwd=request.caller_cwd,
                            session=request.session, expected_generation=generation,
                            timeout_seconds=min(3, remaining(deadline)))
        try:
            payload = exchange(ping, deadline=deadline)
        except ContractError as error:
            if error.code in ('completion_unknown', 'transport_error', 'session_unavailable'):
                raise ContractError('session_unavailable', 'Managed worker control is unavailable.') from None
            raise
        if not payload['ok']:
            raise ContractError('session_unavailable', 'Managed worker did not confirm live control.')
        return payload['result']

    def start(self, request):
        """Internal infrastructure start. Public start stays gated until #20."""
        from .artifacts import Store
        from .paths import normalize
        request = normalize(request)
        deadline = time.monotonic() + request.timeout_seconds
        runtime = Runtime(create=True)
        configuration = dict(mode=request.arguments['mode'], artifacts=request.arguments['artifacts'],
                             output=dict(width=1280, height=720, scale=1))
        with runtime.lock(request.session, deadline=deadline):
            generation, data = self._read(runtime, request)
            if generation is not None:
                if data is None:
                    raise ContractError('session_conflict', 'Name belongs to an unmanaged worker.')
                info = self._observe(runtime, data, deadline)
                if not self._quiescent(data, info):
                    if configuration != data['configuration']:
                        raise ContractError('session_conflict', 'Session configuration differs.')
                    if info['ActiveState'] != 'active':
                        raise ContractError('session_unavailable', 'Existing service is not live.')
                    self._ping(request, generation, deadline)
                    return self._result(request, generation, {'state': 'starting', 'desktop_ready': False, 'reused': True})
                if request.expected_generation is not None:
                    raise ContractError('session_unavailable', 'Expected session generation has stopped.')
                self._retire(runtime, data, failed=data['state'] != 'stopped')
            generation = uuid.uuid4().hex
            unit = unit_name(generation)
            data = dict(schema_version=1, session=request.session, generation=generation, unit=unit,
                        cgroup=self.systemd.cgroup(unit, deadline), configuration=configuration,
                        submission='reserved', state='starting')
            runtime.socket_path(generation).parent.mkdir(mode=0o700)
            store = Store(configuration['artifacts'], request.session, generation, create=True,
                          disposable=[str(runtime.root.parent)])
            store.close()
            self._write(runtime, data)
            atomic(runtime.current / (request.session + '.json'),
                   dict(schema_version=1, session=request.session, generation=generation))
            command = (self.worker_command(data) if self.worker_command else
                       [sys.executable, '-I', '-m', 'agent_desktop.worker', '--managed',
                        '--session', request.session, '--generation', generation,
                        '--artifacts', configuration['artifacts']])
            try:
                remaining(deadline)
                data['submission'] = 'uncertain'
                self._write(runtime, data)
                self.systemd.start(data, runtime, command, deadline)
                data['submission'] = 'acknowledged'
                self._write(runtime, data)
                while True:
                    info = self._observe(runtime, data, deadline)
                    if self._quiescent(data, info):
                        raise ContractError('session_failed', 'Worker service exited during startup.')
                    if info['ActiveState'] == 'active':
                        try:
                            self._ping(request, generation, min(deadline, time.monotonic() + .2))
                            return self._result(request, generation,
                                                {'state': 'starting', 'desktop_ready': False, 'reused': False})
                        except ContractError as error:
                            if error.code not in ('session_unavailable', 'completion_unknown', 'timeout', 'transport_error'):
                                raise
                    time.sleep(min(.02, remaining(deadline)))
            except BaseException as error:
                # Separate finite cleanup reserve follows the start work budget.
                cleanup, preserved = 'uncertain', False
                try:
                    preserved = self._stop(runtime, data, time.monotonic() + STOP_SECONDS, failed=True)
                    cleanup = 'complete'
                except (ContractError, OSError):
                    pass  # Ownership survives when manager completion is uncertain.
                if isinstance(error, ContractError):
                    error.context.update(resolved_generation=generation, service=data['unit'],
                                         cleanup=cleanup, records_preserved=preserved)
                    if cleanup == 'uncertain':
                        error.outcome = 'unknown'
                raise

    def _result(self, request, generation, result):
        return response(request.request_id, request.operation, session=request.session,
                        generation=generation, result=result)

    def handle(self, request):
        deadline = time.monotonic() + request.timeout_seconds
        try:
            runtime = Runtime()
        except ContractError as error:
            if error.code == 'session_not_found' and request.operation == 'session.stop':
                return self._result(request, None, {'state': 'stopped', 'desktop_ready': False})
            error.context.setdefault('expected_generation', request.expected_generation)
            raise
        with runtime.lock(request.session, deadline=deadline):
            generation, data = self._read(runtime, request)
            if generation is None:
                if request.operation == 'session.stop':
                    return self._result(request, None, {'state': 'stopped', 'desktop_ready': False})
                raise ContractError('session_not_found', 'Session does not exist.')
            pinned = replace(request, expected_generation=generation)
            if data is None:
                # Pin before leaving the lock; exchange may reject a replacement
                # but can never silently address it.
                unmanaged = pinned
            elif request.operation == 'session.stop':
                preserved = self._stop(runtime, data, deadline)
                return self._result(request, generation,
                                    {'state': data['state'], 'desktop_ready': False,
                                     'cleanup': 'complete', 'records_preserved': preserved})
            else:
                info = self._observe(runtime, data, deadline)
                if self._quiescent(data, info):
                    self._retire(runtime, data, failed=data['state'] != 'stopped')
                    return self._result(request, generation,
                                        {'state': data['state'], 'desktop_ready': False, 'cleanup': 'complete'})
                if info['ActiveState'] != 'active':
                    raise ContractError('session_unavailable', 'Managed service is not active.')
                self._ping(pinned, generation, deadline)
                return self._result(request, generation, {'state': 'starting', 'desktop_ready': False})
        return exchange(unmanaged, deadline=deadline)
