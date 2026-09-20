"""Gated launch ordering, request retention and bounded kernel observations."""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_desktop import app_launcher
from agent_desktop.app_processes import Application, Registry, birth, identity, live, scan
from agent_desktop.applications import LaunchTask
from agent_desktop.artifacts import Store
from agent_desktop.children import Children
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.paths import normalize

GEN = 'a' * 32


class ScannerTests(unittest.TestCase):
    def test_streaming_limits_and_malformed_or_missing_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            file = root / 'cgroup.procs'
            file.write_text(''.join(str(i + 1) + '\n' for i in range(4096)))
            observed = list(scan(root))
            self.assertEqual(sum(x is not None for x in observed), 4096)
            file.write_text(file.read_text() + '5000\n')
            with self.assertRaises(ContractError):
                list(scan(root))
            for content in ('12', '0\n', 'x\n', '9' * 30 + '\n'):
                file.write_text(content)
                with self.assertRaises(ContractError):
                    list(scan(root))
            file.unlink()
            with self.assertRaises(FileNotFoundError):
                list(scan(root))

    def test_depth_and_directory_limits_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root
            for depth in range(10):
                (path / 'cgroup.procs').write_text('')
                if depth < 9:
                    path = path / 'nested'
                    path.mkdir()
            with self.assertRaises(ContractError):
                list(scan(root))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'cgroup.procs').write_text('')
            for n in range(255):
                child = root / str(n)
                child.mkdir()
                (child / 'cgroup.procs').write_text('')
            list(scan(root))
            child = root / 'overflow'
            child.mkdir()
            (child / 'cgroup.procs').write_text('')
            with self.assertRaises(ContractError):
                list(scan(root))

    def test_identity_rejects_pid_reuse_and_foreign_membership(self):
        for ticks, group, living in (([1, 2], '/owned', True), ([1, 1], '/foreign', True),
                                     ([1, 1], '/owned', False)):
            with patch('agent_desktop.app_processes.birth', side_effect=ticks), \
                    patch('os.pidfd_open', return_value=1234), \
                    patch('agent_desktop.app_processes.membership', return_value=group), \
                    patch('agent_desktop.app_processes.live', return_value=living), patch('os.close') as close:
                self.assertIsNone(identity(12, '/owned'))
                close.assert_called_once_with(1234)

    def test_pidfd_above_select_limit(self):
        fd = os.pidfd_open(os.getpid())
        high = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 1100)
        try:
            self.assertTrue(live(high))
        finally:
            os.close(fd)
            os.close(high)


class HelperTests(unittest.TestCase):
    def test_gate_and_eof_expiry_do_not_execute_and_environment_is_final_only(self):
        for mode in ('release', 'eof', 'expire'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / 'cgroup.procs').touch()
                marker = root / 'marker'
                config = os.memfd_create('test', os.MFD_CLOEXEC)
                os.write(config, json.dumps({'cwd': raw, 'argv': ['/bin/sh', '-c',
                    'printf "%s" "$SECRET" > marker; printf stdout; printf stderr >&2'],
                    'executable': '/bin/sh', 'env': {'SECRET': 'final-only'}}).encode())
                os.lseek(config, 0, 0)
                group = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                gr, gw = os.pipe()
                sr, sw = os.pipe()
                with open(root / 'out', 'wb') as out, open(root / 'err', 'wb') as err:
                    child = subprocess.Popen([sys.executable, '-I', app_launcher.__file__, str(group),
                        str(config), str(gr), str(sw), str(time.monotonic() + .25)],
                        pass_fds=(group, config, gr, sw), env={'PATH': '/usr/bin:/bin'}, stdout=out, stderr=err)
                for fd in (config, group, gr, sw):
                    os.close(fd)
                try:
                    self.assertTrue(select.select([sr], [], [], 2)[0])
                    self.assertEqual(os.read(sr, 1), b'R')
                    self.assertEqual((root / 'cgroup.procs').read_text(), str(child.pid))
                    self.assertFalse(marker.exists())
                    if mode == 'release':
                        os.write(gw, b'G')
                    if mode != 'expire':
                        os.close(gw)
                        gw = None
                    child.wait(timeout=2)
                    self.assertEqual(marker.exists(), mode == 'release')
                    if mode == 'release':
                        self.assertEqual(marker.read_text(), 'final-only')
                        self.assertEqual((root / 'out').read_text(), 'stdout')
                        self.assertEqual((root / 'err').read_text(), 'stderr')
                        self.assertEqual(os.read(sr, 32), b'X')
                finally:
                    if gw is not None:
                        os.close(gw)
                    os.close(sr)
                    if child.poll() is None:
                        child.kill()
                        child.wait()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'artifacts', 'default', GEN, create=True)
        self.children = Children()
        self.registry = object.__new__(Registry)
        self.registry.generation, self.registry.store = GEN, self.store
        self.registry.children = self.children
        self.registry.boot_id = 'boot-test'
        self.registry.active = None
        self.root_group = self.root / 'groups'
        self.root_group.mkdir()
        def reserve():
            self.registry.available()
            app_id = 'b' * 32
            group = self.root_group / app_id
            group.mkdir()
            (group / 'cgroup.events').write_text('populated 0\n')
            (group / 'cgroup.procs').write_text('')
            self.registry.active = Application(self.registry, app_id, os.open(group, os.O_RDONLY),
                                               group, '/owned/applications/' + app_id)
            return self.registry.active
        self.registry.reserve = reserve
        self.request = normalize(make_request('launch', session='default', expected_generation=GEN,
            caller_cwd=str(self.root), arguments={'argv': ['/bin/sleep', '5']}))
        self.token = self.store.request(self.request, time.monotonic(), time.monotonic() + 10)
        self.records = SimpleNamespace(token=lambda request_id: self.token)
        self.effects = []
        self.context = SimpleNamespace(work=SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + 10)),
                                       effects=lambda value, **kw: self.effects.append(value))
        self.desktop = SimpleNamespace(private={}, tick=lambda: None)
        self.task = LaunchTask(self.request, self.context, self.registry, self.desktop, self.records)
        self.compose = patch('agent_desktop.applications.compose', return_value={'PATH': '/usr/bin:/bin'})
        self.compose.start()
        self.member = patch('agent_desktop.app_processes.membership', side_effect=lambda pid:
            '/owned/applications/' + 'b' * 32)
        self.member.start()

    def tearDown(self):
        self.task.request_cancel('test')
        if self.task.app is not None and self.task.app.child is not None:
            self.task.app.child.abort()
            self.task.app.child.process.wait(timeout=2)
        if self.registry.active is not None:
            self.registry.active.close()
        self.children.close()
        self.member.stop()
        self.compose.stop()
        self.store.close()
        self.temp.cleanup()

    def advance(self, condition):
        until = time.monotonic() + 2
        while not condition():
            self.assertLess(time.monotonic(), until)
            self.task.step(time.monotonic())
            self.children.poll()
            time.sleep(.005)

    def test_effects_precede_release_and_cancel_retains_launched_child(self):
        self.advance(lambda: self.task.phase == 'exec')
        app = self.task.app
        self.assertTrue(self.effects)
        self.assertIsNotNone(app.process)
        self.assertEqual(json.loads((self.store.path / 'applications' / app.id / 'record.json').read_text())['state'],
                         'execution-authorized')
        self.task.request_cancel('cancelled')
        self.assertTrue(self.task.cleanup(time.monotonic()))
        self.assertIs(self.registry.active, app)
        self.assertIsNone(app.child.process.poll())
        self.assertTrue(app.authorized)

    def test_effect_record_failure_before_gate_aborts_without_target_effect(self):
        self.context.effects = lambda *a, **k: (_ for _ in ()).throw(ContractError('artifact_failed', 'test'))
        with self.assertRaises(ContractError):
            self.advance(lambda: self.task.phase == 'exec')
        self.assertFalse(self.task.app.authorized)
        self.task.request_cancel('failed')
        self.task.app.child.process.wait(timeout=2)
        self.assertNotEqual(self.task.app.child.returncode, 0)

    def test_second_launch_conflicts_even_after_root_exit_with_descendants(self):
        self.advance(lambda: self.task.phase == 'done')
        app = self.task.app
        (app.path / 'cgroup.events').write_text('populated 1\n')
        app.child.abort()
        app.child.process.wait(timeout=2)
        with self.assertRaises(ContractError) as caught:
            self.registry.available()
        self.assertEqual(caught.exception.code, 'application_active')
        self.assertEqual(app.state, 'root-exited')
        (app.path / 'cgroup.events').write_text('populated 0\n')
        self.registry.available()
        self.assertIsNone(self.registry.active)
        record = json.loads((self.store.path / 'applications' / app.id / 'record.json').read_text())
        self.assertEqual(record['state'], 'all-exited')
        self.assertEqual(record['exit_code'], -9)

    def test_missing_events_never_releases_slot(self):
        self.task.prepare()
        self.task.app.settled = True
        (self.task.app.path / 'cgroup.events').unlink()
        with self.assertRaises(FileNotFoundError):
            self.registry.available()
        self.assertIsNotNone(self.registry.active)

    def test_wait_window_is_rejected_before_allocating_or_spawning(self):
        self.request.arguments['wait_window'] = True
        with self.assertRaises(ContractError) as caught:
            self.task.prepare()
        self.assertEqual(caught.exception.code, 'unsupported_operation')
        self.assertIsNone(self.registry.active)
        self.assertFalse(self.children.owned)

    def test_prelaunch_artifact_failure_prevents_spawn(self):
        with patch.object(self.store, 'allocate', side_effect=ContractError('artifact_failed', 'test')):
            with self.assertRaises(ContractError):
                self.task.prepare()
        self.assertIsNone(self.registry.active)
        self.assertFalse(self.children.owned)

    def test_postlaunch_failure_retains_records_and_logs(self):
        self.advance(lambda: self.task.phase == 'done')
        app = self.task.app
        self.task.request_cancel('wait-failed')
        self.store.transition(self.token, 'terminal', outcome='partial', error_code='timeout',
                              references=app.snapshot())
        record = json.loads((self.store.path / 'requests' / self.token[0] / self.token[1] / 'record.json').read_text())
        self.assertEqual(record['references']['application'], app.handle)
        self.assertEqual(record['references']['logs'], app.logs)
        self.assertIsNone(app.child.process.poll())


if __name__ == '__main__':
    unittest.main()
