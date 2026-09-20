"""Run with a non-editable installed wheel Python exposing distribution gi."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import agent_desktop

ROOT = Path(__file__).resolve().parents[2]
assert not Path(agent_desktop.__file__).is_relative_to(ROOT / 'src'), 'Install the wheel first.'
cli = str(Path(sys.executable).with_name('agent-desktop'))
manager_env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/' + str(os.getuid()))
receipt = {'desktop_ready': False, 'installed_module': agent_desktop.__file__, 'cases': {}}
receipt['systemd'] = subprocess.check_output(['/usr/bin/systemd-run', '--version'], text=True).splitlines()[0]

with tempfile.TemporaryDirectory(prefix='ade-') as temporary:
    root = Path(temporary)
    runtime = root / 'r'
    runtime.mkdir(mode=0o700)
    artifacts = root / 'artifacts'
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
    env.pop('PYTHONPATH', None)

    def result(process):
        output, error = process.communicate(timeout=48)
        assert not error, error
        value = json.loads(output)
        return value

    def controller(operation='session.start', generation='-', *, wait=True, artifact_root=None):
        process = subprocess.Popen([sys.executable, '-I', str(ROOT / 'tests/lifecycle_controller_fixture.py'),
                  operation, 'default', str(artifact_root or artifacts), generation, '-'],
                  env=env, cwd='/', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result(process) if wait else process

    def call(action, *args):
        process = subprocess.Popen([cli, '--json', 'session', action, *args], env=env, cwd='/tmp',
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result(process)

    def manifest(generation):
        return json.loads((artifacts / 'generations' / generation / 'manifest.json').read_text())

    def vanished(identity):
        try:
            fields = Path('/proc', str(identity['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
            return fields[19] != identity['start_ticks'] or fields[0] == 'Z'
        except FileNotFoundError:
            return True

    try:
        # First prove the default production worker command loads the installed
        # package from / with no source import path or controller still running.
        code = """import json,sys
from agent_desktop.contracts import make_request
from agent_desktop.lifecycle import Manager
r=make_request('session.start',arguments={'artifacts':sys.argv[1]},caller_cwd='/',session='packaged')
print(json.dumps(Manager().start(r)))
"""
        process = subprocess.Popen([sys.executable, '-I', '-c', code, str(artifacts)], env=env, cwd='/',
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        start = result(process)
        assert start['ok'], start
        packaged_generation = start['session']['generation']
        status = call('status', '--session', 'packaged')
        assert status['ok'] and status['session']['generation'] == packaged_generation
        assert status['result'] == {'state': 'starting', 'desktop_ready': False}
        assert call('stop', '--session', 'packaged')['ok']
        receipt['cases']['packaged_worker_separate_installed_cli'] = {
            'generation': packaged_generation, 'controller_exited': process.poll() == 0,
            'manifest': manifest(packaged_generation)}

        a, b = controller(wait=False), controller(wait=False)
        first, duplicate = result(a), result(b)
        assert first['ok'] and duplicate['ok'], [first, duplicate]
        generation = first['session']['generation']
        assert duplicate['session']['generation'] == generation
        assert controller(artifact_root=root / 'different')['error']['code'] == 'session_conflict'
        assert call('status')['session']['generation'] == generation
        data = manifest(generation)
        child_path = artifacts / 'generations' / generation / 'fixture-children.json'
        deadline = time.monotonic() + 2
        while not child_path.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        identities = [data['process'], *json.loads(child_path.read_text())]
        unit = 'agent-desktop-' + generation + '.service'
        for identity in identities:
            assert unit in Path('/proc', str(identity['pid']), 'cgroup').read_text()
        properties = subprocess.check_output(['/usr/bin/systemctl', '--user', 'show', unit,
            '-p', 'KillMode', '-p', 'Restart', '-p', 'TimeoutStopUSec', '-p', 'ControlGroup'], env=manager_env, text=True)
        assert 'KillMode=control-group' in properties and 'Restart=no' in properties and 'TimeoutStopUSec=3s' in properties
        started = time.monotonic()
        assert call('stop')['ok']
        elapsed = time.monotonic() - started
        assert elapsed < 15 and all(vanished(identity) for identity in identities)
        assert call('stop')['ok']
        receipt['cases']['concurrent_duplicate_and_ordinary_descendants'] = {
            'generation': generation, 'identities': identities, 'properties': properties.splitlines(),
            'stop_seconds': elapsed, 'all_owned_lifetimes_exited': True, 'manifest': manifest(generation)}

        replacement = controller()['session']['generation']
        assert replacement != generation
        for operation in ('session.start', 'session.status', 'session.stop'):
            assert controller(operation, generation)['error']['code'] == 'generation_mismatch'
        process = manifest(replacement)['process']
        os.kill(process['pid'], signal.SIGUSR1)
        marker = artifacts / 'generations' / replacement / 'fixture-lock-held'
        deadline = time.monotonic() + 2
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        started = time.monotonic()
        unavailable = call('status', '--timeout', '.1')
        status_seconds = time.monotonic() - started
        assert not unavailable['ok'] and status_seconds < .7
        for operation in ('session.start', 'session.status', 'session.stop'):
            assert controller(operation, generation)['error']['code'] == 'generation_mismatch'
        (runtime / 'agent-desktop/g' / replacement / 'control.sock').unlink()
        descendants = [process, *json.loads((artifacts / 'generations' / replacement / 'fixture-children.json').read_text())]
        started = time.monotonic()
        stopped = call('stop', '--generation', replacement)
        elapsed = time.monotonic() - started
        assert stopped['ok'] and elapsed < 15 and all(vanished(identity) for identity in descendants), stopped
        receipt['cases']['replacement_stale_requests_frozen_lock_missing_socket'] = {
            'generation': replacement, 'status_seconds': status_seconds, 'status_error': unavailable['error']['code'],
            'stop_seconds': elapsed, 'all_owned_lifetimes_exited': True, 'manifest': manifest(replacement)}
        crashed = controller()['session']['generation']
        identity = manifest(crashed)['process']
        os.kill(identity['pid'], signal.SIGKILL)
        unit = 'agent-desktop-' + crashed + '.service'
        deadline = time.monotonic() + 5
        while subprocess.check_output(['/usr/bin/systemctl', '--user', 'show', unit, '-p', 'Result', '--value'],
                env=manager_env, text=True).strip() != 'signal':
            assert time.monotonic() < deadline
            time.sleep(.01)
        crash_stop = call('stop')
        assert crash_stop['ok'] and crash_stop['result']['state'] == 'failed'
        for operation in ('stop', 'status'):
            assert call(operation)['result']['state'] == 'failed'
        crash_manifest = manifest(crashed)
        assert crash_manifest['first_failure'] == 'session_failed' and crash_manifest['cleanup']['state'] == 'complete'
        receipt['cases']['sigkill_then_stop_without_status_preserves_failure'] = {
            'generation': crashed, 'manifest': crash_manifest, 'repeated_stop_status_stay_failed': True}
        assert not call('start')['ok'], 'Public readiness gate must remain closed until #20.'
        receipt['public_start_gated_on_readiness'] = True
    finally:
        for path in (runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
            unit = 'agent-desktop-' + path.parent.name + '.service'
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit], env=manager_env, capture_output=True, timeout=8)
            subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', unit], env=manager_env, capture_output=True, timeout=3)

receipt['installed_source_sha256'] = {str(path.relative_to(Path(agent_desktop.__file__).parent)):
    hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(agent_desktop.__file__).parent.glob('*.py'))}
print(json.dumps(receipt, indent=2, sort_keys=True))
