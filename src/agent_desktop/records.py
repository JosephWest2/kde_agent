"""Bounded scheduler/store bridge. Control safety never depends on persistence.

Every write goes through the store's Journal (writer.py). In the worker that runs
them on the writer thread; Records hands the scheduler the tickets, and the
scheduler waits for them before a first step and before a response.
"""
import sys
import time

from .artifacts import timestamp
from .contracts import ContractError
from .scheduler import CONTROL_OPERATIONS
from .writer import journal_of


def diagnostic():
    # No exception text, context, arguments or environmental values.
    try:
        print('agent-desktop: artifact_failed: terminal record remains uncertain.', file=sys.stderr)
    except OSError:
        pass


class Records:
    def __init__(self, store, *, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.live = {}

    @property
    def journal(self):
        return journal_of(self.store)

    def attach(self, request, admission):
        context = {'admission': admission, 'token': None}
        self.live[request.request_id] = context
        previous = admission.on_terminal

        def terminal(payload):
            # After acceptance: queued best effort, never waited on.
            observed_at, observed_monotonic = timestamp(), self.clock()
            try:
                token = self._ensure(context)
                error = payload['error']
                self.journal.submit('transition', token, 'terminal',
                    outcome='success' if payload['ok'] else error['outcome'],
                    error_code=None if payload['ok'] else error['code'],
                    references=payload['result'] if payload['ok'] else error['partial_result'],
                    observed_at=observed_at, observed_monotonic=observed_monotonic,
                    on_error=lambda error: diagnostic())
            except Exception:
                diagnostic()
            finally:
                if self.live.get(request.request_id) is context:
                    del self.live[request.request_id]
                if previous is not None:
                    previous(payload)
        admission.on_terminal = terminal

    def _ensure(self, context):
        """The request's attempt token: a Ticket for Store.request. Queued again only
        after a known failure, as the synchronous store was asked again next time."""
        token = context['token']
        if token is None or (token.done and token.error is not None):
            admission = context['admission']
            context['token'] = self.journal.submit('request', admission.request, admission.admitted_at,
                                                   admission.deadline)
        return context['token']

    def token(self, request_id):
        """Request attempt for task-owned durable allocations (a Ticket the writer resolves)."""
        return self._ensure(self.live[request_id])

    def observe(self, record):
        context = self.live[record['request_id']]
        before = context['token']
        token = self._ensure(context)
        created = token is not before
        phase = record['event']
        # finalizing means pending; only transport's accepted callback can record success.
        transition = self.journal.submit('transition', token, phase,
                                         outcome=record['outcome'] if phase != 'finalizing' else 'pending',
                                         error_code=record['error_code'], references=record['partial_result'])
        event = self.journal.submit('event', token, phase)
        return (token, transition, event) if created else (transition, event)

    def factory(self, factory):
        def start(request, context):
            if request.operation not in CONTROL_OPERATIONS:
                current = self.live[request.request_id]
                before = current['token']
                token = self._ensure(current)
                created = token is not before
                tickets = ((token,) if created else ()) + (
                    self.journal.submit('transition', token, 'started'),
                    self.journal.submit('event', token, 'started'))
                if any(ticket.done and ticket.error is not None for ticket in tickets):
                    raise ContractError('artifact_failed', 'Request start record could not be preserved.')
                track = getattr(context, 'track', None)
                if track is not None:
                    track(tickets)
                if self.clock() >= current['admission'].deadline:
                    raise ContractError('timeout', 'Request deadline expired before task creation.')
            return factory(request, context)
        return start
