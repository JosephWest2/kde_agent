"""Bounded scheduler/store bridge. Control safety never depends on persistence."""
import sys
import time

from .contracts import ContractError
from .scheduler import CONTROL_OPERATIONS


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

    def attach(self, request, admission):
        context = {'admission': admission, 'token': None}
        self.live[request.request_id] = context
        previous = admission.on_terminal

        def terminal(payload):
            try:
                token = self._ensure(context)
                error = payload['error']
                self.store.transition(token, 'terminal',
                    outcome='success' if payload['ok'] else error['outcome'],
                    error_code=None if payload['ok'] else error['code'],
                    references=payload['result'] if payload['ok'] else error['partial_result'])
            except Exception:
                diagnostic()
            finally:
                if self.live.get(request.request_id) is context:
                    del self.live[request.request_id]
                if previous is not None:
                    previous(payload)
        admission.on_terminal = terminal

    def _ensure(self, context):
        if context['token'] is None:
            admission = context['admission']
            context['token'] = self.store.request(admission.request, admission.admitted_at, admission.deadline)
        return context['token']

    def observe(self, record):
        context = self.live[record['request_id']]
        token = self._ensure(context)
        phase = record['event']
        # finalizing means pending; only transport's accepted callback can record success.
        self.store.transition(token, phase, outcome=record['outcome'] if phase != 'finalizing' else 'pending',
                              error_code=record['error_code'], references=record['partial_result'])
        self.store.event(token, phase)

    def factory(self, factory):
        def start(request, context):
            if request.operation not in CONTROL_OPERATIONS:
                current = self.live[request.request_id]
                try:
                    self.store.transition(self._ensure(current), 'started')
                except Exception:
                    raise ContractError('artifact_failed', 'Request start record could not be preserved.') from None
                if self.clock() >= current['admission'].deadline:
                    raise ContractError('timeout', 'Request deadline expired before task creation.')
            return factory(request, context)
        return start
