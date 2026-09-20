"""One selected normal close followed by bounded whole-application observation.

The owner of a CloseOperation must pump Children and Registry between steps.
CloseHook is a reusable primitive, not production shutdown policy (#35).
"""
import time

from .contracts import ContractError
from .targeting import resolve


class CloseOperation:
    cleanup_seconds = 1.5

    def __init__(self, request_id, generation, deadline, adapter, registry, healthy,
                 effects, *, window=None, application=None):
        self.request_id, self.generation, self.deadline = request_id, generation, deadline
        self.adapter, self.registry, self.healthy, self.effects = adapter, registry, healthy, effects
        self.window, self.application = window, application
        self.selected = self.selected_query = self.operation = None
        self.last = self.app_snapshot = self.process_state = None
        self.phase = 'resolve'
        self.error = None
        self.native_id = self.transport_completed_at = self.exit_observed_at = None
        self.dispatch = 'not_started'
        self.exited = None
        self.initial_exit_check = True
        self.superseded = False
        self.hard_cleanup_deadline = None
        self.next_poll = 0
        self.result = None

    def check(self):
        if self.error is not None:
            raise self.error
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Close deadline expired.')
        self.healthy()
        if self.adapter.generation != self.generation or self.registry.generation != self.generation:
            raise ContractError('generation_mismatch', 'Close generation changed.')
        for handle in (self.application, self.window):
            if handle is not None and handle['generation'] != self.generation:
                raise ContractError('generation_mismatch', 'Close target belongs to another generation.')
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Close deadline expired.')

    def observe_exit(self, *, force=False):
        if self.application is None:
            return
        self.app_snapshot, exited = self.registry.observe_application_exit(self.application, force=force)
        self.process_state = self.registry.application_process_state(self.application)
        self.exited = True if exited else (False if self.process_state['subtree_populated'] is True else None)
        if exited:
            if self.dispatch != 'transport_completed':
                raise ContractError('application_exited', 'Application and its descendants have exited.')
            if self.exit_observed_at is None:
                self.exit_observed_at = time.monotonic()

    def projection(self):
        value = {'application': self.application, 'application_snapshot': self.app_snapshot,
                 'window': self.selected, 'exited': self.exited,
                 'root_returncode': None if self.app_snapshot is None else self.app_snapshot.get('exit_code'),
                 'exit_status': None if self.app_snapshot is None else self.app_snapshot.get('exit_code'),
                 'remaining_processes': None, 'descendant_exit_codes': None, 'process_state': self.process_state,
                 'close_state': {'phase': self.phase, 'dispatch': self.dispatch,
                                 'native_operation_id': self.native_id,
                                 'transport_completed_at': self.transport_completed_at,
                                 'exit_observed_at': self.exit_observed_at},
                 'window_state': 'unavailable', 'windows': []}
        if self.app_snapshot is not None:
            value['process'] = self.app_snapshot.get('process')
        if self.last is not None:
            value.update({key: self.last[key] for key in ('query_id', 'query_artifact', 'observed_at', 'accepted_at')})
            value['windows'] = [row['window'] for row in self.last['windows'] if row['app'] == self.application]
            value['window_state'] = 'present' if any(row['window'] == self.selected for row in self.last['windows']) else 'absent'
        return value

    def retain(self, *, failing=False):
        # Read-only resolution does not itself create an effect/outcome.
        if self.dispatch == 'not_started':
            return
        try:
            self.effects(self.projection(), uncertain=self.dispatch != 'transport_completed')
        except Exception:
            if not failing:
                raise

    def guard(self):
        # Executed after asynchronous name collision checking, just before spawn.
        self.check()
        self.observe_exit(force=True)
        if not self.selected_query.recheck_selected(self.selected, self.application):
            raise ContractError('target_lost', 'Selected application association changed.')
        self.dispatch = 'uncertain'
        self.retain()
        self.check()
        self.observe_exit(force=True)
        if not self.selected_query.recheck_selected(self.selected, self.application):
            raise ContractError('target_lost', 'Selected application association changed.')

    def step(self, now):
        if self.result is not None:
            return self.result
        try:
            return self.advance()
        except ContractError as error:
            self.latch(error)
            self.retain(failing=True)
            raise self.error

    def latch(self, error):
        if self.error is None:
            refs = {'phase': self.phase}
            if self.application is not None:
                refs['application'] = self.application
            if self.selected is not None:
                refs['window'] = self.selected
            if self.last is not None:
                refs['query_artifact'] = self.last['query_artifact']
            self.error = ContractError(error.code, error.message, context=refs | error.context)
        if self.operation is not None:
            self.operation.cancel(self.error)

    def advance(self):
        self.check()
        # Native completion/errors must be accepted before app-exit success.
        if self.phase != 'close_request':
            self.observe_exit(force=self.initial_exit_check)
            self.initial_exit_check = False
        self.check()
        if self.exit_observed_at is not None:
            if self.operation is not None:
                if not self.superseded:
                    if self.operation.error is not None:
                        raise self.operation.error
                    self.operation.cancel(ContractError('cancelled', 'Read-only observation superseded by application exit.'))
                    self.superseded = True
                # Never clip the one existing reserve to the work deadline.
                # Only actual scheduler/shutdown cleanup bounds may shorten it.
                done = self.operation.cleanup(self.hard_cleanup_deadline)
                self.check()
                if not done:
                    return None
                self.operation = None
            self.observe_exit(force=True)
            self.check()
            self.retain()
            self.check()
            self.result = self.projection()
            return self.result
        if self.operation is None:
            if time.monotonic() < self.next_poll:
                return None
            self.operation = self.adapter.start(self.request_id, self.deadline,
                                                application=self.application)
            self.next_poll = time.monotonic() + .1
        result = self.operation.step()
        if result is None:
            return None
        completed, self.operation = self.operation, None
        self.check()
        if self.phase == 'close_request':
            self.dispatch = 'transport_completed'
            self.transport_completed_at = result['close_transport_completed_at']
            self.phase = 'exit_wait'
            self.observe_exit(force=True)
            self.retain()
            self.check()
            return None
        self.last = result
        if self.phase == 'resolve':
            row = resolve(result, window=self.window, application=self.application)
            self.selected, self.selected_query = row['window'], completed
            if row['app'] is None:
                raise ContractError('unsupported_operation', 'Close requires an owned application lifetime.',
                                    context={'reason': 'application_association_unavailable'})
            self.application = row['app']
            self.observe_exit(force=True)
            self.check()
            self.phase = 'close_request'
            self.operation = self.adapter.request_close(self.request_id, self.deadline, self.selected, self.guard)
            self.native_id = self.operation.id
        else:
            # Selected surface disappearance after dispatch is expected; never
            # retarget siblings/dialogs or confuse it with whole-app completion.
            self.retain()
        self.check()
        return None

    def request_cancel(self, reason):
        self.latch(ContractError(reason, 'Close did not complete.'))
        self.retain(failing=True)

    def cleanup(self, deadline):
        return self.operation is None or self.operation.cleanup(deadline)


class CloseTask:
    cleanup_seconds = 1.5

    def __init__(self, request, context, adapter, registry, healthy):
        self.context = context
        self.owner = CloseOperation(request.request_id, request.expected_generation,
                                    context.work.admission.deadline, adapter, registry, healthy,
                                    context.effects, window=request.arguments.get('window'),
                                    application=request.arguments.get('app'))

    def step(self, now):
        return self.owner.step(now)

    def request_cancel(self, reason):
        self.owner.request_cancel(reason)
        error = self.context.work.error
        if error is not None:
            error.context.update({key: value for key, value in self.owner.error.context.items()
                                  if key not in error.context})

    def cleanup(self, now):
        return self.owner.cleanup(self.context.work.cleanup_deadline)


class CloseHook:
    """Effect-free until called; host must pump children/Registry and choose target.

    No production shutdown wiring, target fan-out, input or escalation policy.
    The supplied deadline is a hard bound including resource cleanup.
    """
    def __init__(self, request_id, generation, adapter, registry, healthy, effects,
                 *, window=None, application=None):
        self.args = (request_id, generation, adapter, registry, healthy, effects)
        self.selectors = {'window': window, 'application': application}
        self.owner = self.result = None

    def __call__(self, now, deadline):
        if self.result is not None:
            return self.result
        if self.owner is None:
            request_id, generation, adapter, registry, healthy, effects = self.args
            self.owner = CloseOperation(request_id, generation, deadline, adapter, registry,
                                        healthy, effects, **self.selectors)
        owner = self.owner
        owner.deadline = min(owner.deadline, deadline)
        owner.hard_cleanup_deadline = owner.deadline
        if owner.error is None:
            try:
                result = owner.step(now)
                if result is not None:
                    self.result = {'state': 'complete', 'cleanup_confirmed': True, 'result': result}
                    return self.result
            except ContractError:
                pass
        if owner.error is not None:
            clean = owner.cleanup(owner.deadline)
            if clean or time.monotonic() >= owner.deadline:
                self.result = {'state': 'failed', 'error': owner.error.code,
                               'cleanup_confirmed': clean, 'result': owner.projection()}
        return self.result
