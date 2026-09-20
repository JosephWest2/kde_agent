"""Close ownership, whole-lifetime semantics and one-reserve boundaries."""
import copy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent_desktop.artifacts import safe_projection
from agent_desktop.closing import CloseOperation, CloseHook, CloseTask
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.windows import NativeClose, Query
from test_targeting import GEN, APP, HANDLES, IDS, snapshot


class Operation:
    def __init__(self, clock, value, guard=None):
        self.clock, self.value, self.guard = clock, value, guard
        self.id = 'd' * 32
        self.calls = 0
        self.error = self.cleanup_deadline = None
        self.clean_at = 0
        self.recheck_selected = Mock(return_value=True)
    def step(self):
        self.calls += 1
        if self.guard:
            self.guard()
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)
    def cancel(self, cause):
        if self.error is None:
            self.error = cause
            self.cleanup_deadline = self.clock[0] + 1.5
    def cleanup(self, deadline=None):
        if deadline is not None:
            self.cleanup_deadline = min(self.cleanup_deadline, deadline)
        return self.clock[0] < self.cleanup_deadline and self.clock[0] >= self.clean_at


class Adapter:
    generation = GEN
    def __init__(self, clock, values):
        self.clock, self.values = clock, list(values)
        self.starts, self.actions = [], []
        self.active = None
    def start(self, request_id, deadline, **kw):
        self.starts.append(self.clock[0])
        return Operation(self.clock, self.values.pop(0))
    def request_close(self, request_id, deadline, window, guard):
        self.actions.append(window)
        return Operation(self.clock, {'close_transport_completed_at': self.clock[0]}, guard)


class CloseTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.]
        timer = patch('agent_desktop.closing.time.monotonic', side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        self.exited = False
        self.root_code = None
        self.populated = True
        self.registry = NS(generation=GEN, observe_application_exit=Mock(side_effect=self.observe),
                           application_process_state=Mock(side_effect=self.state))
        self.effects, self.health = Mock(), Mock()
    def observe(self, handle, **kw):
        self.assertEqual(handle, APP)
        return {'application': APP, 'state': 'all-exited' if self.exited else 'running',
                'exit_code': self.root_code}, self.exited
    def state(self, handle):
        return {'root_reaped': self.root_code is not None, 'root_returncode': self.root_code,
                'subtree_populated': False if self.exited else self.populated, 'all_exited': self.exited,
                'observed_at': self.clock[0], 'remaining_processes': None, 'enumeration': 'unavailable'}
    def owner(self, values=None, *, app=False, deadline=2):
        self.adapter = Adapter(self.clock, [snapshot()] if values is None else values)
        return CloseOperation('request', GEN, deadline, self.adapter, self.registry, self.health,
                              self.effects, application=APP if app else None, window=None if app else HANDLES[0])
    def step(self, owner, at=None):
        if at is not None:
            self.clock[0] = at
        return owner.step(self.clock[0])
    def error(self, owner, code, at=None):
        with self.assertRaises(ContractError) as caught:
            self.step(owner, at)
        self.assertEqual(caught.exception.code, code)
        return caught.exception
    def dispatch(self, owner):
        self.step(owner)
        self.step(owner, .02)
        self.assertEqual(owner.dispatch, 'transport_completed')
    def test_whole_application_exit_and_nonzero_root_are_required(self):
        owner = self.owner([snapshot(), snapshot(0), snapshot(0)])
        self.dispatch(owner)
        self.root_code = 7
        self.assertIsNone(self.step(owner, .1))
        self.assertIsNone(self.step(owner, .2))
        self.exited = True
        result = self.step(owner, .3)
        self.assertTrue(result['exited'])
        self.assertEqual(result['root_returncode'], 7)
        self.assertEqual(result['window_state'], 'absent')
        self.assertEqual(self.adapter.actions, [HANDLES[0]])
    def test_noop_window_loss_and_dialog_do_not_satisfy_exit_or_retarget(self):
        dialog = snapshot(2)
        dialog['windows'].pop(0)
        owner = self.owner([snapshot(), dialog])
        self.dispatch(owner)
        self.step(owner, .1)
        self.error(owner, 'timeout', 2)
        retained = self.effects.call_args.args[0]
        self.assertEqual(retained['windows'], [HANDLES[1]])
        self.assertFalse(retained['exited'])
        self.assertEqual(retained['window_state'], 'absent')
        self.assertEqual(self.adapter.actions, [HANDLES[0]])
    def test_missing_ambiguous_unassociated_stale_and_expired_send_no_close(self):
        for value, app, code in ((snapshot(0), False, 'target_not_found'),
                                 (snapshot(2), True, 'target_ambiguous'),
                                 (snapshot(app=None), False, 'unsupported_operation')):
            with self.subTest(code=code):
                owner = self.owner([value], app=app)
                error = self.error(owner, code)
                self.assertEqual(self.adapter.actions, [])
                if code == 'unsupported_operation':
                    self.assertEqual(error.context['reason'], 'application_association_unavailable')
                    self.assertEqual(error.context['window'], HANDLES[0])
        owner = self.owner([])
        self.error(owner, 'timeout', 2)
        self.assertEqual(self.adapter.starts, [])
        self.clock[0] = 0
        owner = self.owner([])
        self.adapter.generation = 'f' * 32
        self.error(owner, 'generation_mismatch')
    def test_completed_app_before_dispatch_does_not_close(self):
        self.exited = True
        owner = self.owner([], app=True)
        self.error(owner, 'application_exited')
        self.assertEqual(self.adapter.actions, [])
    def test_effect_failure_or_identity_change_blocks_native_guard(self):
        owner = self.owner()
        self.step(owner)
        self.effects.side_effect = ContractError('artifact_failed', 'record')
        self.error(owner, 'artifact_failed', .02)
        self.assertEqual(owner.dispatch, 'uncertain')
        self.assertIsNone(owner.transport_completed_at)
        self.effects.side_effect = None
        owner = self.owner()
        self.step(owner)
        owner.selected_query.recheck_selected.side_effect = [True, False]
        self.error(owner, 'target_lost', .03)
        self.assertIsNone(owner.transport_completed_at)
    def test_native_error_cannot_turn_into_success_when_app_exits(self):
        owner = self.owner()
        self.step(owner)
        owner.operation.value = ContractError('window_query_failed', 'transport')
        self.error(owner, 'window_query_failed', .02)
        self.exited = True
        owner.request_cancel('cancelled')
        self.error(owner, 'window_query_failed', .03)
    def supersede(self, *, at=1.9, clean_at=2.1):
        owner = self.owner([snapshot(), None])
        self.dispatch(owner)
        self.step(owner, .1)
        query = owner.operation
        query.clean_at = clean_at
        self.exited = True
        self.step(owner, at)
        return owner, query
    def test_supersession_after_work_deadline_uses_remainder_same_reserve(self):
        owner, query = self.supersede()
        self.assertEqual(query.cleanup_deadline, 3.4)
        self.error(owner, 'timeout', 2)
        self.assertEqual(query.cleanup_deadline, 3.4)
        self.clock[0] = 2.1
        self.assertTrue(owner.cleanup(3.5))
        self.assertEqual(query.cleanup_deadline, 3.4)
        self.assertIsNone(owner.result)
    def test_supersession_cleanup_before_deadline_accepts_success(self):
        owner, query = self.supersede(clean_at=1.99)
        self.assertTrue(self.step(owner, 1.99)['exited'])
        self.assertEqual(query.cleanup_deadline, 3.4)
    def test_supersession_exhausted_reserve_is_unconfirmed(self):
        owner, query = self.supersede(clean_at=9)
        self.error(owner, 'timeout', 2)
        self.clock[0] = 3.4
        self.assertFalse(owner.cleanup(3.5))
        self.assertEqual(query.cleanup_deadline, 3.4)
    def test_short_shutdown_bound_permanently_clips_same_reserve(self):
        owner, query = self.supersede(clean_at=2.1)
        self.clock[0] = 2
        owner.request_cancel('cancelled')
        self.assertFalse(owner.cleanup(2.05))
        self.clock[0] = 2.1
        self.assertFalse(owner.cleanup(9))
        self.assertEqual(query.cleanup_deadline, 2.05)
    def test_caller_cancel_during_supersession_preserves_first_cause(self):
        owner, query = self.supersede()
        owner.request_cancel('session_failed')
        owner.request_cancel('cancelled')
        self.error(owner, 'session_failed', 2)
        self.clock[0] = 2.1
        self.assertTrue(owner.cleanup(3.5))
        self.assertEqual(query.cleanup_deadline, 3.4)
        self.assertEqual(query.error.code, 'cancelled')  # Internal only.
        self.assertEqual(owner.error.code, 'session_failed')
    def test_slow_supersession_cleanup_cannot_accept_late_success(self):
        owner, query = self.supersede()
        def slow(deadline=None):
            self.clock[0] = 2
            return True
        query.cleanup = slow
        self.error(owner, 'timeout', 1.99)
    def test_empty_subtree_pending_reap_reports_unknown_not_live(self):
        owner = self.owner()
        self.populated = False
        self.dispatch(owner)
        self.assertIsNone(owner.projection()['exited'])
    def test_hook_effect_free_bounded_and_terminal_idempotent(self):
        self.adapter = Adapter(self.clock, [snapshot()])
        hook = CloseHook('hook', GEN, self.adapter, self.registry, self.health, self.effects, window=HANDLES[0])
        self.assertEqual(self.adapter.starts, [])
        hook(0, 2)
        self.clock[0] = .02
        hook(.02, 2)
        self.exited = True
        self.clock[0] = .03
        result = hook(.03, 2)
        self.assertEqual(result['state'], 'complete')
        self.assertIs(hook(4, 5), result)
        self.assertEqual(len(self.adapter.actions), 1)
    def test_hook_does_not_acquire_fresh_budget_or_compete_with_cleanup(self):
        self.adapter = Adapter(self.clock, [])
        self.adapter.start = Mock(side_effect=ContractError('session_unavailable', 'cleanup'))
        hook = CloseHook('hook', GEN, self.adapter, self.registry, self.health, self.effects, window=HANDLES[0])
        result = hook(0, 1)
        self.assertEqual(result['error'], 'session_unavailable')
        self.assertEqual(self.adapter.actions, [])
        hook = CloseHook('hook', GEN, self.adapter, self.registry, self.health, self.effects, window=HANDLES[0])
        self.clock[0] = 1
        self.assertEqual(hook(1, 1)['error'], 'timeout')
    def test_exact_native_argv_and_adapter_teardown_are_distinct(self):
        adapter = NS(generation=GEN, desktop=NS(), registry=None, binary='/fixed/kdotool')
        for ident in (IDS[0], '{' + IDS[0].upper() + '}'):
            action = NativeClose(adapter, 'request', 2, {'generation': GEN, 'window_id': ident}, Mock())
            self.assertEqual(action._argv()[-2:], ['windowclose', '{' + IDS[0] + '}'])
        with self.assertRaises(ContractError):
            NativeClose(adapter, 'request', 2, {'generation': GEN, 'window_id': '%1'}, Mock())
    def test_durable_projection_keeps_typed_identity_and_drops_arbitrary_values(self):
        owner = self.owner()
        self.dispatch(owner)
        value = owner.projection()
        value['close_state']['secret'] = 'drop'
        value['process_state']['pids'] = list(range(4096))
        projected = safe_projection(value, GEN)
        self.assertEqual(projected['application'], APP)
        self.assertEqual(projected['window'], HANDLES[0])
        self.assertEqual(projected['close_state']['dispatch'], 'transport_completed')
        self.assertNotIn('secret', projected['close_state'])
        self.assertNotIn('pids', projected['process_state'])
        value['process_state'].update(observed_at=float('nan'), root_returncode=True, all_exited='yes')
        invalid = safe_projection(value, GEN)['process_state']
        for key in ('observed_at', 'root_returncode', 'all_exited'):
            self.assertNotIn(key, invalid)

    def test_oversized_and_nonfinite_times_are_dropped_without_conversion_errors(self):
        for value in (10 ** 1000, float('inf'), float('-inf'), float('nan'), True, -1):
            result = safe_projection({'close_state': {'exit_observed_at': value},
                                      'process_state': {'observed_at': value}}, GEN)
            self.assertNotIn('exit_observed_at', result['close_state'])
            self.assertNotIn('observed_at', result['process_state'])

    def test_supersession_does_not_hide_an_existing_query_error(self):
        owner = self.owner([snapshot(), None])
        self.dispatch(owner)
        self.step(owner, .1)
        owner.operation.cancel(ContractError('window_query_failed', 'failed first'))
        self.exited = True
        self.error(owner, 'window_query_failed', .2)
        self.assertFalse(owner.superseded)

    def test_disconnect_terminal_record_retains_close_identity_and_live_lifetime(self):
        import json
        from pathlib import Path
        import tempfile
        from agent_desktop.artifacts import Store
        from agent_desktop.contracts import response
        from agent_desktop.records import Records
        from agent_desktop.scheduler import Scheduler
        request = make_request('close', caller_cwd='/tmp', expected_generation=GEN,
                               arguments={'window': GEN + ':' + IDS[0]})
        admission = NS(request=request, admitted_at=0, deadline=2, disconnected=False,
                       on_terminal=None, final=None)
        def complete(result=None, error=None):
            admission.final = response(request.request_id, request.operation, session='default',
                                       generation=GEN, result=result, error=error)
            admission.on_terminal(admission.final)
        admission.complete = complete
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'artifacts', 'default', GEN, create=True)
            self.addCleanup(store.close)
            records = Records(store, clock=lambda: self.clock[0])
            records.attach(request, admission)
            adapter = Adapter(self.clock, [snapshot()])
            scheduler = Scheduler(clock=lambda: self.clock[0], observer=records.observe,
                factory=records.factory(lambda req, ctx: CloseTask(req, ctx, adapter, self.registry, self.health)))
            scheduler.submit(request, admission)
            scheduler.tick()
            self.clock[0] = .02
            scheduler.tick()
            admission.disconnected = True
            admission.on_disconnect()
            scheduler.tick()
            self.assertEqual(admission.final['error']['code'], 'cancelled')
            self.assertEqual(admission.final['error']['outcome'], 'partial')
            record = json.loads(next((store.path / 'requests').glob('*/*/record.json')).read_text())
            self.assertEqual(record['phase'], 'terminal')
            self.assertEqual(record['references']['application'], APP)
            self.assertEqual(record['references']['window'], HANDLES[0])
            self.assertEqual(record['references']['close_state']['dispatch'], 'transport_completed')
            self.assertTrue(record['references']['process_state']['subtree_populated'])
            self.assertFalse(self.exited)
