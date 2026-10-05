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


def transient(observation, index, kind, app=None):
    """Mark one row as a popup or compositor surface (pid null), as the decoder does."""
    row = observation['windows'][index]
    row['kind'] = kind
    if kind == 'compositor':
        row.update(pid=None, app=None, association={'reason': 'missing_pid', 'verified_at': None, 'process': None})
    return observation


def snapshot(count=1, active=None, app=APP, client=None):
    return {'generation': GEN, 'query_id': 'c' * 32, 'query_artifact': 'window-observations/' + 'c'*32 + '.json',
            'observed_at': .01, 'accepted_at': .02, 'active_window': HANDLES[active] if active is not None else None,
            'windows': [{'window': HANDLES[i], 'kind': 'window', 'app': app, 'pid': 42, 'title': 'same', 'class': 'same',
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


class Harness(unittest.TestCase):
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


class TargetTests(Harness):
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
    def test_app_selection_ignores_tooltips_and_popups(self):
        app = GEN + ':' + APP['application_id']
        # Window 0 plus an associated tooltip (1): focus --app still has one candidate.
        tooltip = transient(snapshot(2), 1, 'popup')
        task = self.task([tooltip, transient(snapshot(2, active=0), 1, 'popup')], arguments={'app': app})
        self.assertIsNone(self.step(task))
        self.assertEqual(self.adapter.actions, [HANDLES[0]])
        self.assertIsNone(self.step(task, .03))
        self.assertIsNone(self.step(task, .09))
        self.assertTrue(self.step(task, .1)['focused'])
        # Two real windows stay ambiguous; candidates never list the popup.
        error = self.error(self.task([transient(snapshot(3), 2, 'popup')], arguments={'app': app}), 'target_ambiguous')
        self.assertEqual(error.context['candidates'], HANDLES[:2])
        # Only popups: nothing to select.
        self.error(self.task([transient(snapshot(1), 0, 'popup')], arguments={'app': app}), 'target_not_found')
        observation = transient(snapshot(2), 1, 'popup')
        self.assertEqual(resolve(observation, application=APP)['window'], HANDLES[0])

    def test_explicit_transient_surfaces_are_refused_before_any_action(self):
        for kind in ('popup', 'compositor'):
            observation = transient(snapshot(2, active=0), 1, kind)
            with self.subTest(kind=kind), self.assertRaises(ContractError) as caught:
                resolve(observation, window=HANDLES[1])
            self.assertEqual(caught.exception.code, 'unsupported_operation')
            self.assertEqual(caught.exception.context['reason'], kind + '_surface')
            task = self.task([observation], arguments={'window': GEN + ':' + IDS[1]})
            self.error(task, 'unsupported_operation')
            self.assertEqual(self.adapter.actions, [])
            self.context.effects.assert_not_called()

    def test_window_wait_is_not_satisfied_by_a_popup_alone(self):
        task = self.task([transient(snapshot(1), 0, 'popup'), snapshot(1)], operation='wait',
                         arguments={'condition': 'window', 'app': GEN + ':' + APP['application_id']})
        self.assertIsNone(self.step(task))
        self.assertEqual(len(self.step(task, .1)['windows']), 1)

    def test_compositor_surface_blocks_input_to_the_focused_window(self):
        # KWin's window menu leaves the client active, but takes keyboard input.
        observation = transient(snapshot(2, active=0), 1, 'compositor')
        with self.assertRaises(ContractError) as caught:
            current_target(observation, observation['windows'][0], require_focus=True)
        self.assertEqual(caught.exception.code, 'target_lost')
        self.assertEqual(caught.exception.context, {'reason': 'compositor_surface_open', 'blocking_windows': [HANDLES[1]]})
        # Reading the target (focus, screenshot) is unaffected, and so are the app's own popups.
        self.assertTrue(current_target(observation, observation['windows'][0])['focused'])
        observation = transient(snapshot(2, active=0), 1, 'popup')
        self.assertTrue(current_target(observation, observation['windows'][0], require_focus=True)['focused'])
        # An input task checks before sending anything.
        request = make_request('key', caller_cwd='/tmp', expected_generation=GEN,
                               arguments={'window': GEN + ':' + IDS[0], 'chord': 'a', 'hold': .05})
        self.adapter = Adapter([transient(snapshot(2, active=0), 1, 'compositor')], self.clock)
        task = TargetTask(request, self.context, self.adapter, self.registry, self.health,
                          condition='observe', require_focus=True)
        self.assertEqual(self.error(task, 'target_lost').context['reason'], 'compositor_surface_open')

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


def titled(title, *, count=1, active=None):
    observation = snapshot(count, active=active)
    observation['windows'][0]['title'] = title
    return observation


class TitleAndGoneWaitTests(Harness):
    def title_task(self, values, match='Saved', regex=False, window=0):
        return self.task(values, operation='wait', arguments={'condition': 'title', 'window': GEN + ':' + IDS[window],
                                                              'match': match, 'regex': regex})

    def gone_task(self, values, window=0):
        return self.task(values, operation='wait', arguments={'condition': 'gone', 'window': GEN + ':' + IDS[window]})

    def test_title_initial_match_returns_on_the_first_poll_with_row_and_title(self):
        task = self.title_task([titled('Report - Saved')])
        result = self.step(task)
        self.assertEqual((result['condition'], result['satisfied'], result['polls']), ('title', True, 1))
        self.assertEqual(result['title'], 'Report - Saved')
        self.assertEqual(result['row']['window'], HANDLES[0])
        self.assertEqual(result['window'], HANDLES[0])
        self.assertEqual(result['match'], {'text': 'Saved', 'regex': False})
        self.assertFalse(result['focused'])  # A title wait never needs or changes focus.
        self.assertEqual(self.adapter.actions, [])
        self.context.effects.assert_not_called()

    def test_title_keeps_polling_through_null_empty_and_other_titles(self):
        task = self.title_task([titled(None), titled(''), titled('Report'), titled('Report - Saved')])
        self.assertIsNone(self.step(task))
        self.assertIsNone(self.step(task, .1))
        self.assertIsNone(self.step(task, .2))
        self.assertEqual(self.step(task, .35)['polls'], 4)
        self.assertEqual(self.adapter.starts, [0, .1, .2, .35])

    def test_regex_title_search_spans_steps_without_starting_queries(self):
        long_title = 'x' * 4000 + ' Saved'
        task = self.title_task([titled(long_title)], match='.*' * 100 + 'Saved$', regex=True)
        steps = 1
        result = self.step(task)
        while result is None:
            steps += 1
            result = self.step(task, .01 * steps)
        self.assertGreater(steps, 1)
        self.assertEqual(len(self.adapter.starts), 1)
        self.assertEqual(result['match'], {'text': '.*' * 100 + 'Saved$', 'regex': True})
        self.assertEqual(result['title'], long_title)

    def test_regex_no_match_resumes_polling(self):
        task = self.title_task([titled('a'), titled('ab')], match='^ab$', regex=True)
        self.assertIsNone(self.step(task))
        self.assertTrue(self.step(task, .1)['satisfied'])

    def test_title_timeout_reports_the_last_observation(self):
        task = self.title_task([titled('Report')] * 3)
        self.step(task)
        self.step(task, .1)
        self.step(task, .2)
        error = self.error(task, 'timeout', 2)
        self.assertEqual(error.context['phase'], 'title_wait')
        self.assertEqual(error.context['window'], HANDLES[0])
        self.assertIn('last_query_artifact', error.context)

    def test_title_window_absent_vanished_or_transient(self):
        self.error(self.title_task([snapshot(0)]), 'target_not_found')
        task = self.title_task([titled('Report'), snapshot(0)])
        self.step(task)
        error = self.error(task, 'target_lost', .1)
        self.assertEqual(error.context['phase'], 'title_wait')
        for kind in ('popup', 'compositor'):
            error = self.error(self.title_task([transient(titled('Saved', count=2), 1, kind)], window=1),
                               'unsupported_operation')
            self.assertEqual(error.context['reason'], kind + '_surface')

    def test_title_generation_change_fails(self):
        task = self.title_task([titled('Report'), titled('Saved')])
        self.step(task)
        self.adapter.generation = 'c' * 32
        self.error(task, 'generation_mismatch', .1)

    def test_gone_succeeds_when_the_window_leaves_while_others_stay(self):
        # A dialog (1) closes; the main window (0) and the app keep running.
        remaining = snapshot(2)
        remaining['windows'].pop(1)
        task = self.gone_task([snapshot(2), snapshot(2), remaining], window=1)
        self.assertIsNone(self.step(task))
        self.assertIsNone(self.step(task, .1))
        result = self.step(task, .2)
        self.assertEqual((result['condition'], result['satisfied'], result['already_gone'], result['polls']),
                         ('gone', True, False, 3))
        self.assertEqual(result['window'], HANDLES[1])
        self.assertEqual(result['last_seen']['window'], HANDLES[1])
        self.assertEqual(result['query_artifact'], remaining['query_artifact'])
        self.registry.observe_application_exit.assert_not_called()
        self.assertEqual(self.adapter.actions, [])

    def test_gone_already_gone_is_success_with_a_flag(self):
        result = self.gone_task([snapshot(1)], window=2).step(0)
        self.assertTrue(result['already_gone'])
        self.assertIsNone(result['last_seen'])
        self.assertEqual(result['polls'], 1)

    def test_gone_normalizes_spelling_and_accepts_transient_rows(self):
        request = make_request('wait', caller_cwd='/tmp', expected_generation=GEN,
                               arguments={'condition': 'gone', 'window': GEN + ':{' + IDS[1].upper() + '}'})
        tooltip = transient(snapshot(2), 1, 'popup')
        self.adapter = Adapter([tooltip, snapshot(1)], self.clock)
        task = TargetTask(request, self.context, self.adapter, self.registry, self.health)
        self.assertIsNone(self.step(task))
        result = self.step(task, .1)
        self.assertEqual(result['window'], HANDLES[1])
        self.assertEqual(result['last_seen']['kind'], 'popup')

    def test_gone_timeout_and_generation_mismatch(self):
        task = self.gone_task([snapshot(1)] * 3)
        self.step(task)
        self.step(task, .1)
        self.assertEqual(self.error(task, 'timeout', 2).context['phase'], 'gone_wait')
        self.clock[0] = 0
        task = self.gone_task([snapshot(1)])
        self.adapter.generation = 'c' * 32
        self.error(task, 'generation_mismatch')
        self.assertEqual(self.adapter.starts, [])


class SchedulerTimeoutContextTests(Harness):
    """Production expiry cancels through the scheduler before TargetTask.step sees it."""

    def run_until_timeout(self, arguments, values):
        from agent_desktop.scheduler import Scheduler
        request = make_request('wait', caller_cwd='/tmp', expected_generation=GEN, arguments=arguments,
                               timeout_seconds=2)
        self.adapter = Adapter(values, self.clock)
        owner = Scheduler(clock=lambda: self.clock[0], factory=lambda req, context: TargetTask(
            req, context, self.adapter, self.registry, self.health))
        admission = NS(request=request, admitted_at=0, deadline=2, disconnected=False, on_disconnect=None,
                       results=[])
        admission.complete = lambda **result: admission.results.append(result)
        owner.submit(request, admission)
        for at in (0, .1, .2, 2, 2.01):
            self.clock[0] = at
            owner.tick()
        self.assertEqual(len(admission.results), 1)
        return admission.results[0]['error']

    def test_title_and_focus_timeouts_keep_observation_references(self):
        cases = (({'condition': 'title', 'window': GEN + ':' + IDS[0], 'match': 'Saved', 'regex': False},
                  titled('Report'), 'title_wait'),
                 ({'condition': 'focus', 'window': GEN + ':' + IDS[0]}, snapshot(), 'focus_wait'))
        for arguments, observation, phase in cases:
            with self.subTest(condition=arguments['condition']):
                self.clock[0] = 0
                error = self.run_until_timeout(arguments, [observation] * 3)
                self.assertEqual(error.code, 'timeout')
                self.assertEqual(error.context, {'phase': phase, 'window': HANDLES[0],
                                                 'last_query_artifact': observation['query_artifact']})

    def test_gone_timeout_keeps_phase_and_last_query(self):
        error = self.run_until_timeout({'condition': 'gone', 'window': GEN + ':' + IDS[0]}, [snapshot()] * 3)
        self.assertEqual(error.code, 'timeout')
        self.assertEqual(error.context, {'phase': 'gone_wait', 'window': HANDLES[0],
                                         'last_query_artifact': snapshot()['query_artifact']})
