"""An essential failure prevents an already-scheduled effect from advancing."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop.artifacts import Store
from agent_desktop.contracts import ContractError
from agent_desktop.runtime import Endpoint
from agent_desktop.worker import run


class WorkerHealthOrder(unittest.TestCase):
    def test_essential_observation_precedes_queued_scheduler_effect(self):
        effects = []
        class FailedDesktop:
            def __init__(self, *args):
                self.phase = 'constructed'
            def tick(self):
                raise ContractError('session_failed', 'Essential child exited.', context={'component': 'bus'})
        with tempfile.TemporaryDirectory(prefix='who-') as temporary:
            root = Path(temporary)
            runtime = root / 'runtime'
            runtime.mkdir(mode=0o700)
            store = Store(str(root / 'artifacts'), 'health-order', 'd' * 32, create=True)
            try:
                with patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(runtime), 'NOTIFY_SOCKET': ''}), \
                        patch('agent_desktop.worker.Endpoint', side_effect=lambda name, generation, **kw: Endpoint(name, generation)), \
                        patch('agent_desktop.desktop.Desktop', FailedDesktop), \
                        patch('agent_desktop.scheduler.Scheduler.tick', side_effect=lambda: effects.append('effect')):
                    with self.assertRaises(ContractError) as caught:
                        run('health-order', 'd' * 32, store=store, managed=True, desktop=True)
                self.assertEqual(caught.exception.context['component'], 'bus')
                self.assertEqual(effects, [])
                self.assertEqual(store.read()['state'], 'failed')
            finally:
                store.close()
