"""Caller normalization, private environment, durable faults and scheduler ordering."""
from contextlib import redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop.artifacts import Store, LIMIT, safe_projection
from agent_desktop.cli import parse_request
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.environment import compose, executable, PROTECTED
from agent_desktop.paths import normalize
from agent_desktop.protocol import request_from_wire
from agent_desktop.records import Records
from agent_desktop.scheduler import Scheduler
from agent_desktop.transport import Admission, Connection

GEN = 'a' * 32
APP = {'generation': GEN, 'application_id': 'retained'}


def request(operation='session.status', arguments=None):
    return normalize(make_request(operation, arguments=arguments or {}, caller_cwd='/caller', expected_generation=GEN))


class PathEnvironmentTests(unittest.TestCase):
    def test_caller_paths_and_raw_wire_rejection(self):
        cases = [(['launch', '--', './tool'], 'cwd', '/caller'),
                 (['launch', '--cwd', '../project', '--', './tool'], 'cwd', '/project'),
                 (['launch', '--cwd', '/project', '--', './tool'], 'cwd', '/project'),
                 (['session', 'start'], 'artifacts', '/caller/.agent-desktop/artifacts'),
                 (['session', 'start', '--artifacts', './out'], 'artifacts', '/caller/out'),
                 (['screenshot', '--output', 'screen.png'], 'output', '/caller/screen.png'),
                 (['doctor'], 'dependency_root', '/caller/.local/dependencies')]
        for argv, key, expected in cases:
            with self.subTest(argv=argv):
                req = parse_request(argv, GEN, '/caller')[0]
                self.assertEqual(req.arguments[key], expected)
        req = request('launch', {'argv': ['./tool', 'literal $HOME'], 'cwd': 'project'})
        self.assertEqual(req.arguments['argv'], ['./tool', 'literal $HOME'])
        self.assertEqual(request_from_wire(req.payload()), req)
        for cwd in (None, 'relative', '/caller/../other'):
            value = req.payload()
            value['arguments']['cwd'] = cwd
            with self.assertRaises(ContractError):
                request_from_wire(value)
        self.assertNotIn('output', request('screenshot').arguments)
        self.assertEqual(normalize(request('launch', {'argv': ['tool'], 'cwd': '~'})).arguments['cwd'], '/caller/~')

    def private(self, root):
        return {'HOME': str(root / 'home'), 'XDG_RUNTIME_DIR': str(root),
                'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
                'XDG_DATA_HOME': str(root / 'data'), 'XDG_STATE_HOME': str(root / 'state'),
                'TMPDIR': str(root / 'tmp'), 'XDG_CONFIG_DIRS': str(root / 'empty'), 'WAYLAND_DISPLAY': 'wayland-private',
                'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(root / 'bus'),
                'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=' + str(root / 'no-bus')}

    def test_environment_fail_closed_and_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.private(Path(directory))
            base = {key: 'HOST_SECRET' for key in PROTECTED} | {'PATH': '/base', 'LANG': 'C'}
            result = compose(base, {'PATH': ''}, private)
            self.assertEqual(result['PATH'], '')
            self.assertNotIn('HOST_SECRET', result.values())
            self.assertNotIn('DISPLAY', result)
            self.assertEqual(result['XDG_CONFIG_DIRS'], private['XDG_CONFIG_DIRS'])
            self.assertEqual(base['PATH'], '/base')
            for key in PROTECTED:
                with self.subTest(key=key), self.assertRaises(ContractError):
                    request('launch', {'argv': ['app'], 'env': {key: 'bad'}})
            for key in private:
                with self.subTest(key=key), self.assertRaises(ContractError):
                    compose({}, {}, {k: v for k, v in private.items() if k != key})
            for config in (str(Path(directory) / 'empty') + ':/etc/xdg',
                           str(Path(directory) / 'empty') + ':',
                           str(Path(directory) / 'name:colon')):
                with self.subTest(config=config), self.assertRaises(ContractError):
                    compose({}, {}, private | {'XDG_CONFIG_DIRS': config})
            colon_root = self.private(Path(directory) / 'root:colon')
            with self.assertRaises(ContractError):
                compose({}, {}, colon_root)
            for extra in (';unix:path=/host', ',guid=abc', '%2fhost'):
                with self.assertRaises(ContractError):
                    compose({}, {}, private | {'DBUS_SESSION_BUS_ADDRESS': private['DBUS_SESSION_BUS_ADDRESS'] + extra})
            with self.assertRaises(ContractError):
                compose({}, {}, private | {'WAYLAND_DISPLAY': '..'})
            Path(directory, 'no-bus').touch()
            with self.assertRaises(ContractError):
                compose({}, {}, private)

    def test_executable_final_path_and_cwd_without_chdir(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            (cwd / 'bin').mkdir()
            tool = cwd / 'bin/tool'
            tool.write_text('#!/bin/sh\n')
            tool.chmod(0o700)
            original = os.getcwd()
            for name, env in [('tool', {'PATH': 'bin'}), ('./bin/tool', {'PATH': '/no'}),
                              (str(tool), {'PATH': ''})]:
                self.assertEqual(executable(name, str(cwd), env)['executable'], str(tool))
            self.assertEqual(executable('tool', str(cwd / 'bin'), {'PATH': ''})['executable'], str(tool))
            self.assertEqual(executable('sh', str(cwd), {})['lookup'], 'path')
            with self.assertRaises(ContractError):
                executable('tool', str(cwd), {'PATH': '/no'})
            with self.assertRaises(ContractError):
                executable('./bin', str(cwd), {})
            self.assertEqual(os.getcwd(), original)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'durable', 'default', GEN, create=True,
                           disposable=[self.root / 'runtime'])
        self.addCleanup(self.store.close)

    def token(self):
        return self.store.request(request(), 1, 4)

    def record(self, token):
        return json.loads((self.store.path / 'requests' / token[0] / token[1] / 'record.json').read_text())

    def test_private_collision_free_attempts_and_exact_launch(self):
        req = request()
        tokens = [self.store.request(req, 1, 4) for _ in range(12)]
        self.assertEqual(len(set(tokens)), 12)
        paths = [self.store.allocate(tokens[0], 'capture') for _ in range(15)]
        self.assertEqual(len(set(paths)), 15)
        self.store.launch(tokens[0], ['./app', '', '--flag', 'literal $HOME'], '/caller')
        launch = paths[0].parent / 'launch.json'
        self.assertEqual(json.loads(launch.read_text())['argv'], ['./app', '', '--flag', 'literal $HOME'])
        for path in self.store.path.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        with self.assertRaises(ContractError):
            Store(self.root / 'durable', 'default', GEN, create=True)
        with self.assertRaises(ContractError):
            Store(self.root / 'runtime/out', 'default', 'b' * 32, create=True, disposable=[self.root / 'runtime'])
        with self.assertRaises(ContractError):
            Store(self.root / 'durable', 'different', GEN)
        reopened = Store(self.root / 'durable', 'default', GEN)
        reopened.close()
        self.store.artifact_state(paths[0], 'failed')
        self.store.artifact_state(paths[0], 'complete')
        self.assertEqual(json.loads((paths[0].parent / (paths[0].name.split('.')[0] + '.json')).read_text())['state'], 'failed')

    def test_atomic_failure_and_sticky_cleanup_survive_runtime_removal(self):
        prior = self.store.read()
        with patch('agent_desktop.artifacts.os.replace', side_effect=OSError), self.assertRaises(ContractError):
            self.store.generation_update(state='running')
        self.assertEqual(self.store.read(), prior)
        self.assertEqual(list(self.store.path.glob('.*.tmp')), [])
        self.store.generation_update(state='failed', failure='capture_failed')
        self.store.generation_update(state='stopped', cleanup='complete')
        value = self.store.read()
        self.assertEqual(value['state'], 'failed')
        self.assertEqual(value['first_failure'], 'capture_failed')
        self.store.generation_update(state='running')
        self.assertEqual(self.store.read()['state'], 'failed')
        # Simulate directory fsync failing after the atomic replace: valid new data.
        original = os.fsync
        count = 0
        def fsync(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError()
            return original(fd)
        with patch('agent_desktop.artifacts.os.fsync', side_effect=fsync), self.assertRaises(ContractError):
            self.store.generation_update(cleanup='uncertain')
        self.assertEqual(self.store.read()['cleanup']['state'], 'uncertain')

    def test_symlinks_and_nonblocking_lock(self):
        lock = os.open(self.store.path / 'record.lock', os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            start = time.monotonic()
            with self.assertRaises(ContractError):
                self.store.generation_update(state='running')
            self.assertLess(time.monotonic() - start, .1)
        finally:
            os.close(lock)
        manifest = self.store.path / 'manifest.json'
        original = manifest.read_bytes()
        manifest.unlink()
        victim = self.root / 'victim'
        victim.write_bytes(original)
        manifest.symlink_to(victim)
        with self.assertRaises(ContractError):
            self.store.generation_update(state='running')
        self.assertEqual(victim.read_bytes(), original)
        self.assertTrue(manifest.is_symlink())

    def test_failed_startup_and_collision_exhaustion(self):
        from agent_desktop.worker import run
        failed_root = self.root / 'failed-start'
        with patch('agent_desktop.worker.Endpoint', side_effect=ContractError('session_conflict', 'Conflict.')):
            with self.assertRaises(ContractError):
                run('default', 'b' * 32, artifacts=failed_root)
        failed = json.loads((failed_root / 'generations' / ('b' * 32) / 'manifest.json').read_text())
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['cleanup']['state'], 'complete')
        token = self.token()
        fixed = Mock(hex='c' * 32)
        with patch('agent_desktop.artifacts.uuid.uuid4', return_value=fixed):
            path = self.store.allocate(token, 'capture')
            with self.assertRaises(ContractError):
                self.store.allocate(token, 'capture')
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b'')
        # Only known owned files survive the diagnostic projection.
        self.store.transition(token, 'effects', outcome='partial', references={
            'artifacts': [str(path), '/host/secret'], 'logs': {'stdout': str(path)},
            'process': {'pid': 123, 'start_time_ticks': 456, 'secret': 'SECRET'}})
        projected = self.record(token)['references']
        self.assertEqual(projected['artifacts'], [str(path)])
        self.assertEqual(projected['process'], {'pid': 123, 'start_time_ticks': 456})
        self.store.provenance(output={'width': 1280, 'height': 720, 'scale': 1},
                              dependencies=[{'component': 'fixture', 'version': 'test', 'patches': 'none'}])
        self.assertEqual(self.store.read()['output']['state'], 'collected')

    def test_directory_links_fsynced_before_descendant_allocation(self):
        calls = []
        original_mkdir, original_fsync = os.mkdir, os.fsync
        def mkdir(path, *args, **kwargs):
            original_mkdir(path, *args, **kwargs)
            parent = Path(os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}"))
            calls.append(('mkdir', parent / path, parent))
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                calls.append(('fsync', Path(os.readlink(f'/proc/self/fd/{fd}'))))
            original_fsync(fd)
        with patch('agent_desktop.artifacts.os.mkdir', side_effect=mkdir), patch('agent_desktop.artifacts.os.fsync', side_effect=fsync):
            nested = Store(self.root / 'new-parent/deep/root', 'default', 'e' * 32, create=True)
            nested.close()
            token = self.token()
        for index, call in enumerate(calls):
            if call[0] != 'mkdir':
                continue
            self.assertEqual(calls[index + 1], ('fsync', call[2]), call)
        self.assertIn(('fsync', self.store.path / 'requests'), calls)
        self.assertIn(('fsync', self.root), calls)
        self.assertIn(('fsync', self.root / 'new-parent'), calls)
        self.assertTrue(self.record(token))

    def test_ancestor_swap_cannot_redirect_store(self):
        durable = self.root / 'race-root'
        disposable = self.root / 'runtime'
        disposable.mkdir(mode=0o700)
        (disposable / 'generations').mkdir(mode=0o700)
        original_open = os.open
        swapped = False
        def open_swapped(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == 'generations' and not swapped:
                swapped = True
                durable.rename(self.root / 'original-root')
                durable.symlink_to(disposable, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)
        with patch('agent_desktop.artifacts.os.open', side_effect=open_swapped), self.assertRaises(ContractError):
            Store(durable, 'default', 'e' * 32, create=True, disposable=[disposable])
        self.assertTrue(swapped)
        self.assertEqual(list((disposable / 'generations').iterdir()), [])
        self.assertEqual(list((self.root / 'original-root/generations').iterdir()), [])

    def test_reopen_refuses_missing_layout_lock_and_incomplete_manifest(self):
        for index, missing in enumerate(('requests', 'applications', 'logs/worker.log', 'record.lock', 'events.jsonl')):
            root = self.root / f'broken-{index}'
            store = Store(root, 'default', GEN, create=True)
            path = store.path
            store.close()
            victim = path / missing
            victim.rmdir() if victim.is_dir() else victim.unlink()
            with self.subTest(missing=missing), self.assertRaises(ContractError):
                Store(root, 'default', GEN)
            self.assertFalse(victim.exists())
        manifest = self.store.path / 'manifest.json'
        good = json.loads(manifest.read_text())
        malformed = [dict(schema_version=1, revision=0, session='default', generation=GEN),
                     good | {'schema_version': True}, good | {'cleanup': {}},
                     good | {'dependencies': {'state': 'partial'}}, good | {'records': {}},
                     good | {'output': {'state': 'collected'}}, good | {'process': {'state': 'collected'}}]
        for value in malformed:
            manifest.write_text(json.dumps(value))
            with self.assertRaises(ContractError):
                Store(self.root / 'durable', 'default', GEN)
        manifest.write_text(json.dumps(good))
        self.assertEqual(self.store.read(), good)
        lock = self.store.path / 'record.lock'
        held = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.unlink()
            with self.assertRaises(ContractError):
                self.store.read()
            self.assertFalse(lock.exists())
            lock.touch(mode=0o600)
            with self.assertRaises(ContractError):
                self.store.read()
        finally:
            os.close(held)

    def test_interrupted_initialization_has_no_attachable_manifest(self):
        root = self.root / 'interrupted'
        original = Store._initialize_file
        def interrupted(store, directory, name):
            if name == 'bus.log':
                raise OSError()
            return original(store, directory, name)
        with patch.object(Store, '_initialize_file', interrupted), self.assertRaises(ContractError):
            Store(root, 'default', GEN, create=True)
        self.assertFalse((root / 'generations' / GEN / 'manifest.json').exists())
        with self.assertRaises(ContractError):
            Store(root, 'default', GEN)

    def test_stable_start_and_terminal_observation_times(self):
        token = self.token()
        self.store.transition(token, 'started')
        started = self.record(token)
        self.store.transition(token, 'effects', outcome='partial', references={'app': APP})
        self.store.transition(token, 'finalizing')
        self.store.transition(token, 'terminal', outcome='success')
        terminal = self.record(token)
        self.assertEqual(terminal['started_at'], started['started_at'])
        self.assertEqual(terminal['started_monotonic'], started['started_monotonic'])
        self.assertIsNotNone(terminal['terminal_observed_at'])
        self.assertGreaterEqual(terminal['terminal_observed_monotonic'], terminal['started_monotonic'])
        self.store.transition(token, 'terminal', outcome='unknown')
        self.assertEqual(self.record(token), terminal)

    def test_bounded_history_projection_and_application_records(self):
        manifest = (self.store.path / 'manifest.json').read_bytes()
        for _ in range(80):
            token = self.token()
            self.store.transition(token, 'effects', outcome='partial', references={
                'app': APP, 'text': 'SECRET', 'environment': {'TOKEN': 'SECRET'},
                'context': {'private': 'SECRET'}, 'title': 'SECRET', 'data': 'x' * 100000})
            self.store.transition(token, 'finalizing')
            self.store.transition(token, 'terminal', outcome='partial', error_code='timeout')
            record = self.record(token)
            self.assertEqual(record['references'], {'app': APP})
            self.assertNotIn('SECRET', json.dumps(record))
            self.assertLess(len(json.dumps(record)), 2048)
        self.assertEqual((self.store.path / 'manifest.json').read_bytes(), manifest)
        self.store.launch(token, ['app'], '/caller')
        self.store.application(token, 'app-1', pid=123, birth_identity='linux-start:12345',
                               executable='/bin/app', logs={})
        self.assertTrue((self.store.path / 'applications/app-1/record.json').exists())
        with self.assertRaises(ContractError):
            self.store.transition(token, 'effects', outcome='success')


class BridgeTests(StoreTests):
    # Store tests also exercise a fresh inherited store fixture.
    def admission(self, req=None):
        server = Mock()
        server.name, server.generation, server.active = 'default', GEN, {}
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        connection = Connection(server, sock)
        admission = Admission(connection, req or request())
        connection.admission = admission
        return admission

    def test_final_serialization_deadline_and_terminal_fault(self):
        records = Records(self.store)
        admission = self.admission()
        records.attach(admission.request, admission)
        token = records._ensure(records.live[admission.request.request_id])
        self.store.transition(token, 'finalizing')
        admission.deadline = .05
        with patch('agent_desktop.transport.time.monotonic', side_effect=[0, .1, .1]):
            admission.complete(result={'application': APP, 'secret': 'NO_DUMP'})
        result = self.record(token)
        self.assertEqual(result['error_code'], 'timeout')
        self.assertNotIn('NO_DUMP', json.dumps(result))
        self.assertEqual(result['references'], {'application': APP})
        admission = self.admission()
        records.attach(admission.request, admission)
        token = records._ensure(records.live[admission.request.request_id])
        self.store.transition(token, 'finalizing')
        with patch.object(self.store, 'transition', side_effect=OSError), redirect_stderr(io.StringIO()) as err:
            admission.complete(result={'ok': True})
        self.assertTrue(admission.final_payload['ok'])
        self.assertEqual(self.record(token)['phase'], 'finalizing')
        self.assertIn('uncertain', err.getvalue())
        self.assertEqual(records.live, {})

    def test_admission_storage_failure_blocks_effects_but_stop_cleans(self):
        records = Records(self.store)
        effects = []
        class Task:
            cleanup_seconds = .1
            def __init__(self, req, context):
                self.req = req
            def step(self, now):
                effects.append(self.req.operation)
                return {}
            def request_cancel(self, code):
                effects.append('release')
            def cleanup(self, now):
                effects.append('cleanup')
                return True
        scheduler = Scheduler(factory=records.factory(Task), observer=records.observe,
                              capabilities={'session.stop'})
        ordinary = self.admission()
        records.attach(ordinary.request, ordinary)
        with patch.object(self.store, 'request', side_effect=OSError), redirect_stderr(io.StringIO()):
            scheduler.submit(ordinary.request, ordinary)
            scheduler.tick()
            self.assertEqual(effects, [])
            self.assertEqual(ordinary.final_payload['error']['code'], 'artifact_failed')
            stop = self.admission(request('session.stop'))
            records.attach(stop.request, stop)
            scheduler.submit(stop.request, stop)
            scheduler.tick()
        self.assertIn('session.stop', effects)
        self.assertEqual(stop.final_payload['error']['code'], 'artifact_failed')
        self.assertEqual(records.live, {})

    def test_request_parent_fsync_failure_prevents_factory_effects(self):
        records = Records(self.store)
        factory = Mock()
        scheduler = Scheduler(factory=records.factory(factory), observer=records.observe)
        admission = self.admission()
        records.attach(admission.request, admission)
        original = os.fsync
        def fsync(fd):
            if os.readlink(f'/proc/self/fd/{fd}') == str(self.store.path / 'requests'):
                raise OSError()
            return original(fd)
        with patch('agent_desktop.artifacts.os.fsync', side_effect=fsync), redirect_stderr(io.StringIO()):
            scheduler.submit(admission.request, admission)
            scheduler.tick()
        factory.assert_not_called()
        self.assertEqual(admission.final_payload['error']['code'], 'artifact_failed')

    def test_cancellation_calls_release_before_failed_persistence(self):
        records = Records(self.store)
        calls = []
        class Task:
            cleanup_seconds = .1
            def __init__(self, req, context):
                pass
            def step(self, now):
                calls.append('step')
            def request_cancel(self, code):
                calls.append('release')
            def cleanup(self, now):
                calls.append('cleanup')
                return True
        scheduler = Scheduler(factory=records.factory(Task), observer=records.observe)
        admission = self.admission()
        records.attach(admission.request, admission)
        scheduler.submit(admission.request, admission)
        scheduler.tick()
        def failed(*args, **kwargs):
            calls.append('storage_failed')
            raise OSError()
        with patch.object(self.store, 'transition', side_effect=failed), redirect_stderr(io.StringIO()):
            scheduler.cancel(admission.request.request_id)
            scheduler.tick()
        self.assertLess(calls.index('release'), calls.index('storage_failed'))
        self.assertIn('cleanup', calls)
        self.assertEqual(admission.final_payload['error']['code'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
