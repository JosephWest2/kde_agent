"""Generation-bound finalization and PID-lifetime-safe survivor cleanup."""
import fcntl
import json
import os
from pathlib import Path
import signal
import time
import unittest
from unittest.mock import patch

import test_lifecycle as lifecycle_tests
from agent_desktop.contracts import ContractError
from agent_desktop.lifecycle import atomic, read_metadata, Systemd, unit_quote
from agent_desktop.ownership import generation_lock, intent
from agent_desktop.runtime import Runtime
from agent_desktop.service_cleanup import finalize, classify, terminate_survivors, relay


class FinalizerTests(unittest.TestCase):
    setUp = lifecycle_tests.LifecycleTests.setUp
    tearDown = lifecycle_tests.LifecycleTests.tearDown
    request = lifecycle_tests.LifecycleTests.request
    start = lifecycle_tests.LifecycleTests.start

    def setup_generation(self):
        generation = self.start()
        runtime = Runtime()
        data = read_metadata(runtime, 'default', generation)
        root = runtime.generations / generation
        (root / 'desktop').mkdir(mode=0o700)
        return runtime, data, root

    def test_intent_write_failure_still_submits_fallback_with_uncertain_provenance(self):
        runtime, data, root = self.setup_generation()
        with patch('agent_desktop.ownership.intent', side_effect=PermissionError('intent read-only')):
            result = self.manager.handle(self.request('session.stop'))
        self.assertIn(('stop', data['unit']), self.services.calls)
        self.assertFalse(self.services.active[data['unit']])
        self.assertEqual(result['result']['cleanup'], 'complete')
        self.assertFalse(result['result']['records_preserved'])
        self.assertEqual(result['result']['state'], 'failed')
        self.assertFalse((root / 'stop-intent.json').exists())

    def test_bookkeeping_failure_never_bypasses_current_generation_recheck(self):
        runtime, data, root = self.setup_generation()
        def changed(*args):
            atomic(runtime.current / 'default.json', {'schema_version': 1, 'session': 'default',
                                                     'generation': 'f' * 32})
            raise PermissionError('intent failed during replacement')
        with patch('agent_desktop.ownership.intent', side_effect=changed), self.assertRaises(ContractError) as caught:
            self.manager.handle(self.request('session.stop'))
        self.assertEqual(caught.exception.code, 'generation_mismatch')
        self.assertNotIn(('stop', data['unit']), self.services.calls)
        self.assertTrue(self.services.active[data['unit']])

    def test_metadata_write_failure_still_submits_fallback(self):
        runtime, data, root = self.setup_generation()
        with patch.object(self.manager, '_write', side_effect=PermissionError('metadata read-only')):
            result = self.manager.handle(self.request('session.stop'))
        self.assertIn(('stop', data['unit']), self.services.calls)
        self.assertFalse(self.services.active[data['unit']])
        self.assertEqual(result['result']['cleanup'], 'complete')
        self.assertFalse(result['result']['records_preserved'])
        self.assertEqual(result['result']['state'], 'stopped')

    def test_uncertain_submission_acknowledgment_write_failure_cannot_gate_stop(self):
        runtime, data, root = self.setup_generation()
        data['submission'] = 'uncertain'
        self.manager._write(runtime, data)
        with patch.object(self.manager, '_write', side_effect=PermissionError('acknowledgment read-only')):
            result = self.manager.handle(self.request('session.stop'))
        self.assertIn(('stop', data['unit']), self.services.calls)
        self.assertFalse(self.services.active[data['unit']])
        self.assertFalse(result['result']['records_preserved'])
        self.assertEqual(result['result']['cleanup'], 'complete')
        self.assertEqual(read_metadata(runtime, 'default', data['generation'])['submission'], 'acknowledged')
        self.assertEqual(self.manager.handle(self.request('session.status'))['result']['state'], 'stopped')

    def test_uncertain_submission_and_busy_generation_lock_still_stop(self):
        runtime, data, root = self.setup_generation()
        data['submission'] = 'uncertain'
        self.manager._write(runtime, data)
        with generation_lock(runtime, data['generation']):
            with self.assertRaises(ContractError):
                self.manager.handle(self.request('session.stop'))
            self.assertIn(('stop', data['unit']), self.services.calls)
            self.assertFalse(self.services.active[data['unit']])
        # Without a functioning post recorder, an unloaded uncertain generation
        # stays reserved; disappearance alone cannot disprove a late submission.
        self.assertEqual(read_metadata(runtime, 'default', data['generation'])['submission'], 'uncertain')

    def test_busy_generation_lock_cannot_prevent_verified_unit_fallback(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']):
            with self.assertRaises(ContractError):
                self.manager.handle(self.request('session.stop'))
            self.assertIn(('stop', data['unit']), self.services.calls)
            self.assertFalse(self.services.active[data['unit']])
        result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'failed')
        self.assertEqual(result['result']['cleanup'], 'complete')

    def test_early_specific_failure_precedes_requested_timeout_without_manifest_failure(self):
        runtime, data, root = self.setup_generation()
        data['state'] = 'ready'
        self.manager._write(runtime, data)
        folder = Path(data['configuration']['artifacts']) / 'generations' / data['generation']
        early = {'generation': data['generation'], 'code': 'input_failed',
                 'message': 'Earlier input failure', 'context': {'component': 'input'}}
        atomic(folder / 'startup-failure.json', early)
        self.assertIsNone(json.loads((folder / 'manifest.json').read_text())['first_failure'])
        with generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]):
            intent(runtime, data, 'manager_request', 'b' * 32)
            updated, _ = finalize(runtime, data, inside=True, service_result='timeout', exit_status='KILL')
        self.assertEqual(updated['state'], 'failed')
        manifest = json.loads((folder / 'manifest.json').read_text())
        terminal = json.loads((folder / 'terminal.json').read_text())
        self.assertEqual(manifest['first_failure'], 'input_failed')
        self.assertEqual(terminal['early_failure'], early)
        self.assertEqual(json.loads((folder / 'startup-failure.json').read_text()), early)

    def test_wrong_generation_early_failure_does_not_override_requested_stop(self):
        runtime, data, root = self.setup_generation()
        folder = Path(data['configuration']['artifacts']) / 'generations' / data['generation']
        atomic(folder / 'startup-failure.json', {'generation': 'f' * 32, 'code': 'input_failed',
                 'message': 'Other generation', 'context': {}})
        with generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]):
            intent(runtime, data, 'manager_request', 'b' * 32)
            updated, _ = finalize(runtime, data, inside=True, service_result='timeout')
        self.assertEqual(updated['state'], 'stopped')
        self.assertNotIn('early_failure', json.loads((folder / 'terminal.json').read_text()))

    def test_terminal_precedence_preserves_requested_fallback_not_watchdog(self):
        for state, failure, intent_value, result, expected in (
            ('ready', None, {}, 'success', 'failed'),
            ('stopped', None, None, 'success', 'failed'),
            ('ready', None, {'origin': 'admitted_request'}, 'timeout', 'stopped'),
            ('ready', None, {'origin': 'manager_request'}, 'signal', 'stopped'),
            ('stopped', None, {'origin': 'manager_request'}, 'watchdog', 'failed'),
            ('ready', 'input_failed', {'origin': 'manager_request'}, 'success', 'failed'),
            ('failed', None, {'origin': 'manager_request'}, 'success', 'failed')):
            with self.subTest(state=state, result=result, failure=failure):
                self.assertEqual(classify({'state': state}, {'first_failure': failure}, intent_value,
                                          result, inside=True), expected)

    def test_post_cleans_autonomously_without_name_lock_and_preserves_claim(self):
        runtime, data, root = self.setup_generation()
        with runtime.lock('default'), generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[{'pid': 42}]) as kill:
            updated, preserved = finalize(runtime, data, inside=True, service_result='signal')
        self.assertTrue(preserved)
        kill.assert_called_once()
        self.assertEqual(updated['state'], 'failed')
        self.assertFalse((root / 'desktop').exists())
        self.assertTrue((root / 'lifecycle.json').exists())
        receipt = json.loads((Path(data['configuration']['artifacts']) / 'generations' / data['generation'] / 'terminal.json').read_text())
        self.assertFalse(receipt['entire_cgroup_empty'])
        self.assertEqual(receipt['excluded_finalizer_pid'], os.getpid())
        self.assertTrue(receipt['ordinary_processes_absent'])
        self.assertEqual(receipt['cleanup'], 'complete')

    def test_artifact_lock_cannot_gate_survivor_kill_or_settings_disposal(self):
        runtime, data, root = self.setup_generation()
        path = Path(data['configuration']['artifacts']) / 'generations' / data['generation'] / 'record.lock'
        with path.open('r') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with generation_lock(runtime, data['generation']), \
                    patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]) as kill:
                updated, preserved = finalize(runtime, data, inside=True, service_result='signal')
        self.assertFalse(preserved)
        kill.assert_called_once()
        self.assertFalse((root / 'desktop').exists())

    def test_survivor_uncertainty_withholds_disposal_and_complete(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', side_effect=ContractError('session_unavailable', 'survivors')):
            with self.assertRaises(ContractError):
                finalize(runtime, data, inside=True, service_result='signal')
        self.assertTrue((root / 'desktop').exists())
        manifest = json.loads((Path(data['configuration']['artifacts']) / 'generations' / data['generation'] / 'manifest.json').read_text())
        self.assertEqual(manifest['cleanup']['state'], 'uncertain')

    def test_stale_finalizer_cannot_touch_replacement_or_signal(self):
        runtime, old, root = self.setup_generation()
        self.manager.handle(self.request('session.stop'))
        replacement = self.start()
        before = (runtime.current / 'default.json').read_bytes()
        with generation_lock(runtime, old['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors') as kill:
            with self.assertRaises(ContractError):
                finalize(runtime, old, inside=True, service_result='success')
        kill.assert_not_called()
        self.assertEqual((runtime.current / 'default.json').read_bytes(), before)
        self.assertTrue(self.services.active['agent-desktop-' + replacement + '.service'])

    def test_generation_lock_serializes_pointer_publication_interleaving(self):
        runtime, data, root = self.setup_generation()
        # Pause a hook after ownership validation: replacement cannot take the
        # same generation lock to publish a new pointer until it exits.
        with generation_lock(runtime, data['generation']):
            with self.assertRaises(ContractError):
                with generation_lock(runtime, data['generation']):
                    atomic(runtime.current / 'default.json', {'generation': 'f' * 32})
            self.assertEqual(runtime.read('default'), data['generation'])

    def test_stale_ready_metadata_cannot_resurrect_finalizer_failure(self):
        runtime, data, root = self.setup_generation()
        stale = dict(data, state='ready')
        with generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]):
            finalize(runtime, data, inside=True, service_result='signal')
        self.manager._write(runtime, stale)
        self.assertEqual(read_metadata(runtime, 'default', data['generation'])['state'], 'failed')

    def test_first_outside_reconciliation_completes_its_own_terminal_receipt(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']):
            finalize(runtime, data, failed=True)
        folder = Path(data['configuration']['artifacts']) / 'generations' / data['generation']
        self.assertEqual(json.loads((folder / 'terminal.json').read_text())['cleanup'], 'complete')
        self.assertFalse((folder / 'reconciliation.json').exists())

    def test_reconciliation_keeps_original_post_receipt(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']), \
                patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]):
            updated, _ = finalize(runtime, data, inside=True, service_result='watchdog', exit_code='killed', exit_status='TERM')
        folder = Path(data['configuration']['artifacts']) / 'generations' / data['generation']
        before = (folder / 'terminal.json').read_bytes()
        with generation_lock(runtime, data['generation']):
            finalize(runtime, updated)
        self.assertEqual((folder / 'terminal.json').read_bytes(), before)
        self.assertTrue(json.loads((folder / 'reconciliation.json').read_text())['entire_cgroup_empty'])

    def test_observe_refreshes_terminal_post_state_before_prior_exit_classification(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']):
            intent(runtime, data, 'admitted_request', 'b' * 32)
        inspect = self.services.inspect
        def completed(*args):
            with generation_lock(runtime, data['generation']), \
                    patch('agent_desktop.service_cleanup.terminate_survivors', return_value=[]):
                finalize(runtime, data, inside=True, service_result='success')
            self.services.active[data['unit']] = False
            return inspect(*args)
        with patch.object(self.services, 'inspect', side_effect=completed):
            result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'stopped')

    def test_stop_intent_and_relay_deadline_never_renew(self):
        runtime, data, root = self.setup_generation()
        with generation_lock(runtime, data['generation']):
            first = intent(runtime, data, 'admitted_request', 'b' * 32)
            second = intent(runtime, data, 'manager_request', 'c' * 32)
        self.assertEqual(first, second)
        with patch('agent_desktop.transport.exchange') as exchange:
            relay(runtime, data)
            before = (root / 'relay-deadline.json').read_bytes()
            relay(runtime, data)
        self.assertEqual((root / 'relay-deadline.json').read_bytes(), before)
        self.assertLessEqual(exchange.call_args.kwargs['deadline'], json.loads(before)['deadline'])
        self.assertEqual(json.loads((root / 'stop-intent.json').read_bytes()), first)

    def test_service_commands_have_clean_installed_helpers_and_literal_paths(self):
        runtime, data, root = self.setup_generation()
        systemd = Systemd()
        with patch.object(systemd, 'command') as command:
            systemd.start(data, runtime, ['/worker'], time.monotonic() + 2)
        argv = command.call_args.args[0]
        self.assertIn('--property=TimeoutStopFailureMode=kill', argv)
        for name in ('ExecStop', 'ExecStopPost'):
            value = next(value for value in argv if value.startswith('--property=' + name + '='))
            self.assertIn('agent_desktop.service_cleanup', value)
            self.assertIn('"/usr/bin/env" "-i"', value)
        self.assertEqual(unit_quote('/space $money %u "quote"'), '"/space $$money %%u \\"quote\\""')


class SurvivorTests(unittest.TestCase):
    def test_owned_pidfds_only_and_rescan_new_descendant(self):
        cgroup = '/test/unit.service'
        stat = '1 (child) ' + ' '.join(['S'] + ['0'] * 18 + ['123'])
        with patch('agent_desktop.service_cleanup.membership', side_effect=lambda pid: cgroup + '/.control' if pid == os.getpid() else cgroup + '/child'), \
                patch('agent_desktop.service_cleanup.members', side_effect=[{os.getpid(), 101}, {os.getpid(), 102}, {os.getpid()}]), \
                patch('os.pidfd_open', side_effect=[901, 902]) as opened, \
                patch('signal.pidfd_send_signal') as sent, patch('os.close') as closed, \
                patch('pathlib.Path.read_text', return_value=stat):
            observed = terminate_survivors(cgroup, time.monotonic() + 1)
        self.assertEqual([item['pid'] for item in observed], [101, 102])
        self.assertEqual([call.args for call in sent.call_args_list], [(901, signal.SIGKILL), (902, signal.SIGKILL)])
        self.assertEqual(closed.call_count, 2)

    def test_wrong_self_cgroup_or_recycled_outside_member_never_signals(self):
        for own in ('/wrong', '/expected/.control'):
            with self.subTest(own=own), \
                    patch('agent_desktop.service_cleanup.membership', side_effect=lambda pid: own if pid == os.getpid() else '/expected-OTHER'), \
                    patch('agent_desktop.service_cleanup.members', return_value={os.getpid(), 101}), \
                    patch('os.pidfd_open', return_value=901), patch('os.close'), \
                    patch('signal.pidfd_send_signal') as sent, \
                    patch('pathlib.Path.read_text', return_value='1 (child) ' + ' '.join(['S'] + ['0'] * 19)):
                with self.assertRaises(ContractError):
                    terminate_survivors('/expected', time.monotonic() + .1)
                sent.assert_not_called()
