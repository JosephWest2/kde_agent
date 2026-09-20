"""Explicit kill authority, retained completion and finite dispatch boundaries."""
from collections import deque
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent_desktop.app_processes import Application, Registry, Termination, birth, live
from agent_desktop.artifacts import safe_projection
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.terminating import KillTask

GEN = 'a' * 32
APP = {'generation': GEN, 'application_id': 'b' * 32}


class TerminationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / 'cgroup.events').write_text('populated 1\n')
        (self.path / 'cgroup.procs').write_text('')
        self.registry = object.__new__(Registry)
        self.registry.generation, self.registry.boot_id = GEN, 'boot'
        self.registry.store = Mock()
        self.registry.identity_index, self.registry.identity_revision = {}, 0
        self.app = Application(self.registry, APP['application_id'], os.open(self.path, os.O_RDONLY), self.path, '/owned/app')
        self.registry.active = self.app
        self.app.authorized = self.app.settled = True
        self.app.child = NS(returncode=None)
        self.addCleanup(self.app.close)
        self.clock = [10.]
        timer = patch('agent_desktop.app_processes.time.monotonic', side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        self.effects = []
        self.context = NS(work=NS(admission=NS(deadline=15.), error=None, observing_failed=False, terminal=False),
                          owner=NS(stopping=False, unavailable=False), effects=self.effect)
        self.healthy = Mock()
        self.request = make_request('kill', session='default', caller_cwd='/', expected_generation=GEN, arguments={'app': APP})
        self.task = KillTask(self.request, self.context, self.registry, self.healthy)
    def effect(self, value, **kwargs):
        self.effects.append(copy.deepcopy(value))
    def acquire(self, pid=123, ticks=456, fd=800):
        key = (pid, ticks)
        self.app.handles[key] = fd
        self.app.observed[key] = {'pid': pid, 'start_time_ticks': ticks}
        self.app.reap_queue.append(key)
        self.registry.index_add(self.app, key)
        return key, fd
    def start(self):
        self.task.step(self.clock[0])
        return self.task.cursor
    def visit(self, cursor, key, fd, group=None):
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                patch('agent_desktop.app_processes.membership', return_value=group or self.app.cgroup), \
                patch('signal.pidfd_send_signal') as sent:
            cursor.prepare()
            cursor.visit(key, fd)
            return sent
    def fake_handles_cleanup(self):
        self.app.handles.clear()
        self.app.reap_queue.clear()
        self.registry.identity_index.clear()
    def test_construction_cancel_before_start_and_cleanup_have_no_authority(self):
        self.assertIsNone(self.app.termination)
        self.task.request_cancel('cancelled')
        with self.assertRaises(ContractError):
            self.task.step(10)
        self.assertTrue(self.task.cleanup(10))
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.app.termination)
    def test_generation_and_expiry_precede_lookup_or_effect(self):
        for changed in ('request', 'handle', 'registry', 'expiry'):
            with self.subTest(changed=changed), patch.object(self.registry, 'lookup') as lookup:
                request = make_request('kill', session='default', caller_cwd='/', expected_generation=GEN, arguments={'app': APP})
                task = KillTask(request, self.context, self.registry, self.healthy)
                if changed == 'request':
                    task.request = NS(expected_generation='c' * 32)
                elif changed == 'handle':
                    task.application = APP | {'generation': 'c' * 32}
                elif changed == 'registry':
                    self.registry.generation = 'c' * 32
                else:
                    self.clock[0] = 15
                with self.assertRaises(ContractError) as error:
                    task.step(10)
                self.assertEqual(error.exception.code, 'timeout' if changed == 'expiry' else 'generation_mismatch')
                lookup.assert_not_called()
                self.registry.generation = GEN
        self.assertEqual(self.effects, [])
    def test_malformed_unknown_and_unowned_historical_never_attach(self):
        for value, error in (('../escape', 'target_not_found'), ('c' * 32, 'target_not_found')):
            self.task.application = APP | {'application_id': value}
            self.registry.store.application_read.side_effect = ContractError('target_not_found', 'missing')
            with self.assertRaises(ContractError) as caught:
                self.task.step(10)
            self.assertEqual(caught.exception.code, error)
            self.assertIsNone(self.app.termination)
            self.task.error = None
        self.task.application = APP | {'application_id': 'c' * 32}
        self.registry.store.application_read.side_effect = None
        self.registry.store.application_read.return_value = {'state': 'running'}
        with self.assertRaises(ContractError) as caught:
            self.task.step(10)
        self.assertEqual(caught.exception.code, 'session_failed')
    def test_completed_old_handle_is_noop_while_new_app_lives(self):
        self.task.application = APP | {'application_id': 'c' * 32}
        self.registry.store.application_read.return_value = {'state': 'all-exited', 'exit_code': -9}
        result = self.task.step(10)
        self.assertTrue(result['already_exited'])
        self.assertTrue(result['exited'])
        self.assertEqual(result['remaining_processes'], [])
        self.assertEqual(result['exit_status'], -9)
        self.assertEqual(sum(result['kill_state']['counts'].values()), 0)
        self.assertIs(self.registry.active, self.app)
        self.assertIsNone(self.app.termination)
    def test_durable_intent_and_phase_before_signal_at_most_once_each(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        self.assertTrue(self.effects[0]['kill_state']['intent'])
        sent = self.visit(cursor, key, fd)
        sent.assert_called_once_with(fd, signal.SIGTERM, None, 0)
        self.assertEqual(self.effects[-1]['kill_state']['phase'], 'term')
        self.visit(cursor, key, fd).assert_not_called()
        self.clock[0] = cursor.term_cutoff
        self.visit(cursor, key, fd).assert_called_once_with(fd, signal.SIGKILL, None, 0)
        self.visit(cursor, key, fd).assert_not_called()
        self.assertEqual(cursor.counts['term_submitted'], 1)
        self.assertEqual(cursor.counts['kill_submitted'], 1)
    def test_new_lifetime_at_kill_phase_gets_no_term_grace(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        self.clock[0] = cursor.term_cutoff + .01
        self.visit(cursor, key, fd).assert_called_once_with(fd, signal.SIGKILL, None, 0)
        second, fd2 = self.acquire(124, 457, 801)
        self.visit(cursor, second, fd2).assert_called_once_with(fd2, signal.SIGKILL, None, 0)
        self.assertEqual(cursor.counts['term_submitted'], 0)
    def test_global_cutoffs_include_resolution_and_phase_persistence_time(self):
        self.clock[0] = 14.
        cursor = self.start()
        self.assertAlmostEqual(cursor.term_cutoff, 14 + 1 / 3)
        self.assertEqual(cursor.signal_cutoff, 14.8)
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        self.clock[0] = cursor.signal_cutoff
        self.visit(cursor, key, fd).assert_not_called()
        self.assertEqual(cursor.phase, 'observe')
        self.clock[0] = 15
        cursor.prepare()
        self.assertTrue(cursor.revoked)
        self.assertEqual(cursor.error.code, 'timeout')
    def test_slow_identity_and_phase_persistence_cannot_cross_cutoff(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        cursor.prepare()
        def slow_birth(pid):
            self.clock[0] = cursor.term_cutoff
            return key[1]
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', side_effect=slow_birth), \
                patch('agent_desktop.app_processes.membership', return_value=self.app.cgroup), \
                patch('signal.pidfd_send_signal') as sent:
            cursor.visit(key, fd)
        sent.assert_not_called()
        self.context.effects = lambda *a, **k: self.clock.__setitem__(0, cursor.signal_cutoff)
        self.visit(cursor, key, fd).assert_not_called()
    def test_exact_subtree_and_foreign_prefix_supervisor_control_membership(self):
        for group, valid in (('/owned/app', True), ('/owned/app/child', True), ('/owned/app-extra', False),
                             ('/owned/supervisor', False), ('/owned/.control', False), ('/owned/sibling', False), ('/foreign', False)):
            with self.subTest(group=group):
                self.app.uncertain = False
                key, fd = self.acquire()
                cursor = Termination(self.app, 15, 11, 14.75, lambda: None, lambda: None)
                self.app.termination = cursor
                sent = self.visit(cursor, key, fd, group)
                self.assertEqual(sent.call_count, int(valid))
                if not valid:
                    self.assertEqual(cursor.error.code, 'session_failed')
                    self.assertIsNotNone(cursor.ownership_uncertain)
                    self.assertEqual(cursor.samples, {})
                cursor.detach()
                self.fake_handles_cleanup()
    def test_external_guard_runs_before_final_membership_verification(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        cursor.prepare()
        group = [self.app.cgroup]
        self.healthy.side_effect = lambda: group.__setitem__(0, '/foreign')
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                patch('agent_desktop.app_processes.membership', side_effect=lambda pid: group[0]), \
                patch('signal.pidfd_send_signal') as sent:
            cursor.visit(key, fd)
        sent.assert_not_called()
        self.assertTrue(cursor.revoked)
    def test_same_fd_reacquisition_revision_during_external_guard_cannot_be_blessed(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        cursor.prepare()
        self.healthy.side_effect = lambda: self.registry.index_add(self.app, key)
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                patch('agent_desktop.app_processes.membership', return_value=self.app.cgroup), \
                patch('signal.pidfd_send_signal') as sent:
            cursor.visit(key, fd)
        sent.assert_not_called()
    def test_esrch_is_one_attempt_while_permission_error_revokes(self):
        for error in (ProcessLookupError(), PermissionError()):
            with self.subTest(error=type(error).__name__):
                key, fd = self.acquire()
                cursor = Termination(self.app, 15, 11, 14.75, lambda: None, lambda: None)
                self.app.termination = cursor
                cursor.prepare()
                with patch('agent_desktop.app_processes.live', return_value=True), \
                        patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                        patch('agent_desktop.app_processes.membership', return_value=self.app.cgroup), \
                        patch('signal.pidfd_send_signal', side_effect=error) as sent:
                    cursor.visit(key, fd)
                    cursor.visit(key, fd)
                self.assertEqual(sent.call_count, 1)
                self.assertEqual(cursor.counts['term_attempted'], 1)
                self.assertEqual(cursor.counts['term_submitted'], 0)
                self.assertEqual(cursor.counts['raced_exit'], int(isinstance(error, ProcessLookupError)))
                self.assertEqual(cursor.revoked, isinstance(error, PermissionError))
                cursor.detach()
                self.fake_handles_cleanup()
    def test_birth_change_missing_proc_and_dead_lifetime_never_signal(self):
        for mode in ('before', 'after', 'missing', 'dead'):
            with self.subTest(mode=mode):
                key, fd = self.acquire()
                cursor = Termination(self.app, 15, 11, 14.75, lambda: None, lambda: None)
                self.app.termination = cursor
                cursor.prepare()
                births = [0, key[1]] if mode == 'before' else [key[1], 0] if mode == 'after' else FileNotFoundError()
                with patch('agent_desktop.app_processes.live', return_value=mode != 'dead'), \
                        patch('agent_desktop.app_processes.birth', side_effect=births), \
                        patch('agent_desktop.app_processes.membership', return_value=self.app.cgroup), \
                        patch('signal.pidfd_send_signal') as sent:
                    cursor.visit(key, fd)
                sent.assert_not_called()
                cursor.detach()
                self.app.uncertain = False
                self.fake_handles_cleanup()
    def test_descriptor_replacement_or_index_revision_during_check_is_rejected(self):
        for mutation in ('fd', 'revision', 'revoke'):
            with self.subTest(mutation=mutation):
                key, fd = self.acquire()
                cursor = Termination(self.app, 15, 11, 14.75, lambda: None, lambda: None)
                self.app.termination = cursor
                cursor.prepare()
                def change(pid):
                    if mutation == 'fd':
                        self.app.handles[key] = 999
                    elif mutation == 'revision':
                        self.registry.index_add(self.app, key)
                    else:
                        cursor.detach()
                    return self.app.cgroup
                with patch('agent_desktop.app_processes.live', return_value=True), \
                        patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                        patch('agent_desktop.app_processes.membership', side_effect=change), \
                        patch('signal.pidfd_send_signal') as sent:
                    cursor.visit(key, fd)
                sent.assert_not_called()
                cursor.detach()
                self.fake_handles_cleanup()
    def test_cancel_revokes_before_diagnostics_and_preserves_original_cause(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        self.visit(cursor, key, fd)
        def broken(*a, **k):
            self.assertTrue(cursor.revoked)
            self.assertIsNone(self.app.termination)
            raise OSError('diagnostic failed')
        self.context.effects = broken
        self.context.work.error = ContractError('cancelled', 'original')
        self.task.request_cancel('timeout')
        self.assertEqual(self.task.error.message, 'original')
        self.clock[0] = cursor.term_cutoff
        self.visit(cursor, key, fd).assert_not_called()
        self.assertTrue(self.task.cleanup(11))
    def test_context_cancel_stop_and_artifact_flags_block_registry_before_scheduler(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        for flag in ('error', 'observing_failed', 'terminal', 'stopping', 'unavailable'):
            with self.subTest(flag=flag):
                task = KillTask(self.request, self.context, self.registry, self.healthy)
                task.step(10)
                cursor = task.cursor
                target = self.context.owner if flag in ('stopping', 'unavailable') else self.context.work
                setattr(target, flag, ContractError('cancelled', 'cancel') if flag == 'error' else True)
                self.visit(cursor, key, fd).assert_not_called()
                self.assertTrue(cursor.revoked)
                setattr(target, flag, None if flag == 'error' else False)
    def test_intent_and_phase_artifact_failures_prevent_dispatch(self):
        self.context.effects = Mock(side_effect=ContractError('artifact_failed', 'disk'))
        with self.assertRaises(ContractError):
            self.task.step(10)
        self.assertIsNone(self.app.termination)
        self.context.effects = self.effect
        self.task = KillTask(self.request, self.context, self.registry, self.healthy)
        cursor = self.start()
        self.context.effects = Mock(side_effect=ContractError('artifact_failed', 'disk'))
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        self.visit(cursor, key, fd).assert_not_called()
        self.assertEqual(cursor.error.code, 'artifact_failed')
    def test_positive_exit_detaches_before_success_and_preserves_observed_time(self):
        cursor = self.start()
        self.app.child.returncode = -15
        (self.path / 'cgroup.events').write_text('populated 0\n')
        self.app.observe(force=True)
        self.registry.store.application_read.return_value = self.app.snapshot()
        self.clock[0] = 10.01
        result = self.task.step(10.01)
        self.assertEqual(result['kill_state']['exit_observed_at'], 10)
        self.assertEqual(result['remaining_processes'], [])
        self.assertTrue(cursor.revoked)
        self.assertEqual(result['exit_status'], -15)
    def test_late_persistence_cannot_turn_completion_into_success(self):
        self.start()
        self.app.child.returncode = 0
        (self.path / 'cgroup.events').write_text('populated 0\n')
        self.app.observe(force=True)
        self.registry.store.application_read.return_value = self.app.snapshot()
        self.context.effects = lambda *a, **k: self.clock.__setitem__(0, 15.)
        with self.assertRaises(ContractError) as caught:
            self.task.step(10)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertTrue(self.task.cursor.revoked)
    def test_missing_pidfd_signal_prerequisite_has_no_intent(self):
        with patch('signal.pidfd_send_signal', None), self.assertRaises(ContractError) as caught:
            self.task.step(10)
        self.assertEqual(caught.exception.code, 'prerequisite_missing')
        self.assertEqual(self.effects, [])
    def test_unknown_empty_sample_is_not_zero_remaining_and_samples_are_bounded(self):
        cursor = self.start()
        self.assertIsNone(self.task.projection()['remaining_processes'])
        for number in range(70):
            key, fd = self.acquire(100 + number, 456 + number, 800 + number)
            self.visit(cursor, key, fd)
        self.addCleanup(self.fake_handles_cleanup)
        result = self.task.projection()
        self.assertEqual(len(result['remaining_processes']), 64)
        self.assertTrue(result['kill_state']['enumeration_incomplete'])
        self.assertTrue(result['kill_state']['sample_truncated'])
        self.assertIsNone(result['exited'])  # No populated observation has occurred.
        self.assertLess(len(json.dumps(safe_projection(result, GEN))), 12000)
    def test_typed_projection_rejects_boolean_numbers_and_arbitrary_state(self):
        value = {'kill_state': {'phase': 'term', 'deadline': True, 'counts': {'term_attempted': True},
            'remaining_processes': [{'pid': True, 'start_time_ticks': 5, 'observed_at': 2}],
            'ownership_uncertain': {'pid': 2, 'start_time_ticks': 5, 'observed_at': float('nan')},
            'secret': 'discard', 'enumeration': 'sampled'}, 'exit_status': True}
        result = safe_projection(value, GEN)
        self.assertNotIn('deadline', result['kill_state'])
        self.assertNotIn('secret', result['kill_state'])
        self.assertNotIn('exit_status', result)
        self.assertEqual(result['kill_state']['counts'], {})
        self.assertEqual(result['kill_state']['remaining_processes'], [])
    def test_forced_observation_cannot_discard_live_migrated_member(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        self.app.child.returncode = 0
        (self.path / 'cgroup.events').write_text('populated 0\n')
        self.app.observe(force=True)
        self.assertFalse(self.app.completed)
        self.assertFalse(self.app.process_state['subtree_populated'])
        self.assertFalse(self.app.process_state['all_exited'])
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', return_value=key[1]), \
                patch('agent_desktop.app_processes.membership', return_value='/foreign'), \
                patch('signal.pidfd_send_signal') as sent, self.assertRaises(ContractError):
            self.registry.tick()
        sent.assert_not_called()
        self.assertTrue(self.app.uncertain)
        self.assertFalse(self.app.completed)
        self.assertEqual(self.app.handles[key], fd)
        self.assertIs(self.registry.active, self.app)
        self.assertIn(key, self.app.reap_queue)
        self.assertTrue(cursor.revoked)
        self.assertEqual(cursor.ownership_uncertain['pid'], key[0])
        self.assertNotIn(key, cursor.samples)
    def test_4096_dead_handles_and_stale_queue_settle_incrementally(self):
        for number in range(4096):
            self.acquire(number + 1, number + 1, number + 10000)
        self.addCleanup(self.fake_handles_cleanup)
        self.app.reap_queue.extend([(99999, 1)] * 32)
        self.app.child.returncode = 7
        (self.path / 'cgroup.events').write_text('populated 0\n')
        with patch('agent_desktop.app_processes.live', return_value=False), patch('os.close') as closed:
            self.app.observe(force=True)
            self.assertEqual(closed.call_count, 1)  # cgroup.events only.
            for turn in range(258):
                before = len(self.app.handles)
                self.app.scan_turn(deadline=11)
                self.assertLessEqual(before - len(self.app.handles), 16)
                self.app.observe(force=True)
                if turn < 257:
                    self.assertFalse(self.app.completed)
            self.assertTrue(self.app.completed)
            counts = [call.args[0] for call in closed.call_args_list]
            self.assertEqual(sum(fd >= 10000 for fd in counts), 4096)
        self.assertEqual(self.registry.identity_index, {})
        self.assertEqual(self.app.reap_queue, deque())
        self.assertIsNone(self.registry.active)
    def test_cancel_during_settlement_keeps_registry_owner_for_later_wait(self):
        key, fd = self.acquire()
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        self.app.child.returncode = 0
        (self.path / 'cgroup.events').write_text('populated 0\n')
        self.app.observe(force=True)
        self.task.request_cancel('cancelled')
        self.assertTrue(cursor.revoked)
        with patch('agent_desktop.app_processes.live', return_value=False), patch('os.close'):
            self.app.scan_turn(deadline=11)
        snapshot, complete = self.registry.observe_application_exit(APP, force=True)
        self.assertTrue(complete)
        self.assertEqual(snapshot['exit_code'], 0)
    def test_signal_attempt_cap_and_discovery_fairness_under_one_deadline(self):
        for number in range(100):
            self.acquire(number + 1, 456, number + 800)
        self.addCleanup(self.fake_handles_cleanup)
        cursor = self.start()
        with patch('agent_desktop.app_processes.live', return_value=True), \
                patch('agent_desktop.app_processes.birth', return_value=456), \
                patch('agent_desktop.app_processes.membership', return_value=self.app.cgroup), \
                patch('signal.pidfd_send_signal') as sent:
            self.registry.tick()
            self.assertEqual(sent.call_count, 16)
        def slow_visit(*args):
            self.clock[0] += .003
        with patch.object(cursor, 'visit', side_effect=slow_visit), \
                patch('agent_desktop.app_processes.live', return_value=True), \
                patch.object(self.app, 'discover_turn', wraps=self.app.discover_turn) as discover:
            self.registry.tick()
            self.registry.tick()
            self.assertEqual(discover.call_count, 2)
            self.assertEqual(len(set(call.kwargs.get('deadline', call.args[0]) for call in discover.call_args_list)), 2)


class RealPidfdTests(unittest.TestCase):
    def test_dead_original_with_simulated_reused_pid_never_signals_live_sentinel(self):
        original = subprocess.Popen(['/bin/sleep', '10'])
        sentinel = subprocess.Popen(['/bin/sleep', '10'])
        fd = os.pidfd_open(original.pid)
        try:
            original.terminate()
            original.wait(timeout=2)
            # Deterministic reuse presentation: original pidfd, unrelated live
            # numeric PID. No claim that the kernel actually recycled a PID.
            registry = NS(active=None, identity_index={}, generation=GEN)
            key = (sentinel.pid, birth(sentinel.pid))
            app = NS(registry=registry, handles={key: fd}, signal_marks={}, completed=False,
                     fd=1, uncertain=False, cgroup='/owned')
            registry.active = app
            registry.identity_index[key[0]] = (app, key, 1)
            cursor = Termination(app, time.monotonic() + 2, time.monotonic() + 1, time.monotonic() + 1.5,
                                 lambda: None, lambda: None)
            app.termination = cursor
            with patch('agent_desktop.app_processes.membership', return_value='/owned'), \
                    patch('os.kill', side_effect=AssertionError('raw PID authority')), \
                    patch('os.killpg', side_effect=AssertionError('process group authority')), \
                    patch('signal.pidfd_send_signal', wraps=signal.pidfd_send_signal) as sent:
                cursor.prepare()
                cursor.visit(key, fd)
            sent.assert_not_called()
            self.assertIsNone(sentinel.poll())
        finally:
            os.close(fd)
            for child in (original, sentinel):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
    def test_real_retained_pidfd_delivers_term_and_root_is_reaped_only_by_child_owner(self):
        child = subprocess.Popen(['/bin/sleep', '10'])
        fd = os.pidfd_open(child.pid)
        try:
            key = (child.pid, birth(child.pid))
            registry = NS(active=None, identity_index={}, generation=GEN)
            app = NS(registry=registry, handles={key: fd}, signal_marks={}, completed=False,
                     fd=1, uncertain=False, cgroup='/owned')
            registry.active = app
            registry.identity_index[key[0]] = (app, key, 1)
            cursor = Termination(app, time.monotonic() + 2, time.monotonic() + 1, time.monotonic() + 1.5,
                                 lambda: None, lambda: None)
            app.termination = cursor
            with patch('agent_desktop.app_processes.membership', return_value='/owned'), \
                    patch('os.kill', side_effect=AssertionError('raw PID authority')), \
                    patch('os.waitpid', side_effect=AssertionError('second reaper')), \
                    patch('os.waitid', side_effect=AssertionError('second reaper')):
                cursor.prepare()
                cursor.visit(key, fd)
            self.assertEqual(child.wait(timeout=2), -signal.SIGTERM)
            self.assertFalse(live(fd))
            self.assertEqual(cursor.counts['term_submitted'], 1)
        finally:
            os.close(fd)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
