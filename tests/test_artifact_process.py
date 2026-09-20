"""Real worker/CLI processes retain records across disconnect, storage faults and stop."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SRC = Path(__file__).resolve().parents[1] / 'src'
GEN = 'd' * 32
WINDOW = GEN + ':2a63a414-1509-460a-bff9-b7c1103ba8d5'


class ArtifactProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ada-')
        self.root = Path(self.temp.name)
        for name in ('runtime', 'caller', 'worker'):
            (self.root / name).mkdir(mode=0o700)
        self.artifacts = self.root / 'artifacts'
        self.markers = self.root / 'markers'
        self.env = os.environ | {'XDG_RUNTIME_DIR': str(self.root / 'runtime'), 'PYTHONPATH': str(SRC),
                                 'PYTHONWARNINGS': 'ignore'}
        self.err = (self.root / 'worker.err').open('w')
        self.worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name('artifact_worker_fixture.py')),
                                        GEN, str(self.artifacts), str(self.markers)],
                                       cwd=self.root / 'worker', env=self.env, stdout=subprocess.DEVNULL, stderr=self.err)
        self.clients = []
        self.wait(lambda: (self.root / 'runtime/agent-desktop/current/default.json').exists())

    def tearDown(self):
        for client in self.clients:
            if client.poll() is None:
                client.kill()
            client.communicate(timeout=3)
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.wait(timeout=3)
        self.err.close()
        self.temp.cleanup()

    @property
    def generation(self):
        return self.artifacts / 'generations' / GEN

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if self.worker.poll() is not None:
                self.fail((self.root / 'worker.err').read_text())
            time.sleep(.003)
        self.fail('Fixture timed out')

    def client(self, *args):
        proc = subprocess.Popen([sys.executable, '-m', 'agent_desktop', '--json', *args],
                                cwd=self.root / 'caller', env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.clients.append(proc)
        return proc

    def complete(self, proc):
        out, err = proc.communicate(timeout=4)
        self.assertEqual(err, '')
        self.assertEqual(len(out.splitlines()), 1)
        return json.loads(out)

    def markers_of(self, event):
        try:
            return [value for line in self.markers.read_text().splitlines()
                    if (value := json.loads(line))['event'] == event]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def record(self, request_id):
        paths = list((self.generation / 'requests' / request_id).glob('*/record.json'))
        return json.loads(paths[0].read_text()) if paths else None

    def test_paths_diagnostics_and_durable_shutdown(self):
        payload = self.complete(self.client('launch', '--cwd', '../project', '--env', 'TOKEN=ENV_SECRET', '--', './tool', 'literal'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['result']['cwd'], str(self.root / 'project'))
        self.assertEqual(payload['result']['argv'], ['./tool', 'literal'])
        terminal = self.wait(lambda: self.record(payload['request_id']))
        self.assertEqual(terminal['outcome'], 'success')
        capture = self.complete(self.client('screenshot', '--output', 'frame.png'))
        self.assertEqual(capture['result']['output'], str(self.root / 'caller/frame.png'))
        default = self.complete(self.client('launch', '--', 'tool'))
        self.assertEqual(default['result']['cwd'], str(self.root / 'caller'))
        self.worker.terminate()
        self.worker.wait(timeout=3)
        manifest = json.loads((self.generation / 'manifest.json').read_text())
        self.assertEqual(manifest['state'], 'stopped')
        self.assertEqual(manifest['cleanup']['state'], 'uncertain')
        self.assertFalse((self.root / 'runtime/agent-desktop/current/default.json').exists())
        for path in self.generation.rglob('*'):
            if path.is_file():
                self.assertNotIn('ENV_SECRET', path.read_text())
        self.assertTrue((self.generation / 'logs/worker.log').exists())

    def test_contended_store_cannot_prevent_stop_release_cleanup(self):
        active = self.client('type', '--window', WINDOW, 'TEXT_SECRET')
        effect = self.wait(lambda: self.markers_of('effect'))[0]
        lock = os.open(self.generation / 'record.lock', os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = time.monotonic()
            stop = self.client('session', 'stop')
            release = self.wait(lambda: self.markers_of('release'))[0]
            self.assertEqual(release['request_id'], effect['request_id'])
            # Includes Python CLI startup, unlike the worker dispatch-only bound.
            self.assertLess(release['at'] - before, .5)
            self.wait(lambda: self.markers_of('cleanup'))
            self.assertEqual(self.complete(active)['error']['code'], 'cancelled')
            self.assertEqual(self.complete(stop)['error']['code'], 'artifact_failed')
        finally:
            os.close(lock)
        record = self.record(effect['request_id'])
        self.assertNotEqual(record['outcome'], 'success')
        log = (self.generation / 'logs/worker.log').read_text()
        self.assertIn('artifact_failed', log)
        self.assertNotIn('TEXT_SECRET', log)

    def test_sigint_retains_partial_handle_after_caller_exit(self):
        active = self.client('type', '--window', WINDOW, 'TEXT_SECRET')
        effect = self.wait(lambda: self.markers_of('effect'))[0]
        active.send_signal(signal.SIGINT)
        self.assertEqual(self.complete(active)['error']['code'], 'cancelled')
        self.wait(lambda: self.markers_of('cleanup'))
        terminal = self.wait(lambda: (record if (record := self.record(effect['request_id']))['phase'] == 'terminal' else None))
        self.assertEqual(terminal['references']['app']['application_id'], 'fixture')
        self.assertEqual(terminal['error_code'], 'cancelled')
        self.assertNotIn('TEXT_SECRET', json.dumps(terminal))


if __name__ == '__main__':
    unittest.main()
