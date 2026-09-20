"""Capability admission and live-health tests without a desktop or native input."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_desktop.contracts import ContractError
from agent_desktop import readiness


class Variant:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class Child:
    def __init__(self):
        self.returncode = None
        self.abort = Mock()


class Bus:
    """Controlled completions with the same absolute deadline contract as Gio."""
    def __init__(self, address, deadline):
        self.address, self.deadline = address, deadline
        self.connection = None
        self.pending = {}
        self.calls = []
        self.error = None
        self.closed = False

    def call(self, token, destination, path, interface, method, parameters, signature,
             deadline, callback, *, fd=False):
        if token in self.pending:
            raise AssertionError('duplicate in-flight operation')
        call = SimpleNamespace(token=token, destination=destination, path=path,
                               interface=interface, method=method, deadline=deadline, callback=callback)
        self.calls.append(call)
        self.pending[token] = call

    def complete(self, token, value=(), *, error=None, fds=None):
        call = self.pending.pop(token)
        call.callback(None if value is None else Variant(value), fds, error)

    def tick(self):
        if self.error:
            raise self.error
        for token, call in tuple(self.pending.items()):
            if readiness.time.monotonic() >= call.deadline:
                del self.pending[token]
                raise ContractError('timeout', 'Private bus operation timed out.', context={'component': token})

    def close(self):
        self.closed = True
        self.pending.clear()


class Input:
    def __init__(self, owner):
        self.owner = owner
        self.usable = False
        self.disposed = False
        self.cookie = None

    def ready(self):
        return self.usable

    def setup(self, fd):
        os.close(fd)

    def dispose(self):
        self.disposed = True


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = Mock(return_value=100.0)
        clock_patch = patch.object(readiness.time, 'monotonic', self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        bus_patch = patch.object(readiness, 'PrivateBus', Bus)
        bus_patch.start()
        self.addCleanup(bus_patch.stop)
        input_patch = patch.object(readiness, 'Input', Input)
        input_patch.start()
        self.addCleanup(input_patch.stop)
        self.counter = 0
        self.provider = self.make_provider()
        self.desktop = self.provider.desktop

    def make_provider(self, *, deadline=130):
        self.counter += 1
        folder = self.root / str(self.counter)
        folder.mkdir(mode=0o700)
        desktop_root = folder / 'desktop'
        desktop_root.mkdir(mode=0o700)
        artifacts = folder / 'artifacts'
        artifacts.mkdir(mode=0o700)
        store = SimpleNamespace(path=artifacts,
                                open_log=lambda source: (artifacts / (source + '.log')).open('ab'))
        desktop = SimpleNamespace(root=desktop_root, store=store, tick=Mock(),
                                  env={'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(desktop_root / 'bus')})
        desktop.launch = Mock(side_effect=self.launch)
        return readiness.Readiness(desktop, 'a' * 32, '/private/kdotool', deadline)

    def launch(self, argv, cwd, overrides, *, stdout, stderr):
        if 'kwinscript' in argv:
            value = {'schema_version': 1, 'request_id': argv[2], 'active_uuid': None, 'windows': [],
                     'outputs': [{'name': 'Virtual-1', 'width': 1280, 'height': 720}]}
            stdout.write(json.dumps(value).encode())
        return Child()

    def to_query(self, provider=None):
        provider = provider or self.provider
        provider.bus.connection = object()
        provider.tick()
        provider.bus.complete('kwin_registration', (True,))
        return provider

    def to_input(self, provider=None):
        provider = self.to_query(provider)
        provider.query.returncode = 0
        provider.tick()
        provider.bus.complete('query_cleanup', (False,))
        self.assertEqual(provider.phase, 'input_resumed')
        return provider

    def to_capture(self, provider=None):
        provider = self.to_input(provider)
        # Native FD bootstrap ownership is independently covered in input tests.
        provider.bus.pending.pop('input_resumed')
        provider.input.usable = True
        provider.tick()
        self.assertEqual(provider.phase, 'screenshot')
        return provider

    def write_receipt(self, provider=None, *, changes=None):
        provider = provider or self.provider
        image = provider.capture_folder / 'image.png'
        image.write_bytes(b'PNG validation lives in the independently tested capture child')
        image.chmod(0o600)
        receipt = {'schema': 1, 'provider': 'm1-provisional', 'generation': provider.generation,
                   'request_id': provider.capture_id, 'deadline': provider.capture_deadline,
                   'ok': True, 'eof': True, 'dimensions': [1280, 720], 'path': str(image),
                   'completed_at': self.clock() + .1, 'raw_bytes': 1280 * 720 * 4,
                   'png_bytes': image.stat().st_size, 'png_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                   'metadata': {'type': 'raw', 'width': 1280, 'height': 720, 'stride': 5120,
                                'format': 6, 'screen': 'Virtual-1', 'scale': 1.0},
                   'session_stop_required': False, 'stages': []}
        receipt.update(changes or {})
        path = provider.capture_folder / 'result.json'
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        provider.capture.returncode = 0
        return path

    def to_health(self, provider=None):
        provider = self.to_capture(provider)
        self.write_receipt(provider)
        provider.tick()
        self.assertEqual(provider.phase, 'health')
        self.assertEqual(provider.state, 'starting')
        return provider

    def ready(self, provider=None):
        provider = self.to_health(provider)
        provider.bus.complete('bus', ('private-bus-id',))
        provider.bus.complete('compositor', ())
        provider.tick()
        self.assertEqual(provider.state, 'ready')
        return provider

    def failure(self, provider, component):
        with self.assertRaises(ContractError) as caught:
            provider.tick()
        self.assertEqual(caught.exception.context['component'], component)
        self.assertEqual(provider.state, 'failed')
        self.assertFalse(provider.snapshot()['desktop_ready'])
        persisted = json.loads((provider.folder / 'health.json').read_text())
        self.assertEqual(persisted['failure']['context']['component'], component)
        return caught.exception

    def test_sockets_alone_and_helper_exit_alone_never_satisfy_capabilities(self):
        for name in ('bus', 'wayland-private'):
            (self.desktop.root / name).touch()
        self.provider.tick()
        self.assertFalse(self.provider.snapshot()['desktop_ready'])
        self.desktop.launch.assert_not_called()
        self.to_query()
        self.provider.query.returncode = 0
        self.provider.tick()
        self.assertEqual(self.provider.phase, 'query_cleanup')
        self.assertEqual(self.provider.health['window_query']['state'], 'pending')
        self.assertFalse(self.provider.snapshot()['desktop_ready'])

    def test_empty_valid_window_snapshot_is_accepted_after_cleanup(self):
        provider = self.to_input()
        self.assertEqual(provider.health['window_query']['state'], 'passed')
        self.assertEqual(provider.health['window_query']['windows'], 0)
        self.assertTrue(provider.health['window_query']['script_unloaded'])
        provider.tick()
        self.assertEqual(provider.phase, 'input_resumed')
        self.assertIsNone(provider.capture)

    def test_failure_of_each_gate_is_attributed_and_persisted(self):
        cases = ('bus', 'window_query', 'input_resumed', 'screenshot')
        for component in cases:
            with self.subTest(component=component):
                provider = self.make_provider()
                if component == 'bus':
                    provider.bus.error = ContractError('timeout', 'No private bus.', context={'component': 'bus'})
                elif component == 'window_query':
                    self.to_query(provider)
                    provider.query.returncode = 1
                elif component == 'input_resumed':
                    self.to_input(provider)
                    self.clock.return_value = provider.input_deadline
                else:
                    self.to_capture(provider)
                    provider.capture.returncode = 1
                self.failure(provider, component)
                self.clock.return_value = 100.0

    def test_query_metadata_identity_mismatch_cannot_be_accepted(self):
        self.to_query()
        payload = json.loads(self.provider.query_out.read_text())
        payload['request_id'] = 'stale-generation'
        self.provider.query_out.write_text(json.dumps(payload))
        self.provider.query.returncode = 0
        self.failure(self.provider, 'window_query')

    def test_query_read_crossing_work_deadline_does_not_gain_cleanup_budget(self):
        self.to_query()
        self.provider.query.returncode = 0
        original = Path.read_text
        def late(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == self.provider.query_out:
                self.clock.return_value = self.provider.query_deadline
            return value
        with patch.object(Path, 'read_text', late):
            self.failure(self.provider, 'window_query')
        self.assertNotIn('query_cleanup', self.provider.bus.pending)

    def test_query_cleanup_has_separate_bounded_budget_capped_by_startup(self):
        provider = self.make_provider(deadline=101.0)
        self.to_query(provider)
        self.clock.return_value = 100.4
        provider.query.returncode = 0
        provider.tick()
        cleanup = provider.bus.pending['query_cleanup']
        self.assertGreater(cleanup.deadline, provider.query_deadline)
        self.assertLessEqual(cleanup.deadline, self.clock() + 1.5)
        self.assertEqual(cleanup.deadline, provider.deadline)
        self.clock.return_value = 100.7
        provider.bus.complete('query_cleanup', (False,))
        self.assertEqual(provider.health['window_query']['state'], 'passed')
        self.assertLessEqual(provider.input_deadline, provider.deadline)

    def test_paused_or_disconnected_input_invalidates_ready_generation(self):
        for cause in ('device_paused', 'disconnected'):
            with self.subTest(cause=cause):
                provider = self.ready(self.make_provider())
                provider.input.usable = False
                provider.cancel(cause)
                self.failure(provider, 'input_resumed')
                self.assertEqual(provider.emissions, 0)

    def test_screenshot_child_must_exit_before_receipt_can_be_accepted(self):
        provider = self.to_capture()
        self.write_receipt()
        provider.capture.returncode = None
        provider.tick()
        self.assertEqual(provider.health['screenshot']['state'], 'pending')
        self.clock.return_value = provider.capture_deadline
        self.failure(provider, 'screenshot')

    def test_late_screenshot_stat_cannot_refresh_acceptance_time(self):
        provider = self.to_capture()
        self.write_receipt()
        original = Path.is_file
        def late(path):
            answer = original(path)
            if path == provider.capture_folder / 'image.png':
                self.clock.return_value = provider.capture_deadline
            return answer
        with patch.object(Path, 'is_file', late):
            self.failure(provider, 'screenshot')

    def test_screenshot_receipt_strict_shape_and_correlated_identity(self):
        cases = ({'generation': 'b' * 32}, {'request_id': 'stale'}, {'ok': 1}, {'eof': 1},
                 {'dimensions': [640, 360]}, {'deadline': 0}, {'completed_at': float('nan')},
                 {'completed_at': True}, {'schema': 2}, {'schema': True}, {'provider': 'unrelated'},
                 {'png_sha256': 'not-a-hash'}, {'png_sha256': 'z' * 64},
                 {'raw_bytes': 1}, {'raw_bytes': 3686400.0}, {'png_bytes': -1},
                 {'path': '/tmp/other.png'}, {'session_stop_required': True})
        for changes in cases:
            with self.subTest(changes=changes):
                provider = self.to_capture(self.make_provider())
                self.write_receipt(provider, changes=changes)
                self.failure(provider, 'screenshot')

    def test_screenshot_symlink_publication_is_rejected(self):
        provider = self.to_capture()
        self.write_receipt()
        image = provider.capture_folder / 'image.png'
        target = provider.capture_folder / 'elsewhere.png'
        image.rename(target)
        image.symlink_to(target)
        self.failure(provider, 'screenshot')

    def test_screenshot_receipt_symlink_or_shared_permissions_are_rejected(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                provider = self.to_capture(self.make_provider())
                receipt = self.write_receipt(provider)
                if shared:
                    receipt.chmod(0o644)
                else:
                    target = receipt.with_name('elsewhere.json')
                    receipt.rename(target)
                    receipt.symlink_to(target)
                self.failure(provider, 'screenshot')

    def test_health_round_shares_one_second_deadline_and_never_overlaps(self):
        provider = self.to_health()
        health = [call for call in provider.bus.calls if call.token in ('bus', 'compositor')]
        self.assertEqual(len(health), 2)
        self.assertEqual(health[0].deadline, health[1].deadline)
        self.assertEqual(health[0].deadline, self.clock() + 1)
        for _ in range(5):
            self.clock.return_value += .1
            provider.tick()
        self.assertEqual(sum(call.token in ('bus', 'compositor') for call in provider.bus.calls), 2)
        provider.bus.complete('bus', ('private-id',))
        provider.bus.complete('compositor', ())
        provider.tick()
        self.assertEqual(provider.state, 'ready')
        self.clock.return_value = 110.0
        provider.tick()
        # Delayed owner starts exactly one new round; it does not catch up in bursts.
        self.assertEqual(sum(call.token in ('bus', 'compositor') for call in provider.bus.calls), 4)
        self.assertEqual(provider.round['deadline'], 111.0)

    def test_bus_failure_makes_compositor_unknown_and_is_separate_from_compositor_failure(self):
        for component in ('bus', 'compositor'):
            with self.subTest(component=component):
                provider = self.to_health(self.make_provider())
                provider.bus.complete('bus', ('id',) if component != 'bus' else None,
                                      error=RuntimeError('failed') if component == 'bus' else None)
                provider.bus.complete('compositor', None, error=RuntimeError('failed'))
                self.failure(provider, component)
                if component == 'bus':
                    self.assertEqual(provider.health['compositor']['state'], 'unknown')

    def test_bus_round_timeout_invalidates_previous_compositor_observation(self):
        provider = self.ready()
        self.clock.return_value += 1
        provider.tick()
        self.clock.return_value = provider.round['deadline']
        self.failure(provider, 'bus')
        self.assertEqual(provider.health['compositor']['state'], 'unknown')

    def test_compositor_round_timeout_after_bus_reply_is_attributed_to_compositor(self):
        provider = self.to_health()
        provider.bus.complete('bus', ('id',))
        self.clock.return_value = provider.round['deadline']
        self.failure(provider, 'compositor')

    def test_failure_is_sticky_even_if_late_successful_observations_arrive(self):
        provider = self.to_health()
        with self.assertRaises(ContractError):
            provider.fail(ContractError('session_failed', 'Already failed.'), 'screenshot')
        provider.bus.complete('bus', ('id',))
        provider.bus.complete('compositor', ())
        self.failure(provider, 'screenshot')

    def test_ready_manifest_retains_provisional_scope_and_replacement_owner(self):
        provider = self.ready()
        value = provider.snapshot()
        self.assertTrue(value['desktop_ready'])
        self.assertEqual(value['provider'], 'm1-provisional')
        self.assertFalse(value['release_qualified'])
        self.assertFalse(value['desktop_operations_supported'])
        self.assertEqual(value['replacement_issue'], 35)
        self.assertTrue(all(value['health'][key]['state'] == 'passed' for key in readiness.CAPABILITIES))
        provider.close()
        self.assertTrue(provider.bus.closed)
        self.assertTrue(provider.input.disposed)
