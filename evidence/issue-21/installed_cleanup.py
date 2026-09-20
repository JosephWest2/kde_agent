"""Owned installed-wheel autonomous cleanup matrix for #21.

Run: VENV/bin/python -I evidence/issue-21/installed_cleanup.py NEW_OUTPUT DEPENDENCIES
Optional trailing case names select a focused rerun. All controller processes run
from / and import the installed wheel; only explicit Python test seams inject the
ordinary descendants, shutdown observers and blocked service helpers.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import agent_desktop

PROJECT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT / 'tests/issue21_fixture.py'
SCRIPT = Path(__file__).resolve()
assert not Path(agent_desktop.__file__).resolve().is_relative_to(PROJECT / 'src')
MANAGER_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    from agent_desktop.lifecycle import atomic
    atomic(Path(path), data)


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19], 'ppid': int(fields[1]),
            'comm': Path('/proc', str(pid), 'comm').read_text().strip(),
            'cgroup': Path('/proc', str(pid), 'cgroup').read_text().strip()}


def alive(item):
    try:
        fields = Path('/proc', str(item['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == item['start_ticks'] and fields[0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


def empty(data):
    path = Path('/sys/fs/cgroup' + data['cgroup'])
    try:
        return 'populated 0' in (path / 'cgroup.events').read_text()
    except FileNotFoundError:
        return not path.exists()


def properties(data):
    fields = ('MainPID', 'ControlGroup', 'ActiveState', 'SubState', 'Result', 'WatchdogUSec',
              'WatchdogSignal', 'TimeoutAbortUSec', 'TimeoutStopUSec', 'KillMode', 'Restart',
              'NotifyAccess', 'SendSIGKILL', 'FinalKillSignal', 'TimeoutStopFailureMode',
              'ExecStop', 'ExecStopPost')
    result = subprocess.run(['/usr/bin/systemctl', '--user', '--no-ask-password', 'show', data['unit'],
                             *[arg for field in fields for arg in ('-p', field)]],
                            env=MANAGER_ENV, cwd='/', capture_output=True, text=True, timeout=4)
    assert result.returncode == 0, result.stderr
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def direct_snapshot(runtime, artifacts, data):
    folder = Path(artifacts) / 'generations' / data['generation']
    disposable = Path(runtime) / 'agent-desktop/g' / data['generation']
    result = {'observed_at': time.monotonic(), 'cgroup_empty': empty(data),
              'settings_removed': not (disposable / 'desktop').exists(),
              'sockets_removed': all(not (disposable / item).exists()
                                     for item in ('control.sock', 'priority.sock'))}
    for item in ('terminal.json', 'reconciliation.json', 'manifest.json', 'shutdown.json', 'startup-failure.json'):
        if (folder / item).exists():
            result[item] = read(folder / item)
    if (folder / 'fixture-events.jsonl').exists():
        result['events'] = [json.loads(line) for line in (folder / 'fixture-events.jsonl').read_text().splitlines()]
    result['descendants'] = [read(folder / name) for name in ('fixture-child.json', 'fixture-grandchild.json')
                             if (folder / name).exists()]
    result['all_descendants_absent'] = all(not alive(item) for item in result['descendants'])
    return result


def controller(action, name, artifacts, dependencies, mode, generation='-'):
    from agent_desktop.contracts import ContractError, make_request
    from agent_desktop.lifecycle import Manager, Systemd
    from agent_desktop.prerequisites import check
    runtime = os.environ['XDG_RUNTIME_DIR']
    output = Path(artifacts).parent
    if action == 'stale':
        from agent_desktop.lifecycle import read_metadata
        from agent_desktop.ownership import generation_lock
        from agent_desktop.runtime import Runtime
        from agent_desktop.service_cleanup import finalize
        owner = Runtime()
        data = read_metadata(owner, name, generation)
        try:
            with generation_lock(owner, generation):
                finalize(owner, data)
        except ContractError as error:
            print(json.dumps({'ok': False, 'error': {'code': error.code}}), flush=True)
            return
        raise AssertionError('Stale finalizer was not rejected')
    kdotool = check(dependencies, time.monotonic() + 30)['kdotool']['executable'] if action == 'start' else '-'

    def command(data):
        if mode == 'failedexec':
            return ['/nonexistent/agent-desktop-issue21']
        return [sys.executable, '-I', str(FIXTURE), 'worker', data['session'], data['generation'],
                artifacts, kdotool, mode]

    class FixtureSystemd(Systemd):
        def start(self, data, runtime, original, deadline):
            save(output / ('service-submission-' + name + '.json'), {'at': time.monotonic()})
            return super().start(data, runtime, command(data), deadline)

    class ObservedManager(Manager):
        def _retire(self, runtime, data, *, failed=False):
            # Called only after the real Manager independently verified service
            # settled/empty. Read before invoking any reconciliation writes.
            save(output / ('before-reconciliation-' + name + '.json'),
                 direct_snapshot(str(runtime.root.parent), artifacts, data))
            return super()._retire(runtime, data, failed=failed)

    helper = ([sys.executable, '-I', str(FIXTURE), 'helper', mode]
              if mode in ('blocked-stop', 'blocked-post') else None)
    instance = ObservedManager(systemd=FixtureSystemd(helper_command=helper),
                               worker_command=command if mode == 'starting' else None)
    request = make_request('session.' + action, caller_cwd='/', session=name,
                           expected_generation=None if generation == '-' else generation,
                           arguments={'artifacts': artifacts, 'dependency_root': dependencies}
                           if action == 'start' else {})
    try:
        value = instance.start(request) if action == 'start' else instance.handle(request)
    except ContractError as error:
        value = {'ok': False, 'error': {'code': error.code, 'message': error.message, 'context': error.context}}
    print(json.dumps(value), flush=True)


def main(output, dependencies, selected):
    output, dependencies = Path(output).resolve(), Path(dependencies).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = output / 'artifacts'
    receipt = {'schema_version': 1, 'production_readiness': False, 'release_qualified': False,
               'replacement_issue': 35, 'installed_module': agent_desktop.__file__,
               'interpreter': sys.executable, 'caller_cwd': '/', 'cases': {},
               'source_commit': subprocess.check_output(['/usr/bin/git', '-C', str(PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
               'source_worktree': subprocess.check_output(['/usr/bin/git', '-C', str(PROJECT), 'status', '--porcelain'], text=True).splitlines(),
               'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted((PROJECT / 'src/agent_desktop').glob('*.py'))},
               'installed_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(Path(agent_desktop.__file__).parent.glob('*.py'))},
               'fixture_hashes': {str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (SCRIPT, FIXTURE)}}
    names = selected or ['normalstop', 'managerstop', 'failedstart', 'failedexec', 'bus-kill', 'bus-freeze',
                         'kwin-kill', 'kwin-freeze', 'worker-kill', 'worker-freeze',
                         'worker-freeze-lock', 'worker-freeze-generation-lock', 'disconnect', 'socketsmissing', 'frozenstarting',
                         'blocked-stop', 'blocked-post', 'blocked-release', 'prior-failure-stop',
                         'prior-failure-stop-recordfail', 'stale-replay']
    with tempfile.TemporaryDirectory(prefix='a21-') as tmp:
        runtime = Path(tmp)
        runtime.chmod(0o700)
        os.environ['XDG_RUNTIME_DIR'] = str(runtime)
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': str(runtime)}
        receipt['runtime_root'] = tmp

        def control(action, name, mode, generation='-'):
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-I', str(SCRIPT), 'controller', action, name,
                                     str(artifacts), str(dependencies), mode, generation],
                                    env=env, cwd='/', text=True, capture_output=True, timeout=62)
            assert result.returncode == 0, result.stderr
            return {'response': json.loads(result.stdout), 'elapsed_seconds': time.monotonic() - started,
                    'stderr': result.stderr}

        def metadata(name):
            generation = read(runtime / 'agent-desktop/current' / (name + '.json'))['generation']
            data = read(runtime / 'agent-desktop/g' / generation / 'lifecycle.json')
            assert data['unit'] == 'agent-desktop-' + generation + '.service'
            assert data['cgroup'].endswith('/' + data['unit'])
            return data

        def wait_for(predicate, deadline):
            while not predicate():
                assert time.monotonic() < deadline, 'Timed out waiting for evidence condition'
                time.sleep(.025)

        def inject(data, item, signum):
            fd = os.pidfd_open(item['pid'])
            try:
                current = identity(item['pid'])
                assert current['start_ticks'] == item['start_ticks']
                assert current['cgroup'].split('::', 1)[1] == data['cgroup']
                signal.pidfd_send_signal(fd, signum)
            finally:
                os.close(fd)

        def raw(data, operation):
            from agent_desktop.contracts import make_request
            from agent_desktop.protocol import encode
            from agent_desktop.runtime import Runtime
            request = make_request(operation, caller_cwd='/', session=data['session'],
                                   expected_generation=data['generation'], arguments={})
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(str(Runtime().socket_path(data['generation'], priority=operation == 'session.stop')))
            sock.sendall(encode(request.payload()))
            return sock, request

        try:
            for key in names:
                name = 'a21-' + key
                mode = ('blocked-release-recordfail' if key == 'prior-failure-stop-recordfail' else
                        'blocked-release' if key == 'prior-failure-stop' else
                        'starting' if key == 'frozenstarting' else key
                        if key in ('failedstart', 'failedexec', 'blocked-stop', 'blocked-post', 'blocked-release') else 'native')
                case = {'mode': mode, 'no_reconciliation_before_observation': True}
                receipt['cases'][key] = case
                started = time.monotonic()
                case['start'] = control('start', name, mode)
                data = metadata(name)
                case['generation'] = data['generation']
                folder = artifacts / 'generations' / data['generation']
                if key in ('failedstart', 'failedexec'):
                    assert not case['start']['response']['ok'], case['start']
                    case['autonomous'] = read(output / ('before-reconciliation-' + name + '.json'))
                    started = (next(event['at'] for event in case['autonomous']['events']
                                    if event['event'] == 'startup_failure') if key == 'failedstart' else
                               read(output / ('service-submission-' + name + '.json'))['at'])
                    case['fault_at'] = started
                    case['fault_to_observation_seconds'] = case['autonomous']['observed_at'] - started
                    assert case['fault_to_observation_seconds'] < 15
                else:
                    assert case['start']['response']['ok'], case['start']
                    wait_for(lambda: (folder / 'fixture-grandchild.json').exists(), time.monotonic() + 3)
                    props = properties(data)
                    case['unit_properties'] = props
                    assert props['WatchdogUSec'] == '5s' and props['TimeoutAbortUSec'] == '3s'
                    assert props['TimeoutStopUSec'] == '3s' and props['TimeoutStopFailureMode'] == 'kill'
                    assert props['KillMode'] == 'control-group' and props['Restart'] == 'no'
                    case['identities'] = {'worker': identity(int(props['MainPID'])),
                                          'child': read(folder / 'fixture-child.json'),
                                          'grandchild': read(folder / 'fixture-grandchild.json')}
                    assert case['identities']['grandchild']['sid'] == case['identities']['grandchild']['pid']
                    assert case['identities']['grandchild']['ppid'] == case['identities']['child']['pid']
                    for proc in Path('/sys/fs/cgroup' + data['cgroup']).rglob('cgroup.procs'):
                        for pid in proc.read_text().split():
                            try:
                                item = identity(int(pid))
                            except FileNotFoundError:
                                continue
                            if item['comm'] in ('dbus-daemon', 'kwin_wayland'):
                                case['identities']['bus' if item['comm'] == 'dbus-daemon' else 'kwin'] = item
                    if key == 'worker-freeze-generation-lock':
                        # Model a lost service-submission acknowledgment through
                        # the same installed generation serialization as writers.
                        from agent_desktop.lifecycle import atomic, read_metadata
                        from agent_desktop.ownership import generation_lock
                        from agent_desktop.runtime import Runtime
                        owner = Runtime()
                        with generation_lock(owner, data['generation']) as generation_root:
                            uncertain = read_metadata(owner, name, data['generation'])
                            assert uncertain['unit'] == data['unit'] and owner.read(name) == data['generation']
                            assert uncertain['submission'] == 'acknowledged'
                            uncertain['submission'] = 'uncertain'
                            atomic(generation_root / 'lifecycle.json', uncertain)
                            case['pre_freeze_metadata'] = uncertain
                            data = uncertain
                    sockets = []
                    if key in ('normalstop', 'disconnect', 'managerstop', 'stale-replay', 'blocked-release'):
                        action, _ = raw(data, 'windows')
                        sockets.append(action)
                        wait_for(lambda: (folder / 'fixture-events.jsonl').exists() and
                                 'action_started' in (folder / 'fixture-events.jsonl').read_text(),
                                 time.monotonic() + 2)
                    started = time.monotonic()
                    case['fault_at'] = started
                    if key in ('normalstop', 'disconnect', 'stale-replay', 'blocked-release'):
                        stop, request = raw(data, 'session.stop')
                        wait_for(lambda: (runtime / 'agent-desktop/g' / data['generation'] / 'stop-intent.json').exists(),
                                 started + 2)
                        case['admitted_stop_request_id'] = request.request_id
                        if key == 'disconnect':
                            stop.close()
                            case['disconnected_after_admission_at'] = time.monotonic()
                        else:
                            sockets.append(stop)
                    elif key == 'managerstop':
                        case['stop'] = control('stop', name, mode, data['generation'])
                    elif key in ('prior-failure-stop', 'prior-failure-stop-recordfail'):
                        (folder / 'inject-startup-failure').touch(mode=0o600)
                        wait_for(lambda: (folder / 'startup-failure.json').exists() and
                                 (folder / 'fixture-events.jsonl').exists() and
                                 'release_blocked' in (folder / 'fixture-events.jsonl').read_text(),
                                 started + 2)
                        failure = read(folder / 'startup-failure.json')
                        assert failure['generation'] == data['generation'] and failure['code'] == 'session_failed'
                        events = [json.loads(line) for line in (folder / 'fixture-events.jsonl').read_text().splitlines()]
                        blocked_at = next(event['at'] for event in events if event['event'] == 'release_blocked')
                        case['pre_stop_failure'] = {'observed_at': time.monotonic(), 'failure': failure,
                                                    'release_blocked_at': blocked_at,
                                                    'manifest': read(folder / 'manifest.json')}
                        if key == 'prior-failure-stop-recordfail':
                            assert any(event['event'] == 'early_manifest_write_failed' for event in events)
                            assert case['pre_stop_failure']['manifest']['first_failure'] is None
                            assert case['pre_stop_failure']['manifest']['state'] == 'ready'
                        case['manager_stop_submitted_at'] = time.monotonic()
                        case['stop'] = control('stop', name, mode, data['generation'])
                    elif key == 'socketsmissing' or key.startswith('blocked-'):
                        if key == 'socketsmissing':
                            for socket_name in ('control.sock', 'priority.sock'):
                                (runtime / 'agent-desktop/g' / data['generation'] / socket_name).unlink()
                        result = subprocess.run(['/usr/bin/systemctl', '--user', '--no-ask-password', '--no-block', 'stop', data['unit']],
                                                env=MANAGER_ENV, cwd='/', capture_output=True, text=True, timeout=4)
                        assert result.returncode == 0, result.stderr
                    else:
                        component = key.split('-')[0] if key != 'frozenstarting' else 'worker'
                        signum = signal.SIGKILL if key.endswith('-kill') else signal.SIGSTOP
                        if key == 'worker-freeze-lock':
                            signum = signal.SIGUSR1
                        elif key == 'worker-freeze-generation-lock':
                            signum = signal.SIGUSR2
                        inject(data, case['identities'][component], signum)
                        case['fault'] = {'component': component, 'signal': signum.name}
                        if key == 'worker-freeze-lock':
                            wait_for(lambda: (folder / 'fixture-lock-held.json').exists(), started + 2)
                            case['held_record_lock'] = read(folder / 'fixture-lock-held.json')
                        elif key == 'worker-freeze-generation-lock':
                            wait_for(lambda: (folder / 'fixture-generation-lock-held.json').exists(), started + 2)
                            case['held_generation_lock'] = read(folder / 'fixture-generation-lock-held.json')
                            case['stop'] = control('stop', name, mode, data['generation'])
                            assert case['stop']['response']['result']['records_preserved'] is False
                    transitions = []
                    previous = None
                    while not empty(data):
                        now = time.monotonic()
                        assert now - started < 15, 'Owned cgroup exceeded the 15s cleanup bound'
                        state = properties(data)
                        current = {field: state[field] for field in ('ActiveState', 'SubState', 'Result')}
                        if current != previous:
                            transitions.append({'after_seconds': now - started, **current})
                            previous = current
                        time.sleep(.025)
                    case['transitions'] = transitions
                    case['empty_after_seconds'] = time.monotonic() - started
                    assert case['empty_after_seconds'] < 15
                    for sock in sockets:
                        sock.close()
                    case['autonomous'] = (read(output / ('before-reconciliation-' + name + '.json'))
                                          if key in ('managerstop', 'prior-failure-stop', 'prior-failure-stop-recordfail',
                                                     'worker-freeze-generation-lock')
                                          else direct_snapshot(runtime, artifacts, data))
                    assert all(not alive(item) for item in case['identities'].values())
                observation = case['autonomous']
                assert observation['cgroup_empty'] and observation['all_descendants_absent'], observation
                assert len(observation['descendants']) == (0 if key == 'failedexec' else 2)
                if key == 'blocked-post':
                    assert not observation.get('terminal.json', {}).get('cleanup') == 'complete', observation
                    case['expected_limitation'] = 'Faulted finalizer leaves truthful pending cleanup until explicit reconciliation.'
                    case['reconciliation'] = control('status', name, mode, data['generation'])
                    case['after_reconciliation'] = direct_snapshot(runtime, artifacts, data)
                    assert case['after_reconciliation']['terminal.json']['cleanup'] == 'complete'
                    assert case['after_reconciliation']['manifest.json']['cleanup']['state'] == 'complete'
                    assert case['after_reconciliation']['settings_removed'] and case['after_reconciliation']['sockets_removed']
                else:
                    terminal = observation['terminal.json']
                    assert terminal['producer'] == 'ExecStopPost', terminal
                    assert terminal['cleanup'] == 'complete' and terminal['ordinary_processes_absent'], terminal
                    assert terminal['entire_cgroup_empty'] is False and terminal['excluded_finalizer_pid'] > 0
                    assert observation['settings_removed'] and observation['sockets_removed']
                    assert observation['manifest.json']['cleanup']['state'] == 'complete'
                    expected = 'stopped' if key in ('normalstop', 'managerstop', 'disconnect', 'stale-replay') else 'failed'
                    assert terminal['state'] == expected, terminal
                    case['post_entry_after_seconds'] = terminal['started_at'] - started
                    case['post_duration_seconds'] = terminal['finished_at'] - terminal['started_at']
                    if key == 'worker-freeze-generation-lock':
                        assert terminal['stop_intent'] is None
                        case['post_cleanup_metadata'] = metadata(name)
                        assert case['post_cleanup_metadata']['submission'] == 'acknowledged'
                    if key in ('prior-failure-stop', 'prior-failure-stop-recordfail'):
                        assert observation['manifest.json']['first_failure'] == 'session_failed'
                        assert terminal['stop_intent']['origin'] == 'manager_request'
                        assert case['pre_stop_failure']['release_blocked_at'] < case['manager_stop_submitted_at']
                        assert case['pre_stop_failure']['observed_at'] < terminal['started_at']
                        assert observation['startup-failure.json'] == case['pre_stop_failure']['failure']
                        assert terminal['service_result'] == 'timeout', terminal
                        assert 'normal_close_attempt' not in [event['event'] for event in observation['events']]
                    if key == 'blocked-release':
                        kinds = [event['event'] for event in observation['events']]
                        assert 'release_blocked' in kinds and 'normal_close_attempt' not in kinds
                        close = observation['shutdown.json']['stages'].get('close', {'state': 'unattempted'})
                        assert close['state'] == 'unattempted' and not close.get('confirmed', False)
                        assert terminal['service_result'] == 'watchdog'
                    if key in ('normalstop', 'managerstop', 'disconnect', 'stale-replay'):
                        events = observation['events']
                        points = {kind: next(event['at'] for event in events if event['event'] == kind)
                                  for kind in ('action_cancel', 'action_cleanup', 'release_attempt',
                                               'normal_close_attempt', 'application_termination')}
                        assert list(points.values()) == sorted(points.values()), points
                        case['hook_order'] = points
                files = {}
                for path in folder.rglob('*'):
                    if path.is_file():
                        mode_bits = path.stat().st_mode & 0o777
                        assert mode_bits & 0o077 == 0, (str(path), oct(mode_bits))
                        files[str(path.relative_to(folder))] = {
                            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'size': path.stat().st_size, 'mode': oct(mode_bits)}
                case['artifact_inventory'] = files
                pngs = list(folder.rglob('image.png'))
                if key not in ('failedstart', 'failedexec', 'frozenstarting'):
                    from PIL import Image
                    assert pngs, 'Successful native readiness must retain PNG evidence'
                    for path in pngs:
                        with Image.open(path) as image:
                            image.load()
                            assert image.format == 'PNG' and image.size == (1280, 720)
                case['retained_pngs'] = len(pngs)
                if key == 'stale-replay':
                    case['replacement_start'] = control('start', name, 'native')
                    assert case['replacement_start']['response']['ok']
                    replacement = metadata(name)
                    assert replacement['generation'] != data['generation']
                    replacement_folder = artifacts / 'generations' / replacement['generation']
                    wait_for(lambda: (replacement_folder / 'fixture-grandchild.json').exists(), time.monotonic() + 3)

                    def replacement_snapshot():
                        root = runtime / 'agent-desktop/g' / replacement['generation']
                        paths = [runtime / 'agent-desktop/current' / (name + '.json'),
                                 root / 'lifecycle.json', root / 'service-control.json']
                        paths += sorted(path for path in (root / 'desktop').rglob('*') if path.is_file())
                        state = properties(replacement)
                        return {'files': {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
                                          for path in paths},
                                'properties': {key: state[key] for key in ('MainPID', 'ActiveState', 'SubState', 'ControlGroup')},
                                'worker': identity(int(state['MainPID'])),
                                'descendants': [read(replacement_folder / filename)
                                                for filename in ('fixture-child.json', 'fixture-grandchild.json')]}

                    case['replacement_before'] = replacement_snapshot()
                    case['stale_replay'] = control('stale', name, 'native', data['generation'])
                    assert case['stale_replay']['response']['error']['code'] == 'generation_mismatch'
                    case['replacement_after'] = replacement_snapshot()
                    assert case['replacement_before'] == case['replacement_after']
                    assert all(alive(item) for item in case['replacement_after']['descendants'])
                    stop, _ = raw(replacement, 'session.stop')
                    try:
                        wait_for(lambda: empty(replacement), time.monotonic() + 15)
                    finally:
                        stop.close()
                    case['replacement_cleanup'] = direct_snapshot(runtime, artifacts, replacement)
                    assert case['replacement_cleanup']['terminal.json']['cleanup'] == 'complete'
                case['passed'] = True
                save(output / 'cleanup.json', receipt)
                print(key + ' passed', flush=True)
            receipt['passed'] = True
        except BaseException as error:
            receipt['passed'] = False
            receipt['failure'] = {'type': type(error).__name__, 'detail': str(error)}
            raise
        finally:
            receipt['fallback_cleanups'] = []
            for path in (runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
                data = read(path)
                if not empty(data):
                    result = subprocess.run(['/usr/bin/systemctl', '--user', '--no-ask-password', 'stop', data['unit']],
                                            env=MANAGER_ENV, cwd='/', capture_output=True, text=True, timeout=16)
                    receipt['fallback_cleanups'].append({'unit': data['unit'], 'returncode': result.returncode,
                                                        'empty': empty(data)})
            save(output / 'cleanup.json', receipt)
    print(str(output / 'cleanup.json'))


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1] == 'controller':
        controller(*sys.argv[2:])
    else:
        main(sys.argv[1], sys.argv[2], sys.argv[3:])
