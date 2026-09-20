"""Ownership races and failure gates independent of installed systemd state."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.lifecycle import Manager, atomic, read_metadata, unit_name
from agent_desktop.runtime import Endpoint, Runtime
from agent_desktop.transport import exchange


class Services:
    def __init__(self):
        self.active = {}
        self.calls = []
        self.endpoints = {}
        self.population = False
        self.job = ''

    def cgroup(self, unit, deadline):
        return '/user.slice/app.slice/' + unit

    def start(self, data, runtime, command, deadline):
        self.calls.append(('start', data['unit']))
        self.active[data['unit']] = True
        self.endpoints[data['unit']] = Endpoint(data['session'], data['generation'], managed=True)

    def inspect(self, data, deadline):
        self.calls.append(('inspect', data['unit']))
        active = self.active.get(data['unit'], False)
        return dict(LoadState='loaded' if active else 'not-found', ActiveState='active' if active else 'inactive',
                    SubState='running' if active else 'dead', Job=self.job, empty=not (active or self.population),
                    ControlGroup=data['cgroup'] if active else '', Result='success', MainPID='1' if active else '0')

    def stop(self, data, deadline):
        self.calls.append(('stop', data['unit']))
        self.active[data['unit']] = False
        if data['unit'] in self.endpoints:
            self.endpoints.pop(data['unit']).close()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adl-')
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'r'
        self.runtime.mkdir(mode=0o700)
        self.env = patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime))
        self.env.start()
        self.services = Services()
        self.manager = Manager(systemd=self.services)
        self.ping = patch.object(self.manager, '_ping', return_value={'desktop_ready': False})
        self.ping.start()

    def tearDown(self):
        for endpoint in self.services.endpoints.values():
            endpoint.close()
        self.ping.stop()
        self.env.stop()
        self.temp.cleanup()

    def request(self, operation='session.start', *, generation=None, artifacts=None, timeout=None):
        return make_request(operation, caller_cwd=str(self.root), expected_generation=generation,
                            arguments={'artifacts': artifacts or str(self.root / 'artifacts')} if operation == 'session.start' else {},
                            timeout_seconds=timeout)

    def start(self):
        return self.manager.start(self.request())['session']['generation']

    def test_duplicate_conflict_stop_restart_and_managed_endpoint_no_lock_deadlock(self):
        first = self.start()
        self.assertEqual(self.manager.start(self.request(artifacts='artifacts'))['session']['generation'], first)
        with self.assertRaises(ContractError) as caught:
            self.manager.start(self.request(artifacts='different'))
        self.assertEqual(caught.exception.code, 'session_conflict')
        for _ in range(2):
            result = self.manager.handle(self.request('session.stop', generation=first))
            self.assertEqual(result['result']['cleanup'], 'complete')
        second = self.start()
        self.assertNotEqual(first, second)
        self.assertTrue((Runtime().generations / first).is_dir())

    def test_stale_requests_and_cleanup_cannot_touch_replacement_without_socket(self):
        old = self.start()
        runtime = Runtime()
        data = read_metadata(runtime, 'default', old)
        self.manager.handle(self.request('session.stop'))
        new = self.start()
        self.services.endpoints[unit_name(new)].close()
        before = list(self.services.calls)
        for operation in ('session.start', 'session.status', 'session.stop'):
            with self.subTest(operation=operation), self.assertRaises(ContractError) as caught:
                request = self.request(operation, generation=old, artifacts='conflicting')
                (self.manager.start if operation == 'session.start' else self.manager.handle)(request)
            self.assertEqual(caught.exception.code, 'generation_mismatch')
        with self.assertRaises(ContractError):
            self.manager._retire(runtime, data)
        self.assertEqual(before, self.services.calls)
        self.assertEqual(runtime.read('default'), new)
        self.assertTrue(self.services.active[unit_name(new)])

    def test_expected_stopped_start_does_not_allocate_new_generation(self):
        generation = self.start()
        self.manager.handle(self.request('session.stop'))
        with self.assertRaises(ContractError) as caught:
            self.manager.start(self.request(generation=generation))
        self.assertEqual(caught.exception.code, 'session_unavailable')
        self.assertEqual(Runtime().read('default'), generation)

    def test_held_artifact_lock_does_not_block_stop(self):
        generation = self.start()
        lock = self.root / 'artifacts' / 'generations' / generation / 'record.lock'
        with lock.open('r') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.manager.handle(self.request('session.stop'))
        self.assertFalse(result['result']['records_preserved'])
        self.assertIn(('stop', unit_name(generation)), self.services.calls)
        self.assertFalse(self.services.active[unit_name(generation)])
        self.assertTrue(self.manager.handle(self.request('session.stop'))['result']['records_preserved'])

    def test_live_descendants_and_pending_job_prevent_replacement(self):
        generation = self.start()
        self.services.active[unit_name(generation)] = False
        for population, job in ((True, ''), (False, '123')):
            self.services.population, self.services.job = population, job
            with self.assertRaises(ContractError):
                self.manager.start(self.request())
            self.assertEqual(Runtime().read('default'), generation)

    def test_ambiguous_submission_absence_never_authorizes_retirement(self):
        generation = self.start()
        self.services.active[unit_name(generation)] = False
        runtime = Runtime()
        data = read_metadata(runtime, 'default', generation)
        data['submission'] = 'uncertain'
        self.manager._write(runtime, data)
        with self.assertRaises(ContractError) as caught:
            self.manager.handle(self.request('session.stop', timeout=.03))
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(read_metadata(runtime, 'default', generation)['submission'], 'uncertain')
        with self.assertRaises(ContractError):
            self.manager.start(self.request())
        self.assertEqual(runtime.read('default'), generation)

    def test_manager_inspection_failure_preserves_owner(self):
        generation = self.start()
        with patch.object(self.services, 'inspect', side_effect=ContractError('session_unavailable', 'Test')):
            with self.assertRaises(ContractError):
                self.manager.start(self.request())
        self.assertEqual(Runtime().read('default'), generation)

    def test_metadata_unit_mismatch_fails_before_service_call(self):
        generation = self.start()
        runtime = Runtime()
        data = read_metadata(runtime, 'default', generation)
        data['unit'] = unit_name('f' * 32)
        self.manager._write(runtime, data)
        before = list(self.services.calls)
        with self.assertRaises(ContractError) as caught:
            self.manager.handle(self.request('session.stop'))
        self.assertEqual(caught.exception.code, 'protocol_error')
        self.assertEqual(self.services.calls, before)

    def test_name_lock_wait_is_bounded(self):
        self.start()
        with Runtime().lock('default'):
            start = time.monotonic()
            with self.assertRaises(ContractError) as caught:
                self.manager.handle(self.request('session.stop', timeout=.03))
            self.assertLess(time.monotonic() - start, .1)
            self.assertEqual(caught.exception.code, 'timeout')

    def test_expired_client_deadline_prevents_connect_and_expired_manager_prevents_ping(self):
        generation = self.start()
        request = self.request('session.status', generation=generation)
        with patch('agent_desktop.transport.socket.socket') as socket:
            with self.assertRaises(ContractError):
                exchange(request, deadline=time.monotonic() - 1)
            socket.return_value.connect.assert_not_called()
        inspect = self.services.inspect
        def slow(*args):
            result = inspect(*args)
            time.sleep(.02)
            return result
        with patch.object(self.services, 'inspect', side_effect=slow), patch.object(self.manager, '_ping') as ping:
            with self.assertRaises(ContractError):
                self.manager.handle(self.request('session.status', timeout=.01))
            ping.assert_not_called()

    def test_start_failure_cleanup_reserve_and_managed_manifest_not_ready(self):
        generation = self.start()
        value = json.loads((self.root / 'artifacts' / 'generations' / generation / 'manifest.json').read_text())
        self.assertEqual(value['state'], 'starting')
        self.manager.handle(self.request('session.stop'))
        with patch.object(self.services, 'start', side_effect=ContractError('timeout', 'Test')), \
                patch.object(self.manager, '_stop', return_value=True) as stop:
            with self.assertRaises(ContractError):
                self.manager.start(self.request(timeout=.1))
            self.assertGreater(stop.call_args.args[2] - time.monotonic(), 14)

    def test_symlink_metadata_is_not_followed_or_removed(self):
        generation = self.start()
        runtime = Runtime()
        path = runtime.socket_path(generation).parent / 'lifecycle.json'
        content = path.read_bytes()
        destination = self.root / 'preserve.json'
        destination.write_bytes(content)
        path.unlink()
        path.symlink_to(destination)
        before = list(self.services.calls)
        with self.assertRaises(ContractError) as caught:
            self.manager.handle(self.request('session.stop'))
        self.assertEqual(caught.exception.code, 'transport_error')
        self.assertTrue(path.is_symlink())
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(self.services.calls, before)

    def test_start_failure_reports_uncertain_retained_generation(self):
        with patch.object(self.services, 'start', side_effect=ContractError('session_failed', 'Test')), \
                patch.object(self.manager, '_stop', side_effect=ContractError('timeout', 'Test')):
            with self.assertRaises(ContractError) as caught:
                self.manager.start(self.request())
        error = caught.exception
        self.assertEqual(error.outcome, 'unknown')
        self.assertEqual(error.context['cleanup'], 'uncertain')
        self.assertEqual(error.context['resolved_generation'], Runtime().read('default'))

    def test_late_ambiguous_submission_is_stopped_when_it_appears(self):
        generation = self.start()
        runtime = Runtime()
        data = read_metadata(runtime, 'default', generation)
        data['submission'] = 'uncertain'
        self.manager._write(runtime, data)
        self.services.active[unit_name(generation)] = False
        inspect = self.services.inspect
        observations = 0
        def delayed(*args):
            nonlocal observations
            observations += 1
            if observations == 2:
                self.services.active[unit_name(generation)] = True
            return inspect(*args)
        with patch.object(self.services, 'inspect', side_effect=delayed):
            result = self.manager.handle(self.request('session.stop', generation=generation, timeout=.2))
        self.assertTrue(result['ok'])
        self.assertGreaterEqual(observations, 3)
        self.assertEqual(self.services.calls.count(('stop', unit_name(generation))), 1)
        self.assertFalse(self.services.active[unit_name(generation)])
        self.assertEqual(read_metadata(runtime, 'default', generation)['state'], 'stopped')

    def test_late_stop_does_not_touch_a_replacement_pointer(self):
        generation = self.start()
        runtime = Runtime()
        data = read_metadata(runtime, 'default', generation)
        data['submission'] = 'uncertain'
        self.manager._write(runtime, data)
        self.services.active[unit_name(generation)] = False
        inspect = self.services.inspect
        observations = 0
        replacement = 'f' * 32
        def delayed(*args):
            nonlocal observations
            observations += 1
            if observations == 2:
                self.services.active[unit_name(generation)] = True
                atomic(runtime.current / 'default.json', dict(schema_version=1, session='default', generation=replacement))
            return inspect(*args)
        with patch.object(self.services, 'inspect', side_effect=delayed):
            with self.assertRaises(ContractError) as caught:
                self.manager.handle(self.request('session.stop', timeout=.2))
        self.assertEqual(caught.exception.code, 'generation_mismatch')
        self.assertNotIn(('stop', unit_name(generation)), self.services.calls)
        self.assertEqual(runtime.read('default'), replacement)

    def test_stop_preserves_observed_unexpected_failure_without_status(self):
        generation = self.start()
        self.services.active[unit_name(generation)] = False
        inspect = self.services.inspect
        def failed(*args):
            return inspect(*args) | {'LoadState': 'loaded', 'ActiveState': 'failed', 'Result': 'signal'}
        with patch.object(self.services, 'inspect', side_effect=failed):
            result = self.manager.handle(self.request('session.stop'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['state'], 'failed')
        manifest = json.loads((self.root / 'artifacts' / 'generations' / generation / 'manifest.json').read_text())
        self.assertEqual(manifest['state'], 'failed')
        self.assertEqual(manifest['first_failure'], 'session_failed')
        self.assertEqual(manifest['cleanup']['state'], 'complete')

    def test_sticky_artifact_failure_reconciles_routing_after_stop(self):
        from agent_desktop.artifacts import Store
        generation = self.start()
        store = Store(str(self.root / 'artifacts'), 'default', generation)
        try:
            store.generation_update(state='failed', failure='input_failed')
        finally:
            store.close()
        result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'failed')
        self.assertEqual(read_metadata(Runtime(), 'default', generation)['state'], 'failed')
        for operation in ('session.stop', 'session.status'):
            self.assertEqual(self.manager.handle(self.request(operation))['result']['state'], 'failed')
        manifest = json.loads((self.root / 'artifacts' / 'generations' / generation / 'manifest.json').read_text())
        self.assertEqual(manifest['first_failure'], 'input_failed')

    def test_unexpected_clean_worker_exit_is_failed_on_stop(self):
        generation = self.start()
        self.services.active[unit_name(generation)] = False
        result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'failed')
        self.assertEqual(read_metadata(Runtime(), 'default', generation)['state'], 'failed')

    def test_requested_fallback_signal_does_not_become_a_prior_crash_on_repeat(self):
        generation = self.start()
        result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'stopped')
        inspect = self.services.inspect
        def failed_unit(*args):
            return inspect(*args) | {'LoadState': 'loaded', 'ActiveState': 'failed', 'Result': 'timeout'}
        with patch.object(self.services, 'inspect', side_effect=failed_unit):
            result = self.manager.handle(self.request('session.stop'))
        self.assertEqual(result['result']['state'], 'stopped')


if __name__ == '__main__':
    unittest.main()
