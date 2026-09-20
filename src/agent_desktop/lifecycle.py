"""Generation-owned service lifecycle, live readiness and bounded fallback cleanup."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .contracts import ContractError, EXIT_CODES, GENERATION, make_request, response
from .protocol import decode
from .health import fresh
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
            or data['state'] not in ('starting', 'ready', 'stopping', 'stopped', 'failed')):
        raise ContractError('protocol_error', 'Invalid lifecycle ownership record.')
    config = data['configuration']
    if (not isinstance(config, dict) or set(config) not in ({'mode', 'artifacts', 'output'}, {'mode', 'artifacts', 'output', 'dependency_root'})
            or config['mode'] != 'headless' or config['output'] != {'width': 1280, 'height': 720, 'scale': 1}
            or not isinstance(config['artifacts'], str) or not config['artifacts'].startswith('/')
            or os.path.normpath(config['artifacts']) != config['artifacts']):
        raise ContractError('protocol_error', 'Invalid lifecycle configuration.')
    if 'dependency_root' in config and (not isinstance(config['dependency_root'], str)
            or not config['dependency_root'].startswith('/')
            or os.path.normpath(config['dependency_root']) != config['dependency_root']):
        raise ContractError('protocol_error', 'Invalid dependency root configuration.')
    return data


def unit_quote(value, *, expand=False):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if not expand:
        value = value.replace('$', '$$')
    return '"' + value + '"'


class Systemd:
    def __init__(self, *, helper_command=None):
        self.helper_command = helper_command or [sys.executable, '-I', '-m', 'agent_desktop.service_cleanup']

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
        helpers = []
        for operation, property_name in (('stop', 'ExecStop'), ('post', 'ExecStopPost')):
            command_words = ['/usr/bin/env', '-i', *env, *self.helper_command, operation,
                             data['session'], data['generation']]
            # Only these manager-provided service facts undergo substitution.
            command_words[2:2] = ['SERVICE_RESULT=${SERVICE_RESULT}', 'EXIT_CODE=${EXIT_CODE}',
                                  'EXIT_STATUS=${EXIT_STATUS}']
            encoded = ' '.join(unit_quote(word, expand=word.startswith(('SERVICE_RESULT=', 'EXIT_CODE=', 'EXIT_STATUS=')))
                               for word in command_words)
            helpers.append('--property=' + property_name + '=' + encoded)
        argv = ['/usr/bin/systemd-run', '--user', '--no-ask-password', '--no-block', '--quiet',
                '--service-type=exec', '--unit=' + data['unit'], '--property=Slice=app.slice',
                '--property=Restart=no', '--property=Delegate=', '--property=DelegateSubgroup=supervisor',
                '--property=KillMode=control-group', '--property=SendSIGKILL=yes',
                '--property=TimeoutStopSec=' + str(SYSTEMD_STOP_SECONDS) + 's', '--property=UMask=0077',
                '--property=WatchdogSec=5s', '--property=TimeoutAbortSec=3s',
                '--property=WatchdogSignal=SIGTERM', '--property=FinalKillSignal=SIGKILL',
                '--property=NotifyAccess=main', '--property=TimeoutStopFailureMode=kill', *helpers,
                '--property=StandardOutput=append:' + log, '--property=StandardError=append:' + log,
                '--working-directory=/', '--expand-environment=no', '--', '/usr/bin/env', '-i', '-S',
                'NOTIFY_SOCKET=${NOTIFY_SOCKET} WATCHDOG_USEC=${WATCHDOG_USEC} WATCHDOG_PID=${WATCHDOG_PID}',
                *env, *command]
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



def retained_failure(data):
    """Historical diagnostic only; never used as evidence of live readiness."""
    path = Path(data['configuration']['artifacts']) / 'generations' / data['generation'] / 'startup-failure.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            check_file(fd)
            value = decode(os.read(fd, 16385))
        finally:
            os.close(fd)
        if (value.get('generation') == data['generation'] and value.get('code') in EXIT_CODES
                and isinstance(value.get('message'), str) and isinstance(value.get('context'), dict)):
            return value
    except (OSError, ContractError, ValueError):
        pass
    return None


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
        from .ownership import write_metadata
        write_metadata(runtime, data)

    def _observe(self, runtime, data, deadline, *, persist=True):
        known_submission = data['submission']
        info = self.systemd.inspect(data, deadline)
        remaining(deadline)
        # The autonomous post hook may have finalized while inspection ran.
        data.update(read_metadata(runtime, data['session'], data['generation']))
        if (data['submission'] == 'uncertain'
                and (known_submission == 'acknowledged' or info['LoadState'] != 'not-found')):
            data['submission'] = 'acknowledged'
            if persist:
                self._write(runtime, data)
        return info

    def _quiescent(self, data, info):
        # An absent unit cannot disprove an ambiguous late submission. Preserve
        # the reservation rather than allowing a replacement to race that start.
        return data['submission'] != 'uncertain' and settled(info)

    def _retire(self, runtime, data, *, failed=False):
        # Unit quiescence was observed before entry; never wait with this lock.
        from .ownership import generation_lock
        from .service_cleanup import finalize
        with generation_lock(runtime, data['generation']):
            updated, preserved = finalize(runtime, data, failed=failed or data['state'] == 'failed')
            data.update(updated)
            return preserved

    def _stop(self, runtime, data, deadline, *, failed=False, requested=None):
        stop_submitted = False
        bookkeeping_uncertain = False
        while True:
            info = self._observe(runtime, data, deadline, persist=False)
            # Preserve failure already observed before our requested termination.
            # A later timeout/signal caused by that termination is not evidence
            # of an earlier unexpected death.
            prior_exit = self._quiescent(data, info) and data['state'] in ('starting', 'ready')
            if (not stop_submitted and data['state'] in ('starting', 'ready')
                    and (prior_exit or info['ActiveState'] == 'failed' or info['Result'] != 'success')):
                failed = True
            if self._quiescent(data, info):
                break
            if info['ActiveState'] == 'deactivating':
                stop_submitted = True  # The independent stop/post job already owns cleanup.
            if not stop_submitted and info['LoadState'] != 'not-found':
                if runtime.read(data['session']) != data['generation']:
                    raise ContractError('generation_mismatch', 'Stop generation is no longer current.')
                # An ambiguous submission can become visible during this loop.
                # Submit stop at its first observation, including pending jobs.
                # Artifact attachment never precedes this fallback call.
                try:
                    if requested is not None:
                        from .ownership import generation_lock, intent
                        with generation_lock(runtime, data['generation']):
                            # Preserve the observation preceding requested fallback.
                            if failed:
                                data['state'] = 'failed'
                            intent(runtime, data, 'manager_request', requested.request_id)
                    data['state'] = 'failed' if failed else 'stopping'
                    self._write(runtime, data)
                except (OSError, ContractError) as error:
                    if isinstance(error, ContractError) and error.code == 'generation_mismatch':
                        raise
                    # Bookkeeping cannot gate termination of an already-validated
                    # exact unit. Lost intent/records must not invent preservation.
                    bookkeeping_uncertain = True
                if runtime.read(data['session']) != data['generation']:
                    raise ContractError('generation_mismatch', 'Stop generation is no longer current.')
                self.systemd.stop(data, deadline)
                stop_submitted = True
            time.sleep(min(.02, remaining(deadline)))
        preserved = self._retire(runtime, data, failed=failed)
        return preserved and not bookkeeping_uncertain

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
        result = payload['result']
        if (not isinstance(result, dict) or result.get('state') not in ('starting', 'ready', 'stopping', 'stopped', 'failed')
                or type(result.get('desktop_ready')) is not bool):
            raise ContractError('protocol_error', 'Invalid worker health response.')
        if result['state'] == 'ready':
            observed = result.get('observed_at')
            now = time.monotonic()
            health = result.get('health', {})
            if (not isinstance(health, dict) or result.get('provider') != 'm1-provisional'
                    or result.get('release_qualified') is not False or result.get('replacement_issue') != 35
                    or result['desktop_ready'] is not True or not fresh(observed, now)
                    or any(not isinstance(health.get(key), dict) or health[key].get('state') != 'passed'
                           for key in ('bus', 'compositor', 'window_query', 'input_resumed', 'screenshot'))
                    or any(not fresh(health[key].get('observed_at'), now) for key in ('bus', 'compositor'))):
                raise ContractError('session_unavailable', 'Worker readiness observations are stale or incomplete.')
        remaining(deadline)
        if isinstance(result.get('health'), dict):
            result['health']['control'] = {'state': 'passed', 'observed_at': time.monotonic()}
        return result

    def start(self, request):
        """Wait for real capability readiness and correlated live control."""
        from .artifacts import Store
        from .paths import normalize
        request = normalize(request)
        deadline = time.monotonic() + request.timeout_seconds
        configuration = dict(mode=request.arguments['mode'], artifacts=request.arguments['artifacts'],
                             dependency_root=request.arguments['dependency_root'],
                             output=dict(width=1280, height=720, scale=1))
        prerequisite = None
        if self.worker_command is None:
            from .prerequisites import check
            try:
                prerequisite = check(configuration['dependency_root'], deadline)
            except ContractError as error:
                error.context['expected_generation'] = request.expected_generation
                raise
        runtime = Runtime(create=True)
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
                    while True:
                        result = self._ping(request, generation, deadline)
                        if result['state'] == 'ready' or self.worker_command is not None:
                            remaining(deadline)
                            return self._result(request, generation, result | {'reused': True})
                        if result['state'] != 'starting':
                            raise ContractError('session_unavailable', 'Existing generation is not ready.', context=result)
                        time.sleep(min(.02, remaining(deadline)))
                if request.expected_generation is not None:
                    raise ContractError('session_unavailable', 'Expected session generation has stopped.')
                self._retire(runtime, data, failed=data['state'] != 'stopped')
            previous = data
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
            from .ownership import generation_lock
            from contextlib import nullcontext
            with generation_lock(runtime, generation) as root:
                atomic(root / 'service-control.json', dict(schema_version=1, session=request.session,
                       generation=generation, request_id=uuid.uuid4().hex))
            # Old hooks validate current routing while holding this same lock.
            with generation_lock(runtime, previous['generation']) if previous else nullcontext():
                atomic(runtime.current / (request.session + '.json'),
                       dict(schema_version=1, session=request.session, generation=generation))
            command = (self.worker_command(data) if self.worker_command else
                       [sys.executable, '-I', '-m', 'agent_desktop.worker', '--managed',
                        '--session', request.session, '--generation', generation,
                        '--artifacts', configuration['artifacts'],
                        '--kdotool', prerequisite['kdotool']['executable'], '--startup-deadline', str(deadline)])
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
                            result = self._ping(request, generation, min(deadline, time.monotonic() + .2))
                            if result['state'] == 'ready' or self.worker_command is not None:
                                if result['state'] == 'ready':
                                    data['state'] = 'ready'
                                    self._write(runtime, data)
                                remaining(deadline)
                                return self._result(request, generation, result | {'reused': False})
                            if result['state'] == 'failed':
                                raise ContractError('session_failed', 'Capability startup failed.', context=result)
                        except ContractError as error:
                            if error.code not in ('session_unavailable', 'completion_unknown', 'timeout', 'transport_error'):
                                raise
                    time.sleep(min(.02, remaining(deadline)))
            except BaseException as error:
                if isinstance(error, ContractError):
                    failure = retained_failure(data)
                    if failure:
                        error.code, error.message = failure['code'], failure['message']
                        error.context.update(failure['context'])
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
                preserved = self._stop(runtime, data, deadline, requested=request)
                return self._result(request, generation,
                                    {'state': data['state'], 'desktop_ready': False,
                                     'cleanup': 'complete', 'records_preserved': preserved})
            else:
                info = self._observe(runtime, data, deadline)
                if self._quiescent(data, info):
                    self._retire(runtime, data, failed=data['state'] != 'stopped')
                    return self._result(request, generation,
                                        {'state': data['state'], 'desktop_ready': False, 'cleanup': 'complete',
                                         'failure': retained_failure(data) if data['state'] == 'failed' else None})
                if info['ActiveState'] != 'active':
                    raise ContractError('session_unavailable', 'Managed service is not active.',
                                        context={'state': 'failed', 'cleanup': 'pending', 'component': 'service',
                                                 'failure': retained_failure(data), 'service': data['unit']})
                try:
                    result = self._ping(pinned, generation, deadline)
                except ContractError as error:
                    error.context.update(state='failed', component='worker', cleanup='pending',
                                         resolved_generation=generation, service=data['unit'])
                    raise
                if result['state'] in ('failed', 'stopping', 'stopped'):
                    raise ContractError('session_unavailable', 'Managed generation is unavailable.', context=result)
                return self._result(request, generation, result)
        return exchange(unmanaged, deadline=deadline)
