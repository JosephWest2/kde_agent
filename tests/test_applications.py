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
from unittest.mock import Mock, patch

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


class ObservationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'cgroup.events').write_text('populated 0\n')
        self.store = Mock()
        self.registry = object.__new__(Registry)
        self.registry.generation, self.registry.store, self.registry.boot_id = GEN, self.store, 'test-boot'
        self.app = Application(self.registry, 'b' * 32, os.open(self.root, os.O_RDONLY), self.root, '/owned')
        self.registry.active = self.app

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def handle(self):
        fd = os.pidfd_open(os.getpid())
        key = (os.getpid(), birth(os.getpid()))
        self.app.handles[key] = fd
        self.app.reap_queue.append(key)
        return key

    def test_completed_observation_survives_registry_retirement_and_repeated_reads(self):
        self.app.child = SimpleNamespace(returncode=7)
        self.app.authorized = self.app.settled = True
        self.app.observe(force=True)  # Registry, before any request consumer.
        self.assertIsNone(self.registry.active)
        self.assertIsNone(self.app.fd)
        with patch.object(self.app, 'populated', side_effect=AssertionError('Retired kernel handle reopened')):
            first = self.app.observe_exit()
            self.assertEqual(first, {'root_returncode': 7, 'all_exited': True, 'state': 'all-exited'})
            self.assertEqual(self.app.observe_exit(), first)

    def test_process_state_reports_actual_empty_bit_before_root_reaping(self):
        self.app.child = SimpleNamespace(returncode=None)
        self.app.settled = True
        self.app.observe(force=True)
        state = self.registry.application_process_state(self.app.handle)
        self.assertFalse(state['subtree_populated'])
        self.assertFalse(state['root_reaped'])
        self.assertFalse(state['all_exited'])
        self.assertIsNone(state['remaining_processes'])

    def test_cached_process_state_is_constant_size_and_uncertainty_is_explicit(self):
        (self.root / 'cgroup.events').write_text('populated 1\n')
        self.app.observe(force=True)
        # Historic identities are not current liveness or an enumeration.
        self.app.observed = {n: object() for n in range(4096)}
        with patch.object(self.app, 'populated', side_effect=AssertionError('unexpected read')):
            state = self.registry.application_process_state(self.app.handle)
            self.assertEqual(len(state), 7)
            self.assertTrue(state['subtree_populated'])
            self.assertEqual(state['enumeration'], 'unavailable')
            self.app.uncertain = True
            self.assertIsNone(self.registry.application_process_state(self.app.handle)['subtree_populated'])

    def test_pending_publication_blocks_completion_but_does_not_invent_populated(self):
        self.app.child = SimpleNamespace(returncode=7)
        self.app.authorized = self.app.settled = True
        self.app.pending_processes = [{'pid': 42}]
        self.app.observe(force=True)
        state = self.registry.application_process_state(self.app.handle)
        self.assertFalse(state['subtree_populated'])
        self.assertFalse(state['all_exited'])
        self.assertTrue(state['root_reaped'])
        self.assertEqual(state['root_returncode'], 7)
        self.assertIs(self.registry.active, self.app)

    def test_historical_process_completion_does_not_invent_observation_time(self):
        self.registry.active = None
        self.store.application_read.return_value = {'state': 'all-exited', 'exit_code': 7}
        state = self.registry.application_process_state(self.app.handle)
        self.assertTrue(state['all_exited'])
        self.assertFalse(state['subtree_populated'])
        self.assertIsNone(state['observed_at'])

    def test_live_launch_failed_is_not_retired_exit(self):
        self.app.state = 'launch-failed'
        self.app.settled = True
        self.app.child = SimpleNamespace(returncode=None)
        (self.root / 'cgroup.events').write_text('populated 1\n')
        snapshot, exited = self.registry.observe_application_exit(self.app.handle, force=True)
        self.assertEqual(snapshot['state'], 'launch-failed')
        self.assertFalse(exited)
        self.assertIs(self.registry.active, self.app)

    def test_retained_terminal_records_are_readonly_but_nonterminal_fails_closed(self):
        self.registry.active = None
        for state in ('all-exited', 'launch-failed', 'running'):
            self.store.application_read.return_value = {'state': state, 'exit_code': 7}
            if state == 'running':
                with self.assertRaises(ContractError) as caught:
                    self.registry.observe_application_exit(self.app.handle)
                self.assertEqual(caught.exception.code, 'session_failed')
            else:
                snapshot, exited = self.registry.observe_application_exit(self.app.handle)
                self.assertTrue(exited)
                self.assertEqual(snapshot['exit_code'], 7)

    def test_close_without_empty_observation_preserves_uncertainty(self):
        self.app.close()
        self.assertFalse(self.app.completed)
        with self.assertRaises(ContractError) as caught:
            self.app.observe_exit()
        self.assertEqual(caught.exception.outcome, 'unknown')

    def test_registry_tick_skips_scan_after_lifetime_observation_exhausts_budget(self):
        now = [1.0]
        def slow_observation():
            now[0] += .003
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.app, 'observe', side_effect=slow_observation), \
                patch.object(self.app, 'scan_turn', wraps=self.app.scan_turn) as scanning:
            Registry.tick(self.registry)
        scanning.assert_not_called()

    def test_registry_scan_uses_only_budget_remaining_after_lifetime_observation(self):
        self.handle()
        self.app.scanner = (pid for pid in [123])
        now = [1.0]
        def observation():
            now[0] += .001
        def delayed_poll(fd):
            now[0] += .0015
            return True
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.app, 'observe', side_effect=observation), \
                patch('agent_desktop.app_processes.live', side_effect=delayed_poll), \
                patch('agent_desktop.app_processes.identity') as acquire:
            Registry.tick(self.registry)
        acquire.assert_not_called()
        self.assertIsNone(self.app.pending_pid)
        self.store.application_processes.assert_not_called()

    def test_budget_exhausted_before_work_schedules_no_poll_or_snapshot(self):
        self.handle()
        self.app.dirty = True
        self.app.scanner = (pid for pid in [123])
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=[1, 1.003]), \
                patch('agent_desktop.app_processes.live') as polling:
            self.app.scan_turn()
        polling.assert_not_called()
        self.store.application_update.assert_not_called()
        self.assertTrue(self.app.dirty)

    def test_budget_exhausted_during_reaping_defers_scan_and_snapshot(self):
        key = self.handle()
        self.app.dirty = True
        now = [1.0]
        def delayed_poll(fd):
            now[0] += .003
            return True
        def scanning():
            raise AssertionError('Scan began after budget expired')
            yield
        self.app.scanner = scanning()
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch('agent_desktop.app_processes.live', side_effect=delayed_poll):
            self.app.scan_turn()
        self.assertEqual(list(self.app.reap_queue), [key])
        self.store.application_update.assert_not_called()
        self.assertTrue(self.app.dirty)

    def test_slow_iterator_retains_member_without_scheduling_identity_acquisition(self):
        now = [1.0]
        def scanning():
            now[0] += .003
            yield 123
        self.app.scanner = scanning()
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch('agent_desktop.app_processes.identity') as acquire:
            self.app.scan_turn()
        acquire.assert_not_called()
        self.assertEqual(self.app.pending_pid, 123)
        self.store.application_processes.assert_not_called()

    def test_slow_acquisition_preserves_batch_until_later_turn_and_completion(self):
        now = [1.0]
        fd = os.pidfd_open(os.getpid())
        info = {'pid': os.getpid(), 'start_time_ticks': birth(os.getpid()), 'cgroup': '/owned'}
        def acquire(*args):
            now[0] += .003
            return fd, info
        self.app.scanner = (pid for pid in [os.getpid()])
        self.app.dirty = True
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch('agent_desktop.app_processes.identity', side_effect=acquire):
            self.app.scan_turn()
        self.assertEqual(len(self.app.pending_processes), 1)
        self.store.application_processes.assert_not_called()
        self.store.application_update.assert_not_called()
        self.app.child = SimpleNamespace(returncode=7)
        self.app.authorized = self.app.settled = True
        self.app.observe(force=True)
        self.assertIs(self.registry.active, self.app)
        self.assertFalse(self.app.completed)
        batches = []
        self.store.application_processes.side_effect = lambda app_id, batch: batches.append(list(batch))
        now[0] = 2
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]):
            self.app.scan_turn()
        self.assertEqual(batches, [[info]])
        self.assertEqual(self.app.pending_processes, [])
        self.assertFalse(self.app.dirty)
        self.app.observe(force=True)
        self.assertTrue(self.app.completed)
        self.assertIsNone(self.registry.active)

    def test_slow_batch_write_defers_poll_and_dirty_snapshot_without_duplicate_batch(self):
        self.handle()
        self.app.dirty = True
        self.app.pending_processes = [{'pid': 123}]
        now = [1.0]
        batches = []
        def write(app_id, batch):
            batches.append(list(batch))
            now[0] += .003
        self.store.application_processes.side_effect = write
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]), \
                patch('agent_desktop.app_processes.live') as polling:
            self.app.scan_turn()
        polling.assert_not_called()
        self.assertEqual(batches, [[{'pid': 123}]])
        self.assertEqual(self.app.pending_processes, [])
        self.store.application_update.assert_not_called()
        self.assertTrue(self.app.dirty)
        self.app.scanner = (pid for pid in [])
        now[0] = 2
        with patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: now[0]):
            self.app.scan_turn()
        self.assertEqual(len(batches), 1)
        self.assertFalse(self.app.dirty)

    def test_failed_batch_publication_retains_bounded_pending_work(self):
        batch = [{'pid': number} for number in range(16)]
        self.app.pending_processes = batch.copy()
        self.store.application_processes.side_effect = OSError('storage unavailable')
        with self.assertRaises(OSError):
            self.app.scan_turn()
        self.assertEqual(self.app.pending_processes, batch)


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
        self.task.request_cancel('cancelled')
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

    def test_protocol_loss_after_authorization_is_unknown_and_retained(self):
        self.advance(lambda: self.task.phase == 'exec')
        app = self.task.app
        app.child.abort()
        app.child.process.wait(timeout=2)
        with self.assertRaises(ContractError) as caught:
            self.advance(lambda: self.task.phase == 'done')
        self.assertEqual(caught.exception.code, 'completion_unknown')
        self.assertTrue(app.authorized)
        self.assertIs(self.registry.active, app)

    def test_gate_timeout_aborts_without_authorization(self):
        self.task.prepare()
        self.task.handshake = time.monotonic() - 1
        with self.assertRaises(ContractError) as caught:
            self.task.step(time.monotonic())
        self.assertEqual(caught.exception.code, 'timeout')
        self.task.request_cancel('timeout')
        self.assertFalse(self.task.app.authorized)
        self.task.app.child.process.wait(timeout=2)

    def test_target_inherits_only_standard_descriptors(self):
        self.advance(lambda: self.task.phase == 'done')
        paths = sorted(int(path.name) for path in Path('/proc', str(self.task.app.child.process.pid), 'fd').iterdir())
        self.assertEqual(paths, [0, 1, 2])

    def test_scheduler_exception_calls_cleanup_and_retains_real_effects(self):
        from agent_desktop.scheduler import Scheduler
        from agent_desktop.records import Records
        from agent_desktop.contracts import response
        class Admission:
            def __init__(self, request):
                self.request = request
                self.admitted_at = time.monotonic()
                self.deadline = self.admitted_at + 10
                self.disconnected = False
                self.on_terminal = None
                self.final = None
            def complete(self, result=None, error=None):
                self.final = response(self.request.request_id, self.request.operation, session='default',
                                      generation=GEN, result=result, error=error)
                if self.on_terminal:
                    self.on_terminal(self.final)
        admission = Admission(self.request)
        records = Records(self.store)
        records.attach(self.request, admission)
        def factory(req, context):
            self.task = LaunchTask(req, context, self.registry, self.desktop, records)
            original = self.task.step
            def failing(now):
                result = original(now)
                if result is not None:
                    raise ContractError('timeout', 'Injected post-launch wait failure.')
            self.task.step = failing
            return self.task
        scheduler = Scheduler(factory=records.factory(factory), observer=records.observe, children=self.children)
        scheduler.submit(self.request, admission)
        until = time.monotonic() + 2
        while admission.final is None:
            self.assertLess(time.monotonic(), until)
            scheduler.tick()
            time.sleep(.005)
        self.assertEqual(admission.final['error']['code'], 'timeout')
        self.assertEqual(admission.final['error']['outcome'], 'partial')
        refs = admission.final['error']['partial_result']
        self.assertEqual(refs['application'], self.task.app.handle)
        self.assertEqual(refs['logs'], self.task.app.logs)
        self.assertIsNone(self.task.app.child.process.poll())
        self.assertTrue(self.task.cancelled)
        self.assertIsNone(self.task.gate)
        self.assertIsNone(self.task.status)
        self.assertNotIn(self.request.request_id, records.live)

    def test_wait_window_shares_launch_deadline_and_retains_application_on_cancel(self):
        self.request.arguments['wait_window'] = True
        self.advance(lambda: self.task.phase == 'window_wait')
        self.assertEqual(self.task.window_wait.deadline, self.context.work.admission.deadline)
        self.assertIsNone(self.task.handshake)
        self.assertIsNone(self.task.status)
        self.task.request_cancel('cancelled')
        self.assertIsNone(self.task.app.child.process.poll())
        self.assertEqual(self.effects[-1]['application'], self.task.app.handle)
        self.assertEqual(self.effects[-1]['logs'], self.task.app.logs)
        self.assertIs(self.registry.active, self.task.app)

    def test_record_failure_after_exec_ack_retains_known_partial_launch(self):
        self.advance(lambda: self.task.phase == 'exec')
        self.context.effects = Mock()
        with patch.object(self.task.app, 'persist', side_effect=ContractError('artifact_failed', 'record')):
            with self.assertRaises(ContractError) as caught:
                self.advance(lambda: self.task.phase == 'done')
        self.assertEqual(caught.exception.code, 'artifact_failed')
        self.assertTrue(self.task.exec_confirmed)
        self.task.request_cancel('artifact_failed')
        self.assertFalse(self.context.effects.call_args.kwargs['uncertain'])
        self.assertEqual(self.context.effects.call_args.args[0]['application'], self.task.app.handle)
        self.assertIsNone(self.task.app.child.process.poll())

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
