"""Explicit application termination; Registry alone owns signal authority.

No subprocess, native window operation or asynchronous cleanup is owned here.
Registry must be pumped before step(), using its existing shared turn budget.
"""
import time
import signal

from .contracts import ContractError


class KillTask:
    cleanup_seconds = 0

    def __init__(self, request, context, registry, healthy):
        self.request, self.context, self.registry, self.healthy = request, context, registry, healthy
        self.application = request.arguments['app']
        self.deadline = context.work.admission.deadline
        self.cursor = self.pinned = None
        self.snapshot = self.process_state = None
        self.error = None
        self.started_at = self.term_cutoff = self.signal_cutoff = None
        self.exit_observed_at = None
        self.already_exited = False
        self.exited = None
        self.intent = False

    def check(self):
        work = self.context.work
        if self.error is not None:
            raise self.error
        if work.error is not None:
            raise work.error
        if work.observing_failed:
            raise ContractError('artifact_failed', 'Request record could not be preserved.')
        if work.terminal or self.context.owner.stopping or self.context.owner.unavailable:
            raise ContractError('cancelled', 'Termination authority was revoked.')
        if (self.request.expected_generation != self.registry.generation
                or self.application['generation'] != self.registry.generation):
            raise ContractError('generation_mismatch', 'Kill target belongs to another generation.')
        self.healthy()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Kill deadline expired.')

    def observe(self):
        self.snapshot, complete = self.registry.observe_application_exit(self.application, cached=True)
        self.process_state = self.registry.application_process_state(self.application)
        if self.pinned is not None and self.pinned.completed:
            # Keep the real positive observation time across Registry retirement;
            # historical lookup intentionally supplies no invented timestamp.
            self.process_state = dict(self.pinned.process_state)
        if self.process_state.get('root_returncode') is not None:
            self.snapshot = self.snapshot | {'exit_code': self.process_state['root_returncode']}
        self.exited = True if complete else (False if self.process_state['subtree_populated'] is True else None)
        if complete:
            self.exit_observed_at = self.process_state['observed_at']
        return complete

    def projection(self):
        cursor = self.cursor
        samples = None if cursor is None or not cursor.samples else list(cursor.samples.values())
        complete = self.exited is True
        counts = dict(cursor.counts) if cursor is not None else {
            'term_attempted': 0, 'term_submitted': 0, 'kill_attempted': 0, 'kill_submitted': 0, 'raced_exit': 0}
        phase = cursor.phase if cursor is not None else 'resolve'
        state = {'phase': phase, 'intent': self.intent, 'started_at': self.started_at,
                 'term_cutoff': self.term_cutoff, 'signal_cutoff': self.signal_cutoff, 'deadline': self.deadline,
                 'phase_started_at': None if cursor is None else cursor.phase_started_at,
                 'last_signal_at': None if cursor is None else cursor.last_signal_at,
                 'exit_observed_at': self.exit_observed_at, 'counts': counts,
                 'dispatch_revoked': cursor is None or cursor.revoked,
                 'remaining_processes': [] if complete else samples,
                 'enumeration': 'complete' if complete else 'sampled' if samples else 'unavailable',
                 'enumeration_incomplete': not complete,
                 'sample_truncated': False if complete else cursor is not None and len(cursor.samples) == 64,
                 'ownership_uncertain': None if cursor is None else cursor.ownership_uncertain}
        code = None if self.snapshot is None else self.snapshot.get('exit_code')
        value = {'application': self.application, 'application_snapshot': self.snapshot,
                 'process_state': self.process_state, 'kill_state': state,
                 'exited': self.exited, 'already_exited': self.already_exited,
                 'root_returncode': code, 'exit_status': code, 'descendant_exit_codes': None,
                 'remaining_processes': state['remaining_processes']}
        if self.snapshot is not None:
            value['process'] = self.snapshot.get('process')
            value['logs'] = self.snapshot.get('logs')
            observation = self.snapshot.get('window_observation')
            if observation is not None:
                value['query_artifact'] = observation.get('query_artifact')
        return value

    def retain(self):
        # Signal intent remains uncertain until whole-lifetime exit is accepted.
        self.context.effects(self.projection(), uncertain=self.exited is not True)

    def step(self, now):
        try:
            self.check()
            if self.cursor is not None and self.cursor.error is not None:
                raise self.cursor.error
            complete = self.observe()
            self.check()
            if complete:
                self.already_exited = self.started_at is None
                if self.cursor is not None:
                    self.cursor.detach()
                self.retain()
                self.check()
                return self.projection()
            if self.cursor is None:
                if not callable(getattr(signal, 'pidfd_send_signal', None)):
                    raise ContractError('prerequisite_missing', 'pidfd signaling is unavailable.')
                self.pinned = self.registry.active
                self.started_at = time.monotonic()
                remaining = self.deadline - self.started_at
                if remaining <= 0:
                    raise ContractError('timeout', 'Kill deadline expired.')
                self.term_cutoff = self.started_at + min(1., remaining / 3)
                self.signal_cutoff = self.deadline - min(.25, remaining / 5)
                self.intent = True
                self.retain()  # Failure here leaves no dispatchable cursor.
                self.check()
                self.cursor = self.registry.begin_termination(self.application, self.deadline,
                    self.term_cutoff, self.signal_cutoff, self.check, self.retain)
            return None
        except ContractError as error:
            self.cancel(error)
            raise self.error

    def cancel(self, error):
        # Revoke before any lookup, diagnostics or artifact callback.
        if self.cursor is not None:
            self.cursor.detach()
        if self.error is None:
            self.error = error
        self.error.context.setdefault('phase', self.cursor.phase if self.cursor else 'resolve')
        self.error.context.setdefault('application', self.application)
        if self.intent:
            try:
                self.observe()
            except Exception:
                self.exited = None
                if self.process_state is not None:
                    self.process_state = self.process_state | {'all_exited': None, 'subtree_populated': None}
            try:
                self.retain()
            except Exception:
                pass

    def request_cancel(self, reason):
        self.cancel(self.context.work.error or ContractError(reason, 'Kill did not complete.'))

    def cleanup(self, now):
        if self.cursor is not None:
            self.cursor.detach()
        return True
