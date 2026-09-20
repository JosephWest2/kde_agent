"""Effects, exact identity and deadline/cleanup boundaries of composed targeting."""
import copy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent_desktop.contracts import ContractError, make_request
from agent_desktop.targeting import TargetTask, current_target, resolve
from agent_desktop.windows import Query

GEN = 'a' * 32
APP = {'generation': GEN, 'application_id': 'b' * 32}
IDS = ['12345678-1234-1234-1234-123456789ab' + str(n) for n in range(3)]
HANDLES = [{'generation': GEN, 'window_id': ident} for ident in IDS]


def snapshot(count=1, active=None, app=APP, client=None):
    return {'generation': GEN, 'query_id': 'c' * 32, 'query_artifact': 'window-observations/' + 'c'*32 + '.json',
            'observed_at': .01, 'accepted_at': .02, 'active_window': HANDLES[active] if active is not None else None,
            'windows': [{'window': HANDLES[i], 'app': app, 'pid': 42, 'title': 'same', 'class': 'same',
                         'active': i == active, 'client': client, 'frame': {'x': 9},
                         'association': {'reason': 'verified_process', 'verified_at': .02, 'process': None}}
                        for i in range(count)]}


class Operation:
    def __init__(self, value, guard=None):
        self.value, self.guard = value, guard
        self.error = None
        self.calls = 0
        self.clean = True
        self.recheck_selected = Mock(return_value=True)
    def step(self):
        self.calls += 1
        if self.guard:
            self.guard()
        if isinstance(self.value, Exception):
            raise self.value
        return self.value
    def cancel(self, error):
        self.error = self.error or error
    def cleanup(self, deadline):
        self.cleanup_deadline = deadline
        return self.clean


class Adapter:
    generation = GEN
    def __init__(self, values, clock):
        self.values, self.clock = list(values), clock
        self.starts, self.actions, self.operations = [], [], []
    def start(self, request_id, deadline, **kw):
        self.starts.append(self.clock[0])
        value = self.values.pop(0)
        operation = Operation(value if isinstance(value, Exception) else copy.deepcopy(value))
        self.operations.append(operation)
        return operation
    def activate(self, request_id, deadline, window, guard):
        self.actions.append(window)
        operation = Operation({'activation_completed_at': self.clock[0]}, guard)
        self.operations.append(operation)
        return operation


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.timer = patch('agent_desktop.targeting.time.monotonic', side_effect=lambda: self.clock[0])
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.registry = NS(generation=GEN, observe_application_exit=Mock(return_value=({'state': 'running', 'exit_code': None}, False)))
        self.context = NS(work=NS(admission=NS(deadline=2), cleanup_deadline=3.5), effects=Mock())
        self.health = Mock()
    def task(self, values, *, operation='focus', arguments=None):
        if arguments is None:
            arguments = {'window': GEN + ':' + IDS[0]}
        request = make_request(operation, caller_cwd='/tmp', expected_generation=GEN, arguments=arguments)
        self.adapter = Adapter(values, self.clock)
        return TargetTask(request, self.context, self.adapter, self.registry, self.health)
    def step(self, task, at=None):
        if at is not None:
            self.clock[0] = at
        return task.step(self.clock[0])
    def error(self, task, code, at=None):
        with self.assertRaises(ContractError) as caught:
            self.step(task, at)
        self.assertEqual(caught.exception.code, code)
        return caught.exception
    def test_app_ambiguity_reports_every_uuid_before_activation(self):
        task = self.task([snapshot(3)], arguments={'app': GEN + ':' + APP['application_id']})
        error = self.error(task, 'target_ambiguous')
        self.assertEqual(error.context['candidates'], HANDLES)
        self.assertEqual(self.adapter.actions, [])
        self.context.effects.assert_not_called()
    def test_explicit_unassociated_braced_uppercase_and_fresh_geometry(self):
        bounds = {'x': -.5, 'y': 3.25, 'width': 4.5, 'height': 2}
        task = self.task([snapshot(app=None), snapshot(active=0, app=None, client=bounds)],
                         arguments={'window': GEN + ':{' + IDS[0].upper() + '}'})
        self.assertIsNone(self.step(task))
        self.assertIsNone(self.step(task, .03))
        self.assertIsNone(self.step(task, .09))
        result = self.step(task, .1)
        self.assertTrue(result['focused'])
        self.assertEqual(result['client'], bounds)
        self.assertEqual(self.adapter.actions, [HANDLES[0]])
        self.context.effects.assert_called_once()
        self.assertEqual(self.adapter.starts, [0, .1])
    def test_successful_noop_activation_keeps_polling_then_times_out(self):
        task = self.task([snapshot(2, active=1)] * 3)
        self.step(task)
        self.step(task, .03)
        self.step(task, .1)
        self.step(task, .3)
        self.error(task, 'timeout', 2)
        self.assertEqual(len(self.adapter.actions), 1)
        self.assertEqual(self.adapter.starts, [0, .1, .3])
    def test_initial_absence_and_later_loss_do_not_retarget_same_app(self):
        task = self.task([snapshot(0)])
        self.error(task, 'target_not_found')
        later = snapshot(2, active=1)
        later['windows'].pop(0)
        task = self.task([snapshot(), later])
        self.step(task)
        self.step(task, .03)
        self.error(task, 'target_lost', .1)
        self.assertEqual(self.adapter.actions, [HANDLES[0]])
    def test_queued_deadline_or_generation_mismatch_spawns_nothing(self):
        task = self.task([])
        self.error(task, 'timeout', 2)
        self.assertEqual(self.adapter.starts, [])
        self.clock[0] = 0
        task = self.task([])
        self.adapter.generation = 'c' * 32
        self.error(task, 'generation_mismatch')
        self.assertEqual(self.adapter.starts, [])
    def test_effect_persistence_expiry_prevents_activation_dispatch(self):
        task = self.task([snapshot()])
        self.step(task)
        def slow(*args, **kwargs):
            self.clock[0] = 2
        self.context.effects.side_effect = slow
        self.error(task, 'timeout', .03)
        self.assertFalse(task.activated)
    def test_final_selected_identity_recheck_after_effect_persistence(self):
        task = self.task([snapshot()], arguments={'app': GEN + ':' + APP['application_id']})
        self.step(task)
        task.selected_query.recheck_selected.side_effect = [True, False]
        self.error(task, 'target_lost', .03)
        self.assertFalse(task.activated)
    def test_window_wait_is_passive_existential_and_late_acceptance_fails(self):
        task = self.task([snapshot(0), snapshot(3)], operation='wait',
                         arguments={'condition': 'window', 'app': GEN + ':' + APP['application_id']})
        self.step(task)
        self.assertIsNone(self.step(task, .09))
        result = self.step(task, .11)
        self.assertEqual(len(result['windows']), 3)
        self.assertEqual(self.adapter.actions, [])
        self.context.effects.assert_not_called()
        task = self.task([snapshot(active=0)], operation='wait', arguments={'condition': 'focus', 'window': GEN + ':' + IDS[0]})
        def delayed(*args):
            self.clock[0] = 2
        task.progress = delayed
        self.error(task, 'timeout', .2)
    def test_focus_wait_passive_success_timeout_and_vanish(self):
        for second, code in ((snapshot(active=0), None), (snapshot(0), 'target_lost')):
            self.clock[0] = 0
            task = self.task([snapshot(), second], operation='wait', arguments={'condition': 'focus', 'window': GEN + ':' + IDS[0]})
            self.step(task)
            if code:
                self.error(task, code, .1)
            else:
                self.assertTrue(self.step(task, .1)['satisfied'])
            self.assertEqual(self.adapter.actions, [])
    def test_exit_requires_complete_subtree_not_root_status(self):
        task = self.task([], operation='wait', arguments={'condition': 'exit', 'app': GEN + ':' + APP['application_id']})
        self.registry.observe_application_exit.return_value = ({'state': 'root-exited', 'exit_code': 7}, False)
        self.assertIsNone(self.step(task))
        self.registry.observe_application_exit.return_value = ({'state': 'all-exited', 'exit_code': 7}, True)
        result = self.step(task, .1)
        self.assertTrue(result['exited'])
        self.assertEqual(result['root_returncode'], 7)
        self.assertEqual(self.adapter.starts, [])

    def test_exit_wait_forces_only_initial_observation_until_completion(self):
        task = self.task([], operation='wait', arguments={'condition': 'exit', 'app': GEN + ':' + APP['application_id']})
        for n in range(20):
            self.step(task, n * .005)
        calls = self.registry.observe_application_exit.call_args_list
        self.assertEqual([c.kwargs['force'] for c in calls], [True] + [False] * 19)
    def test_known_exit_precedes_inflight_query_but_original_error_stays_latched(self):
        task = self.task([None], operation='wait', arguments={'condition': 'window', 'app': GEN + ':' + APP['application_id']})
        self.step(task)
        query = task.operation
        self.registry.observe_application_exit.return_value = ({'state': 'all-exited'}, True)
        self.error(task, 'application_exited', .03)
        self.assertEqual(query.calls, 1)
        self.assertEqual(query.error.code, 'application_exited')
        task.request_cancel('cancelled')
        self.assertEqual(query.error.code, 'application_exited')
    def test_query_failure_is_not_false_condition_and_cleanup_owns_slot(self):
        task = self.task([ContractError('window_query_failed', 'bad')])
        self.error(task, 'window_query_failed')
        task.operation.clean = False
        task.request_cancel('cancelled')
        self.assertFalse(task.cleanup(0))
        self.assertEqual(task.operation.error.code, 'window_query_failed')
        self.assertEqual(task.operation.cleanup_deadline, 3.5)

    def test_failure_retention_cannot_replace_latched_timeout(self):
        task = self.task([None])
        self.step(task)
        task.progress = Mock(side_effect=ContractError('artifact_failed', 'record'))
        error = self.error(task, 'timeout', 2)
        self.assertIs(task.operation.error, error)
        task.progress.assert_called_once()

    def test_launch_cancel_record_failure_keeps_native_cleanup_reserve_and_refs(self):
        from agent_desktop.applications import LaunchTask
        from agent_desktop.scheduler import Context, Scheduler, Work
        request = make_request('launch', caller_cwd='/tmp', expected_generation=GEN,
                               arguments={'argv': ['/bin/true'], 'wait_window': True})
        admission = NS(request=request, deadline=10)
        work = Work(admission)
        owner = Scheduler(clock=lambda: self.clock[0], observer=Mock(side_effect=OSError('record')))
        context = Context(owner, work)
        task = LaunchTask(request, context, self.registry, NS(tick=lambda: None), None)
        work.task = task
        task.phase = 'window_wait'
        task.exec_confirmed = True
        retained = {'application': APP, 'process': {'pid': 42}, 'logs': {'stdout': '/owned/log'}}
        task.app = NS(authorized=True, settled=True, snapshot=lambda: retained)
        operation = Operation(None)
        task.window_wait = NS(operation=operation, request_cancel=lambda reason: operation.cancel(reason))
        owner._cancel(work, 'cancelled')
        self.assertEqual(work.error.code, 'cancelled')
        self.assertEqual(work.partial, retained)
        self.assertEqual(work.cleanup_deadline, 1.5)
        self.assertEqual(operation.error, 'cancelled')
    def test_readonly_guard_requires_focus_and_client_without_frame_fallback(self):
        observation = snapshot()
        with self.assertRaises(ContractError):
            current_target(observation, observation['windows'][0], require_focus=True)
        with self.assertRaises(ContractError) as caught:
            current_target(observation, observation['windows'][0], require_client=True)
        self.assertEqual(caught.exception.context['reason'], 'client_geometry_unavailable')
    def test_selected_recheck_uses_original_uuid_pair_and_final_association(self):
        query = object.__new__(Query)
        identity = {'application': APP, 'pid': 42}
        query.owner = NS(generation=GEN)
        query.done, query.phase = True, 'done'
        query.result = snapshot(2)
        query.result['windows'] = [query.result['windows'][1]]
        query.decoder = NS(windows=[NS(uuid=IDS[0], pid=41), NS(uuid=IDS[1], pid=42)])
        query.identities = [None, identity]
        query.bracket = object()
        query.registry = NS(window_identity=Mock(return_value=identity))
        self.assertTrue(query.recheck_selected(HANDLES[1], APP))
        query.registry.window_identity.assert_called_once_with(query.bracket, 42)
        query.result['windows'][0]['app'] = None
        self.assertFalse(query.recheck_selected(HANDLES[1], APP))
