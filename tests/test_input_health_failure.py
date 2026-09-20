"""Real GLib continuation errors fail readiness before effects or heartbeats."""
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gi.repository import GLib
from agent_desktop.artifacts import Store
from agent_desktop.contracts import ContractError
from agent_desktop.provisional_input import Input, Failure
from agent_desktop.readiness import Readiness
from agent_desktop.runtime import Endpoint
from agent_desktop.worker import run


class InputHealthFailure(unittest.TestCase):
    def provider(self, root, *, age=0):
        provider = Readiness.__new__(Readiness)
        provider.GLib = GLib
        provider.state, provider.phase = 'ready', 'health'
        provider.error = provider.fatal = None
        provider.observed_at = time.monotonic() - age
        provider.health = {key: {'state': 'passed', 'observed_at': provider.observed_at}
                           for key in ('bus', 'compositor', 'window_query', 'input_resumed', 'screenshot')}
        provider.bus = SimpleNamespace(tick=Mock(), close=Mock())
        provider.desktop = SimpleNamespace(tick=Mock())
        provider.folder = root / 'readiness'
        provider.folder.mkdir(mode=0o700)
        provider.query = provider.capture = provider.round = None
        provider.input = SimpleNamespace(ready=lambda: True, dispose=Mock())
        provider.next_health = time.monotonic() + 1
        provider.deadline = time.monotonic() + 30
        return provider

    def rejected_worker(self, root, provider, component):
        """Exercise actual worker GLib turn and cleanup, with queued-effect spies."""
        effects, heartbeats = [], []
        class Constructed:
            def __init__(self, *args):
                self.phase = 'constructed'
                self.deadline = time.monotonic() + 30
            def tick(self):
                pass
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        store = Store(str(root / 'artifacts'), 'health-failure', 'e' * 32, create=True)
        try:
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(runtime), 'NOTIFY_SOCKET': ''}), \
                    patch('agent_desktop.worker.Endpoint', side_effect=lambda name, generation, **kw: Endpoint(name, generation)), \
                    patch('agent_desktop.desktop.Desktop', Constructed), \
                    patch('agent_desktop.scheduler.Scheduler.tick', side_effect=lambda: effects.append('effect')), \
                    patch('agent_desktop.watchdog.Watchdog.tick', side_effect=lambda: heartbeats.append('heartbeat')):
                with self.assertRaises(ContractError) as caught:
                    run('health-failure', 'e' * 32, store=store, managed=True, desktop=True,
                        readiness_factory=lambda *args: provider)
            self.assertEqual(caught.exception.context['component'], component)
            self.assertEqual(effects, [])
            self.assertEqual(heartbeats, [])
            self.assertEqual(store.read()['state'], 'failed')
            self.assertFalse(provider.snapshot()['desktop_ready'])
            self.assertTrue(provider.bus.close.called)
        finally:
            store.close()

    def test_three_second_owner_delay_rejects_work_and_heartbeat_on_next_real_glib_turn(self):
        with tempfile.TemporaryDirectory(prefix='ihf-') as temporary:
            root = Path(temporary)
            provider = self.provider(root, age=3.5)
            self.rejected_worker(root, provider, 'bus')

    def test_protocol_failure_in_second_drain_batch_is_sticky_and_closes_worker(self):
        with tempfile.TemporaryDirectory(prefix='ihf-') as temporary:
            root = Path(temporary)
            provider = self.provider(root)
            provider.cancel = Mock(wraps=provider.cancel)
            provider.log = Mock()
            provider.emissions = 0
            lib = Mock()
            with patch('agent_desktop.provisional_input.binding.load', return_value=lib):
                source = Input(provider)
            provider.input = source
            source.context = object()
            source.connected = True
            source.devices = {9: {'resumed': True}}
            source.seats = {42}
            lib.ei_get_event.side_effect = range(1, 259)
            lib.ei_event_get_type.side_effect = [4] * 256 + [3]
            lib.ei_event_get_seat.side_effect = [0] * 256 + [42]
            lib.ei_event_get_device.return_value = 0
            # An active source must be removed when the idle continuation fails.
            source.watch = GLib.timeout_add(60000, lambda: True)
            fd_source_id = source.watch
            source.drain()
            self.assertEqual(lib.ei_get_event.call_count, 256)
            self.assertTrue(source.ready())
            continuation = source.idle_watch
            self.assertIsNotNone(continuation)
            context = GLib.MainContext.default()
            end = time.monotonic() + 1
            while source.idle_watch is not None and time.monotonic() < end:
                context.iteration(False)
            self.assertIsInstance(provider.fatal, Failure)
            self.assertEqual(provider.fatal.code, 'input_protocol')
            self.assertFalse(source.ready())
            provider.cancel.assert_called_once_with('dispatch_failure')
            self.assertIsNone(context.find_source_by_id(continuation))
            self.assertIsNone(context.find_source_by_id(fd_source_id))
            self.assertEqual(lib.ei_get_event.call_count, 257)
            first = provider.fatal
            source.drain_once()  # Late invocation cannot revive or drain again.
            self.assertIs(provider.fatal, first)
            self.assertEqual(lib.ei_get_event.call_count, 257)
            self.rejected_worker(root, provider, 'input_resumed')
