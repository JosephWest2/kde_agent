"""Owned installed-wheel lifecycle and autonomous fault matrix for #20.

Run: .local/issue20-venv/bin/python -I evidence/issue-20/installed_health.py
     /absolute/new/receipt-directory /absolute/dependency-root
All public calls are separate installed CLI processes with cwd=/ and a private
runtime namespace. SIGKILL/SIGSTOP target verified exact-generation identities.
No desktop endpoint or service belonging to another generation is addressed.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

import agent_desktop
from PIL import Image

PROJECT = Path(__file__).resolve().parents[2]
assert not Path(agent_desktop.__file__).resolve().is_relative_to(PROJECT / 'src')
ROOT = Path(sys.argv[1]).resolve()
DEPENDENCIES = Path(sys.argv[2]).resolve()
ROOT.mkdir(mode=0o700, parents=True, exist_ok=False)
ARTIFACTS = ROOT / 'artifacts'
CLI = str(Path(sys.executable).with_name('agent-desktop'))
MANAGER_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
receipt = {'schema_version': 1, 'scope': 'installed M1-provisional issue-20 evidence',
           'production_readiness': False, 'release_qualified': False, 'replacement_issue': 35,
           'installed_module': agent_desktop.__file__, 'interpreter': sys.executable,
           'caller_cwd': '/', 'dependency_root': str(DEPENDENCIES), 'cases': {},
           'source_hashes': {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted((PROJECT / 'src/agent_desktop').glob('*')) if p.is_file()},
           'installed_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(Path(agent_desktop.__file__).parent.glob('*')) if p.is_file()},
           'source_commit': subprocess.check_output(['/usr/bin/git', '-C', str(PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
           'source_worktree': subprocess.check_output(['/usr/bin/git', '-C', str(PROJECT), 'status', '--porcelain'], text=True).splitlines()}


def save():
    path = ROOT / 'health.json'
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    path.chmod(0o600)


def manager(*args):
    result = subprocess.run(['/usr/bin/systemctl', '--user', '--no-ask-password', *args], env=MANAGER_ENV,
                            cwd='/', capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise AssertionError({'systemctl': args, 'stderr': result.stderr})
    return result.stdout


def properties(data):
    fields = ('MainPID', 'ControlGroup', 'ActiveState', 'SubState', 'Result', 'WatchdogUSec',
              'WatchdogSignal', 'TimeoutAbortUSec', 'TimeoutStopUSec', 'KillMode', 'Restart',
              'NotifyAccess', 'SendSIGKILL', 'FinalKillSignal')
    return dict(line.split('=', 1) for line in manager('show', data['unit'],
                *[arg for field in fields for arg in ('-p', field)]).splitlines())


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    try:
        executable = os.readlink('/proc/' + str(pid) + '/exe')
    except PermissionError:
        executable = None  # KWin capabilities can prohibit ptrace-style /proc reads.
    return {'pid': pid, 'start_ticks': fields[19], 'ppid': int(fields[1]),
            'comm': Path('/proc', str(pid), 'comm').read_text().strip(),
            'executable': executable,
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


def wait_empty(data, deadline):
    while not empty(data):
        assert time.monotonic() < deadline, 'exact owned cgroup did not become empty within 15s'
        time.sleep(.025)


def inject(data, item, signum):
    # PID reuse and generation/cgroup checks occur immediately before signaling.
    current = identity(item['pid'])
    assert current['start_ticks'] == item['start_ticks']
    assert current['cgroup'].split('::', 1)[1] == data['cgroup']
    assert data['unit'] == 'agent-desktop-' + data['generation'] + '.service'
    os.kill(item['pid'], signum)


def evidence(data, *, require_png=True):
    folder = ARTIFACTS / 'generations' / data['generation']
    files = {}
    for path in folder.rglob('*'):
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, (str(path), oct(mode))
            files[str(path.relative_to(folder))] = {'size': path.stat().st_size,
                                                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                                   'mode': oct(mode)}
    pngs = list(folder.rglob('image.png'))
    if require_png:
        assert pngs, 'readiness PNG must survive failure and cleanup'
        for path in pngs:
            with Image.open(path) as image:
                image.load()
                assert image.size == (1280, 720) and image.format == 'PNG'
    assert 'manifest.json' in files and 'logs/worker.log' in files
    if require_png:
        assert all('logs/' + name + '.log' in files for name in ('bus', 'compositor'))
    result = {'files': files, 'complete_pngs': len(pngs),
              'manifest': json.loads((folder / 'manifest.json').read_text())}
    for name in ('startup-failure.json', 'provisional/health.json'):
        path = folder / name
        if path.exists():
            result[name] = json.loads(path.read_text())
    return result


with tempfile.TemporaryDirectory(prefix='a20-health-') as tmp:
    runtime = Path(tmp)
    runtime.chmod(0o700)
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': str(runtime)}
    receipt['runtime_root'] = str(runtime)

    def cli(action, name=None, generation=None, *, timeout=46):
        args = [CLI, '--json', *action.split()]
        if name:
            args += ['--session', name]
        if generation:
            args += ['--generation', generation]
        if action == 'session start':
            args += ['--artifacts', str(ARTIFACTS), '--dependency-root', str(DEPENDENCIES)]
        elif action == 'doctor':
            args += ['--dependency-root', str(DEPENDENCIES)]
        started = time.monotonic()
        result = subprocess.run(args, env=env, cwd='/', capture_output=True, text=True, timeout=timeout)
        output = json.loads(result.stdout)
        assert not result.stderr, result.stderr
        assert (result.returncode == 0) == output['ok']
        return {'elapsed_seconds': round(time.monotonic() - started, 6), 'exit_code': result.returncode,
                'response': output, 'argv': args}

    def metadata(generation):
        data = json.loads((runtime / 'agent-desktop/g' / generation / 'lifecycle.json').read_text())
        assert data['generation'] == generation
        assert data['unit'] == 'agent-desktop-' + generation + '.service'
        assert data['cgroup'].endswith('/app.slice/' + data['unit'])
        return data

    def baseline(name):
        start = cli('session start', name)
        assert start['response']['ok'], start
        assert start['response']['result']['state'] == 'ready', start
        assert start['response']['result']['desktop_ready'] is True, start
        generation = start['response']['session']['generation']
        data = metadata(generation)
        props = properties(data)
        pids = [int(pid) for p in Path('/sys/fs/cgroup' + data['cgroup']).rglob('cgroup.procs')
                for pid in p.read_text().splitlines()]
        items = [identity(pid) for pid in pids]
        worker = next(item for item in items if item['pid'] == int(props['MainPID']))
        bus = next(item for item in items if item['comm'] == 'dbus-daemon')
        compositor = next(item for item in items if item['comm'] == 'kwin_wayland')
        assert bus['ppid'] == worker['pid'] and compositor['ppid'] == worker['pid']
        notification = {}
        for component, item in [('worker', worker), ('bus', bus), ('compositor', compositor)]:
            try:
                values = dict(value.split(b'=', 1) for value in Path('/proc', str(item['pid']), 'environ').read_bytes().split(b'\0') if b'=' in value)
            except PermissionError:
                notification[component] = {'inspection': 'unavailable: kernel denies this /proc environ read'}
                continue
            notification[component] = {key.decode(): value.decode() for key, value in values.items()
                                       if key in (b'NOTIFY_SOCKET', b'WATCHDOG_USEC', b'WATCHDOG_PID')}
        assert notification['worker'].get('WATCHDOG_USEC') == '5000000'
        assert notification['worker'].get('WATCHDOG_PID') == str(worker['pid'])
        assert not notification['bus']
        assert not any(key in notification['compositor'] for key in ('NOTIFY_SOCKET', 'WATCHDOG_USEC', 'WATCHDOG_PID'))
        assert props['WatchdogUSec'] == '5s' and props['TimeoutAbortUSec'] == '3s'
        assert props['TimeoutStopUSec'] == '3s' and props['KillMode'] == 'control-group'
        assert props['Restart'] == 'no' and props['NotifyAccess'] == 'main'
        return data, {'start': start, 'unit_properties': props, 'identities': {'worker': worker, 'bus': bus, 'compositor': compositor},
                      'notification_environment': notification}

    try:
        receipt['doctor'] = cli('doctor')
        assert receipt['doctor']['response']['ok'], receipt['doctor']
        data, case = baseline('a20-healthy')
        receipt['cases']['healthy'] = case
        case['duplicate'] = cli('session start', data['session'], data['generation'])
        assert case['duplicate']['response']['ok']
        assert case['duplicate']['response']['session']['generation'] == data['generation']
        assert case['duplicate']['response']['result']['reused'] is True
        case['status'] = cli('session status', data['session'], data['generation'])
        assert case['status']['response']['ok'] and case['status']['response']['result']['desktop_ready']
        case['stop'] = cli('session stop', data['session'], data['generation'])
        assert case['stop']['response']['ok'] and case['stop']['response']['result']['state'] == 'stopped'
        assert empty(data)
        case['evidence'] = evidence(data)
        case['passed'] = True
        save()
        print('healthy passed', flush=True)
        for component in ('bus', 'compositor', 'worker'):
            for signum in (signal.SIGKILL, signal.SIGSTOP):
                key = component + '-' + signum.name
                data, case = baseline('a20-' + key.lower())
                receipt['cases'][key] = case
                started = time.monotonic()
                inject(data, case['identities'][component], signum)
                case['fault'] = {'component': component, 'signal': signum.name,
                                 'pid': case['identities'][component]['pid']}
                if component != 'worker':
                    # Give the owner its documented independent health round.
                    # The command proves failed admission, not initial detection.
                    time.sleep(2.2 if signum == signal.SIGSTOP else .2)
                    case['rejected_work'] = cli('windows', data['session'], data['generation'], timeout=4)
                    rejection = case['rejected_work']['response']
                    assert not rejection['ok'] and rejection['error']['code'] != 'unsupported_operation', rejection
                case['no_client_until_empty'] = component == 'worker'
                wait_empty(data, started + 15)
                case['empty_after_seconds'] = round(time.monotonic() - started, 6)
                case['all_original_pids_dead'] = all(not alive(item) for item in case['identities'].values())
                assert case['all_original_pids_dead']
                case['post_empty_unit'] = properties(data)
                case['status'] = cli('session status', data['session'], data['generation'])
                response = case['status']['response']
                assert response['ok'] and response['result']['state'] == 'failed' and response['result']['desktop_ready'] is False, response
                case['after_failure_work'] = cli('windows', data['session'], data['generation'], timeout=4)
                assert not case['after_failure_work']['response']['ok']
                case['evidence'] = evidence(data)
                case['passed'] = True
                save()
                print(key + ' passed ' + str(case['empty_after_seconds']) + 's', flush=True)

        # Explicit infrastructure fixture proves starting heartbeats independently
        # of capability duration. It does not claim desktop capability readiness.
        from agent_desktop.contracts import make_request
        from agent_desktop.lifecycle import Manager
        def fixture_command(data):
            return [sys.executable, '-I', str(PROJECT / 'tests/lifecycle_worker_fixture.py'),
                    data['session'], data['generation'], str(ARTIFACTS), '-']
        starting = Manager(worker_command=fixture_command).start(make_request('session.start',
                    caller_cwd='/', session='a20-starting-fixture', arguments={'artifacts': str(ARTIFACTS),
                                                                              'dependency_root': str(DEPENDENCIES)}))
        assert starting['ok'] and starting['result']['state'] == 'starting'
        data = metadata(starting['session']['generation'])
        case = {'start': starting, 'fixture': 'installed worker without desktop provider; test-only command injection'}
        receipt['cases']['healthy-starting'] = case
        before = properties(data)
        worker = identity(int(before['MainPID']))
        hold = time.monotonic()
        while time.monotonic() - hold < 6.1:
            assert not empty(data)
            time.sleep(.1)
        case['healthy_starting_seconds'] = round(time.monotonic() - hold, 6)
        case['unit_properties'] = properties(data)
        assert case['unit_properties']['ActiveState'] == 'active' and alive(worker)
        case['passed'] = True
        save()
        frozen = {'identity': worker, 'no_client_until_empty': True, 'signal': 'SIGSTOP'}
        receipt['cases']['frozen-starting'] = frozen
        started = time.monotonic()
        inject(data, worker, signal.SIGSTOP)
        wait_empty(data, started + 15)
        frozen['empty_after_seconds'] = round(time.monotonic() - started, 6)
        frozen['post_empty_unit'] = properties(data)
        frozen['status'] = cli('session status', data['session'], data['generation'])
        assert frozen['status']['response']['result']['state'] == 'failed'
        frozen['evidence'] = evidence(data, require_png=False)
        frozen['passed'] = True
        receipt['passed'] = True
        print('starting watchdog passed ' + str(frozen['empty_after_seconds']) + 's', flush=True)
    except BaseException as error:
        receipt['passed'] = False
        receipt['failure'] = {'type': type(error).__name__, 'detail': str(error)}
        raise
    finally:
        # Exact units from this unique runtime namespace only. Stop frozen workers
        # without SIGCONT: systemd owns the finite SIGTERM -> SIGKILL escalation.
        cleanups = []
        for path in (runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
            data = metadata(path.parent.name)
            if not empty(data):
                result = subprocess.run(['/usr/bin/systemctl', '--user', '--no-ask-password', 'stop', data['unit']],
                                        env=MANAGER_ENV, capture_output=True, text=True, timeout=10)
                cleanups.append({'unit': data['unit'], 'returncode': result.returncode, 'empty': empty(data)})
        receipt['fallback_cleanups'] = cleanups
        save()
print(str(ROOT / 'health.json'))
