"""Run with a fresh non-editable wheel venv exposing distribution gi.

Usage: installed-python -I installed_smoke.py /absolute/durable/run-directory
"""
import hashlib
import importlib.util
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
from agent_desktop.contracts import make_request
from agent_desktop.lifecycle import Manager

PROJECT = Path(__file__).resolve().parents[2]
assert not Path(agent_desktop.__file__).is_relative_to(PROJECT / 'src')
run_root = Path(sys.argv[1]).resolve()
run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
artifacts = run_root / 'artifacts'
receipt = {'installed_module': agent_desktop.__file__, 'cases': {}, 'desktop_ready': False}
cli = str(Path(sys.executable).with_name('agent-desktop'))
manager_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
# Read-only snapshots of relevant personal settings. No host desktop operation.
config_root = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
def host_settings():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for pattern in ('kwin*', 'kglobalshortcutsrc', 'kdeglobals')
            for path in config_root.glob(pattern) if path.is_file()}
before = host_settings()
spec = importlib.util.spec_from_file_location('private_harness', PROJECT / 'tools/private_harness.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
binary, receipt['fixture_build'] = harness.build(run_root / 'build')


def wait(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, 'Observation timed out'
        time.sleep(.02)


def vanished(identity):
    try:
        fields = Path('/proc', str(identity['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] != identity['start_ticks'] or fields[0] == 'Z'
    except FileNotFoundError:
        return True


with tempfile.TemporaryDirectory(prefix='a19-') as temporary:
    runtime = Path(temporary) / 'r'
    runtime.mkdir(mode=0o700)
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)

    def cli_call(action, name):
        process = subprocess.run([cli, '--json', 'session', action, '--session', name],
            env=env, cwd='/tmp', capture_output=True, text=True, timeout=35)
        assert not process.stderr, process.stderr
        return json.loads(process.stdout)

    def metadata(generation):
        return json.loads((runtime / 'agent-desktop/g' / generation / 'lifecycle.json').read_text())

    def manifest(generation):
        return json.loads((artifacts / 'generations' / generation / 'manifest.json').read_text())

    def stopped(generation, identities):
        data = metadata(generation)
        cgroup = Path('/sys/fs/cgroup' + data['cgroup'])
        assert not cgroup.exists() or 'populated 0' in (cgroup / 'cgroup.events').read_text()
        assert all(vanished(identity) for identity in identities)
        assert not (runtime / 'agent-desktop/g' / generation / 'desktop').exists()
        assert manifest(generation)['cleanup']['state'] == 'complete'
        for name in ('worker', 'bus', 'compositor'):
            path = artifacts / 'generations' / generation / 'logs' / (name + '.log')
            assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600

    try:
        # Default packaged worker is tested separately from the observer fixture.
        request = make_request('session.start', caller_cwd='/tmp', session='packaged',
                               arguments={'artifacts': str(artifacts)})
        start = Manager().start(request)
        assert start['ok'], start
        generation = start['session']['generation']
        desktop = runtime / 'agent-desktop/g' / generation / 'desktop'
        wait(lambda: (desktop / 'wayland-private').is_socket())
        status = cli_call('status', 'packaged')
        assert status['ok'] and status['result']['desktop_ready'] is False
        identity = manifest(generation)['process']
        stop = cli_call('stop', 'packaged')
        assert stop['ok'], stop
        stopped(generation, [identity])
        receipt['cases']['packaged_default_worker'] = {'generation': generation, 'status': status,
            'stop': stop, 'manifest': manifest(generation), 'settings_removed': True}

        for fault in ('none', 'bus'):
            name = 'probe-' + fault
            def command(data):
                return [sys.executable, '-I', str(PROJECT / 'tests/desktop_worker_fixture.py'), name,
                        data['generation'], str(artifacts), str(PROJECT), str(binary), fault]
            start = Manager(worker_command=command).start(make_request('session.start', caller_cwd='/tmp',
                     session=name, arguments={'artifacts': str(artifacts)}))
            assert start['ok'], start
            generation = start['session']['generation']
            root = artifacts / 'generations' / generation
            wait(lambda: (root / 'probe-complete').exists())
            owner = json.loads((root / 'owner.json').read_text())
            app = json.loads((root / 'application.json').read_text())
            adapter = json.loads((root / 'adapter.json').read_text())
            assert not (root / 'forbidden-launch').exists()
            identities = [manifest(generation)['process'], owner['bus'], owner['compositor']]
            if fault == 'none':
                for identity in identities:
                    assert metadata(generation)['unit'] in Path('/proc', str(identity['pid']), 'cgroup').read_text()
                private_root = Path(owner['private']['XDG_RUNTIME_DIR'])
                modes = {str(path.relative_to(private_root)): oct(stat.S_IMODE(path.lstat().st_mode))
                         for path in private_root.rglob('*')}
                assert all(not (path.lstat().st_mode & 0o077) for path in private_root.rglob('*'))
                assert not cli_call('start', name)['ok']
            else:
                wait(lambda: all(vanished(identity) for identity in identities), seconds=8)
                modes = {'observed_by_child': 'bus/Wayland 0600'}
            started = time.monotonic()
            stop = cli_call('stop', name)
            elapsed = time.monotonic() - started
            assert stop['ok'] and elapsed < 15, stop
            assert stop['result']['state'] == ('failed' if fault == 'bus' else 'stopped')
            stopped(generation, identities)
            receipt['cases'][name] = {'generation': generation, 'owner': owner, 'application': app,
                'adapter': adapter, 'private_modes': modes, 'rejected_overrides': json.loads((root / 'rejected.json').read_text()),
                'native_events': [json.loads(line) for line in (root / 'native.jsonl').read_text().splitlines()],
                'stop': stop, 'stop_seconds': elapsed, 'manifest': manifest(generation),
                'all_owned_lifetimes_exited': True, 'settings_removed': True}
    finally:
        for path in (runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
            unit = 'agent-desktop-' + path.parent.name + '.service'
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit], env=manager_env, capture_output=True, timeout=8)
            subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', unit], env=manager_env, capture_output=True, timeout=3)

after = host_settings()
assert before == after
receipt['personal_kde_config_unchanged'] = before
receipt['installed_source_sha256'] = {str(path.relative_to(Path(agent_desktop.__file__).parent)):
    hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(agent_desktop.__file__).parent.glob('*.py'))}
receipt['native_versions'] = subprocess.check_output(['/usr/bin/pacman', '-Q', 'kwin', 'dbus', 'systemd', 'wayland'], text=True).splitlines()
(run_root / 'summary.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
print(str(run_root / 'summary.json'))
