"""Exact current targeting, verified focus and passive bounded conditions.

Only accepted Query returns grant observation authority. Each task keeps a single
native operation; selected UUIDs never change after activation is requested.
"""
import time

from .contracts import ContractError
from .window_types import window_id


def resolve(observation, *, window=None, application=None, seen=False):
    if window is not None:
        if window['generation'] != observation['generation']:
            raise ContractError('generation_mismatch', 'Window belongs to another generation.')
        ident = window_id(window['window_id'])
        candidates = [r for r in observation['windows'] if r['window']['window_id'] == ident]
    else:
        candidates = sorted((r for r in observation['windows'] if r['app'] == application),
                            key=lambda r: r['window']['window_id'])
    if not candidates:
        raise ContractError('target_lost' if seen else 'target_not_found', 'Window is not present in the current observation.')
    if len(candidates) != 1:
        raise ContractError('target_ambiguous', 'Application has multiple windows; select an explicit identity.',
                            context={'candidates': [r['window'] for r in candidates],
                                     'query_artifact': observation['query_artifact']})
    return candidates[0]


def current_target(observation, row, *, require_focus=False, require_client=False):
    """Read-only input seam; emits nothing and never substitutes frame geometry."""
    focused = row['active'] and observation['active_window'] == row['window']
    if require_focus and not focused:
        raise ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
    if require_client and row['client'] is None:
        raise ContractError('unsupported_operation', 'Current client geometry is unavailable.',
                            context={'reason': 'client_geometry_unavailable'})
    return {k: observation[k] for k in ('generation', 'query_id', 'query_artifact', 'observed_at', 'accepted_at', 'active_window')} | {
        'window': row['window'], 'app': row['app'], 'association': row['association'],
        'focused': focused, 'client': row['client'], 'frame': row['frame']}


class TargetTask:
    cleanup_seconds = 1.5

    def __init__(self, request, context, adapter, registry, healthy, *, condition=None,
                 application=None, progress=None, require_focus=False, require_client=False):
        self.request, self.context, self.adapter = request, context, adapter
        self.registry, self.healthy = registry, healthy
        self.condition = condition or ('activate' if request.operation == 'focus' else request.arguments['condition'])
        self.application = application or request.arguments.get('app')
        self.window = request.arguments.get('window')
        self.progress = progress
        self.require_focus, self.require_client = require_focus, require_client
        self.deadline = context.work.admission.deadline
        self.operation = self.selected_query = None
        self.selected = self.last = self.app_snapshot = None
        self.error = None
        self.phase = 'resolve' if self.condition in ('activate', 'observe') else self.condition + '_wait'
        self.next_poll = 0
        self.polls = 0
        self.started_at = None
        self.activated = False
        self.initial_exit_check = True

    def check(self):
        if self.error is not None:
            raise self.error
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Target condition deadline expired.')
        self.healthy()
        generation = self.request.expected_generation
        if self.adapter.generation != generation or self.registry.generation != generation:
            raise ContractError('generation_mismatch', 'Target generation changed.')
        for handle in (self.application, self.window):
            if handle is not None and handle['generation'] != generation:
                raise ContractError('generation_mismatch', 'Target belongs to another generation.')
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Target condition deadline expired.')

    def app_exit(self, *, force=False):
        if self.application is None:
            return False
        self.app_snapshot, exited = self.registry.observe_application_exit(self.application, force=force)
        if exited and self.condition != 'exit':
            raise ContractError('application_exited', 'Application and its descendants have exited.')
        return exited

    def retain(self, *, failing=False):
        if self.progress is not None:
            try:
                self.progress()
            except Exception:
                if not failing:
                    raise

    def activation_guard(self):
        # Runs after asynchronous collision checking, immediately before spawn.
        self.check()
        self.app_exit(force=True)
        app = self.application
        if app is not None and not self.selected_query.recheck_selected(self.selected, app):
            raise ContractError('target_lost', 'Selected application association changed.')
        partial = current_target(self.last, resolve(self.last, window=self.selected)) | {'activation_requested': True}
        self.context.effects(partial, uncertain=True)
        # Persistence may consume the remaining deadline or invalidate identity.
        self.check()
        self.app_exit(force=True)
        if app is not None and not self.selected_query.recheck_selected(self.selected, app):
            raise ContractError('target_lost', 'Selected application association changed.')
        self.activated = True

    def step(self, now):
        try:
            return self.advance()
        except ContractError as error:
            if self.error is None:
                refs = {'phase': self.phase}
                if self.application is not None:
                    refs['application'] = self.application
                if self.selected is not None:
                    refs['window'] = self.selected
                if self.last is not None:
                    refs['last_query_artifact'] = self.last['query_artifact']
                self.error = ContractError(error.code, error.message, context=refs | error.context)
            if self.operation is not None:
                self.operation.cancel(self.error)
            self.retain(failing=True)
            raise self.error

    def advance(self):
        self.check()
        if self.started_at is None:
            self.started_at = time.monotonic()
        # Known completion precedes advancing a query whose bracket may retire.
        exited = self.app_exit(force=self.initial_exit_check)
        self.initial_exit_check = False
        self.check()
        if self.condition == 'exit':
            if not exited:
                return None
            self.app_exit(force=True)
            self.check()
            return {'condition': 'exit', 'satisfied': True, 'exited': True,
                    'root_returncode': self.app_snapshot['exit_code'],
                    'descendant_exit_codes': None, 'application': self.app_snapshot}
        if self.operation is None:
            if time.monotonic() < self.next_poll:
                return None
            self.check()
            self.operation = self.adapter.start(self.request.request_id, self.deadline,
                                                application=self.application)
            self.polls += 1
            self.next_poll = time.monotonic() + .1
        result = self.operation.step()
        if result is None:
            return None
        completed, self.operation = self.operation, None
        self.check()
        self.app_exit(force=True)
        if self.phase == 'activate':
            self.phase = 'focus_wait'
            return None
        self.last = result
        self.retain()
        self.check()
        if self.condition == 'window':
            if not result['windows']:
                return None
            self.check()
            return result | {'condition': 'window', 'satisfied': True, 'application': self.app_snapshot, 'polls': self.polls}
        row = resolve(result, window=self.selected or self.window,
                      application=self.application, seen=self.selected is not None)
        target = current_target(result, row, require_focus=self.require_focus, require_client=self.require_client)
        if self.condition == 'activate' and self.selected is None:
            self.selected, self.selected_query = row['window'], completed
            self.phase = 'activate'
            # No task-internal wait or other operation intervenes after selection.
            self.operation = self.adapter.activate(self.request.request_id, self.deadline,
                                                   self.selected, self.activation_guard)
            return None
        self.selected = row['window']
        if self.condition == 'observe' or target['focused']:
            self.check()
            return target | {'condition': 'focus' if self.condition == 'activate' else self.condition,
                             'satisfied': True, 'polls': self.polls}
        return None

    def request_cancel(self, reason):
        if self.error is None:
            self.error = ContractError(reason, 'Target condition did not complete.', context={'phase': self.phase})
        if self.operation is not None:
            self.operation.cancel(self.error)
        # Scheduler cancellation may happen before step sees the deadline/EOF.
        # Add controlled phase information without replacing its original cause.
        error = getattr(self.context.work, 'error', None)
        if error is not None:
            error.context.setdefault('phase', self.phase)

    def cleanup(self, now):
        return self.operation is None or self.operation.cleanup(self.context.work.cleanup_deadline)
