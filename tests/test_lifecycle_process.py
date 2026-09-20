"""Separate processes and real user services; no mocked cleanup or skipped gi."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))
from agent_desktop.lifecycle import Systemd, read_metadata
from agent_desktop.runtime import Runtime
from unittest.mock import patch


class LifecycleProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adp-')
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'r'
        self.runtime.mkdir(mode=0o700)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime), PYTHONPATH=str(SRC))
        self.env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/' + str(os.getuid()) + '/bus')
        self.manager_env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/' + str(os.getuid()))
        self.artifacts = str(self.root / 'artifacts')
        self.generations = []

    def controller(self, operation='session.start', generation='-', artifacts=None, *, wait=True):
        argv = [sys.executable, '-I', str(ROOT / 'tests/lifecycle_controller_fixture.py'), operation,
                'default', artifacts or self.artifacts, generation, str(SRC)]
        process = subprocess.Popen(argv, env=self.env, cwd='/', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not wait:
            return process
        return self.result(process)

    def result(self, process):
        output, error = process.communicate(timeout=48)
        self.assertEqual(error, '')
        value = json.loads(output)
        generation = value['session']['generation']
        if generation and generation not in self.generations:
            self.generations.append(generation)
        return value

    def cli(self, operation, *args):
        process = subprocess.run([sys.executable, '-m', 'agent_desktop', '--json', 'session', operation, *args],
                                 env=self.env, cwd='/tmp', capture_output=True, text=True, timeout=17)
        self.assertEqual(process.stderr, '')
        return json.loads(process.stdout)

    def manifest(self, generation):
        return json.loads((Path(self.artifacts) / 'generations' / generation / 'manifest.json').read_text())

    def children(self, generation):
        path = Path(self.artifacts) / 'generations' / generation / 'fixture-children.json'
        deadline = time.monotonic() + 2
        while not path.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        return json.loads(path.read_text())

    def gone(self, identity):
        try:
            fields = Path('/proc', str(identity['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
            return fields[19] != identity['start_ticks'] or fields[0] == 'Z'
        except FileNotFoundError:
            return True

    def tearDown(self):
        # Exact fixture units only; include published claims if start failed.
        for path in (self.runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
            self.generations.append(path.parent.name)
        for generation in set(self.generations):
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', 'agent-desktop-' + generation + '.service'],
                           env=self.manager_env, capture_output=True, timeout=8)
            subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', 'agent-desktop-' + generation + '.service'],
                           env=self.manager_env, capture_output=True, timeout=3)
        self.temp.cleanup()

    def test_concurrent_duplicate_start_replacement_stale_requests_and_cgroup_children(self):
        a = self.controller(wait=False)
        b = self.controller(wait=False)
        first, duplicate = self.result(a), self.result(b)
        self.assertTrue(first['ok'], first)
        self.assertTrue(duplicate['ok'], duplicate)
        generation = first['session']['generation']
        self.assertEqual(duplicate['session']['generation'], generation)
        self.assertFalse(first['result']['desktop_ready'])
        self.assertEqual(self.controller(artifacts=str(self.root / 'other'))['error']['code'], 'session_conflict')
        self.assertEqual(self.cli('status')['session']['generation'], generation)
        manifest = self.manifest(generation)
        unit = 'agent-desktop-' + generation + '.service'
        self.assertEqual(manifest['process']['service'], unit)
        identities = [manifest['process'], *self.children(generation)]
        for identity in identities:
            self.assertIn(unit, Path('/proc', str(identity['pid']), 'cgroup').read_text())
        properties = subprocess.check_output(['/usr/bin/systemctl', '--user', 'show', unit, '-p', 'KillMode',
                    '-p', 'Restart', '-p', 'TimeoutStopUSec'], env=self.manager_env, text=True)
        self.assertIn('KillMode=control-group', properties)
        self.assertIn('Restart=no', properties)
        self.assertIn('TimeoutStopUSec=3s', properties)
        started = time.monotonic()
        self.assertTrue(self.cli('stop')['ok'])
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(all(self.gone(identity) for identity in identities))
        self.assertTrue(self.cli('stop')['ok'])
        replacement = self.controller()['session']['generation']
        self.assertNotEqual(generation, replacement)
        for operation in ('session.start', 'session.status', 'session.stop'):
            self.assertEqual(self.controller(operation, generation)['error']['code'], 'generation_mismatch')
        self.assertEqual(self.cli('status')['session']['generation'], replacement)
        self.assertEqual(self.manifest(generation)['cleanup']['state'], 'complete')

    def test_frozen_worker_holding_record_lock_status_bound_and_fallback_stop(self):
        generation = self.controller()['session']['generation']
        manifest = self.manifest(generation)
        identities = [manifest['process'], *self.children(generation)]
        os.kill(manifest['process']['pid'], signal.SIGUSR1)
        marker = Path(self.artifacts) / 'generations' / generation / 'fixture-lock-held'
        deadline = time.monotonic() + 2
        while not marker.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        started = time.monotonic()
        unavailable = self.cli('status', '--timeout', '.1')
        self.assertFalse(unavailable['ok'])
        self.assertLess(time.monotonic() - started, .7)
        # Removing the socket must not erase the manager's unit ownership.
        (self.runtime / 'agent-desktop/g' / generation / 'control.sock').unlink()
        started = time.monotonic()
        result = self.cli('stop', '--generation', generation)
        self.assertTrue(result['ok'], result)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(all(self.gone(identity) for identity in identities))
        self.assertEqual(self.manifest(generation)['cleanup']['state'], 'complete')

    def test_start_stop_race_and_unexpected_worker_death(self):
        generation = self.controller()['session']['generation']
        stopper = self.controller('session.stop', generation, wait=False)
        starter = self.controller(wait=False)
        stopped, started = self.result(stopper), self.result(starter)
        self.assertTrue(stopped['ok'], stopped)
        self.assertTrue(started['ok'], started)
        # The start may serialize before or after stop; a later start always
        # resolves a live generation, and the stale stop cannot affect it.
        live = self.controller()['session']['generation']
        self.assertNotEqual(generation, live)
        self.assertEqual(self.controller('session.stop', generation)['error']['code'], 'generation_mismatch')
        manifest = self.manifest(live)
        identities = [manifest['process'], *self.children(live)]
        os.kill(manifest['process']['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not all(self.gone(identity) for identity in identities):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        status = self.cli('status')
        self.assertEqual(status['result']['state'], 'failed', status)
        self.assertEqual(self.manifest(live)['first_failure'], 'session_failed')

    def test_direct_stop_after_sigkill_retains_failure(self):
        generation = self.controller()['session']['generation']
        manifest = self.manifest(generation)
        identities = [manifest['process'], *self.children(generation)]
        os.kill(manifest['process']['pid'], signal.SIGKILL)
        # Observe the actual service failure, without a toolkit status call that
        # could mask whether stop itself performs the reconciliation.
        deadline = time.monotonic() + 5
        while True:
            state = subprocess.check_output(['/usr/bin/systemctl', '--user', 'show',
                    'agent-desktop-' + generation + '.service', '-p', 'Result', '--value'],
                    env=self.manager_env, text=True).strip()
            if state == 'signal':
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        result = self.cli('stop')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['state'], 'failed')
        manifest = self.manifest(generation)
        self.assertEqual(manifest['state'], 'failed')
        self.assertEqual(manifest['first_failure'], 'session_failed')
        self.assertEqual(manifest['cleanup']['state'], 'complete')
        for operation in ('stop', 'status'):
            self.assertEqual(self.cli(operation)['result']['state'], 'failed')
        self.assertTrue(all(self.gone(identity) for identity in identities))


if __name__ == '__main__':
    unittest.main()
