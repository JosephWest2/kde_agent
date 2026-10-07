"""Exact current targeting, verified focus and passive bounded conditions.

Only accepted Query returns grant observation authority. Each task keeps a single
native operation; selected UUIDs never change after activation is requested.
"""
import time

from .contracts import ContractError
from .window_types import window_id
from .writer import recorded, track


def resolve(observation, *, window=None, application=None, seen=False):
    if window is not None:
        if window['generation'] != observation['generation']:
            raise ContractError('generation_mismatch', 'Window belongs to another generation.')
        ident = window_id(window['window_id'])
        candidates = [r for r in observation['windows'] if r['window']['window_id'] == ident]
        if candidates and candidates[0]['kind'] != 'window':
            # Popups and compositor surfaces are listed but never targeted.
            raise ContractError('unsupported_operation', 'Window is a transient surface, not a targetable window.',
                                context={'reason': candidates[0]['kind'] + '_surface', 'kind': candidates[0]['kind'],
                                         'query_artifact': observation['query_artifact']})
    else:
        # App selection never counts popups (tooltips, menus, popovers) as candidates.
        candidates = sorted((r for r in observation['windows'] if r['app'] == application and r['kind'] == 'window'),
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
    blocking = [r['window'] for r in observation['windows'] if r['kind'] == 'compositor']
    if require_focus and blocking:
        # A KWin surface such as the window menu takes keyboard and pointer input
        # while the client keeps its active flag; input would reach the menu.
        raise ContractError('target_lost', 'A compositor surface (such as the window menu) has input.',
                            context={'reason': 'compositor_surface_open', 'blocking_windows': blocking})
    if require_client and row['client'] is None:
        raise ContractError('unsupported_operation', 'Current client geometry is unavailable.',
                            context={'reason': 'client_geometry_unavailable'})
    return {k: observation[k] for k in ('generation', 'query_id', 'query_artifact', 'observed_at', 'accepted_at', 'active_window')} | {
        'window': row['window'], 'app': row['app'], 'association': row['association'],
        'focused': focused, 'client': row['client'], 'frame': row['frame']}


class TargetTask:
    # Each effect waits for this request's records (Context.recorded), so the first
    # step may run while the admission and start records are still queued (#96).
    gates_effects = True
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
        self.activation_requested = False
        self.initial_exit_check = True
        self.search = self.matched = self.last_seen = None
        self.searched_title = self.unmatched_title = None

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
        """Runs after asynchronous collision checking, immediately before spawn.
        False: the activation record is not durable yet, so the spawn waits."""
        app = self.application
        if not self.activation_requested:
            self.check()
            self.app_exit(force=True)
            if app is not None and not self.selected_query.recheck_selected(self.selected, app):
                raise ContractError('target_lost', 'Selected application association changed.')
            partial = current_target(self.last, resolve(self.last, window=self.selected)) | {'activation_requested': True}
            self.context.effects(partial, uncertain=True)
            self.activation_requested = True
        if not recorded(self.context):
            return False
        # Persistence may consume the remaining deadline or invalidate identity.
        self.check()
        self.app_exit(force=True)
        if app is not None and not self.selected_query.recheck_selected(self.selected, app):
            raise ContractError('target_lost', 'Selected application association changed.')
        self.activated = True
        return True

    def refs(self):
        refs = {'phase': self.phase}
        if self.application is not None:
            refs['application'] = self.application
        if self.selected is not None:
            refs['window'] = self.selected
        elif self.condition == 'gone':
            # Gone never selects a row; report the awaited window in canonical spelling.
            refs['window'] = {'generation': self.window['generation'], 'window_id': window_id(self.window['window_id'])}
        if self.last is not None:
            refs['last_query_artifact'] = self.last['query_artifact']
        return refs

    def step(self, now):
        try:
            return self.advance()
        except ContractError as error:
            if self.error is None:
                self.error = ContractError(error.code, error.message, context=self.refs() | error.context)
            if self.operation is not None:
                self.operation.cancel(self.error)
            if self.search is not None:
                self.search.abort()
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
        if self.search is not None:
            # A regex search runs in a child; no query starts until it answers.
            matched = self.advance_search()
            if matched is not None or self.search is not None:
                return matched
        if self.operation is None:
            if time.monotonic() < self.next_poll:
                return None
            self.check()
            self.operation = self.adapter.start(self.request.request_id, self.deadline,
                                                application=self.application,
                                                recorded=lambda: recorded(self.context),
                                                track=lambda tickets: track(self.context, tickets))
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
            # A popup alone (e.g. a tooltip) never satisfies a window wait.
            if not any(r['kind'] == 'window' for r in result['windows']):
                return None
            self.check()
            return result | {'condition': 'window', 'satisfied': True, 'application': self.app_snapshot, 'polls': self.polls}
        if self.condition == 'gone':
            return self.gone(result)
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
        if self.condition == 'title':
            # A null or empty title never matches; the window keeps being polled.
            # An unchanged title that did not match is not searched again.
            if row['title'] == self.unmatched_title:
                return None
            from .title_regex import Search
            text, regex = self.request.arguments['match'], self.request.arguments['regex']
            self.search = Search(self.adapter.desktop.children if regex else None, text, regex, row['title'])
            self.searched_title = row['title']
            self.matched = target | {'condition': 'title', 'satisfied': True, 'polls': self.polls,
                                     'title': row['title'], 'row': row, 'match': {'text': text, 'regex': regex}}
            return self.advance_search()
        if self.condition == 'observe' or target['focused']:
            self.check()
            return target | {'condition': 'focus' if self.condition == 'activate' else self.condition,
                             'satisfied': True, 'polls': self.polls}
        return None

    def advance_search(self):
        found = self.search.step()
        if found is None:
            return None
        self.search = None
        if not found:
            self.matched, self.unmatched_title = None, self.searched_title
            return None
        self.check()
        return self.matched

    def gone(self, result):
        """Passive: any row kind may be awaited; the app may keep running."""
        ident = window_id(self.window['window_id'])
        rows = [r for r in result['windows'] if r['window']['window_id'] == ident]
        if rows:
            self.last_seen = rows[0]
            return None
        self.check()
        return {k: result[k] for k in ('generation', 'query_id', 'query_artifact', 'observed_at', 'accepted_at')} | {
            'condition': 'gone', 'satisfied': True, 'window': {'generation': result['generation'], 'window_id': ident},
            'already_gone': self.last_seen is None, 'last_seen': self.last_seen, 'polls': self.polls}

    def request_cancel(self, reason):
        if self.error is None:
            self.error = ContractError(reason, 'Target condition did not complete.', context={'phase': self.phase})
        if self.operation is not None:
            self.operation.cancel(self.error)
        if self.search is not None:
            self.search.abort()
        # Scheduler cancellation (deadline, EOF) usually happens before step sees
        # it. Add controlled phase information without replacing its original
        # cause; a standalone focus/wait also keeps its observation references,
        # as when step itself fails. Composite tasks own their context.
        error = getattr(self.context.work, 'error', None)
        if error is not None:
            refs = self.refs() if self.request.operation in ('focus', 'wait') else {'phase': self.phase}
            for key, value in refs.items():
                error.context.setdefault(key, value)

    def cleanup(self, now):
        # A regex child is killed on cancel; the slot is held until Children reaps it.
        if self.search is not None:
            self.search.abort()
            if not self.search.reaped():
                return False
        return self.operation is None or self.operation.cleanup(self.context.work.cleanup_deadline)
