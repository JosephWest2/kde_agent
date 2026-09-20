"""One progressing owner, one fixed graceful deadline; no production adapters.

Hooks must be bounded/nonblocking and return None while pending or a result dict.
A blocked hook is terminated by the independent manager; later hooks then remain
unattempted, never reported as having released input or closed windows.
"""
import time


def unavailable(now, deadline):
    return {'state': 'not_connected', 'replacement_issue': 35, 'confirmed': False}


class Shutdown:
    def __init__(self, scheduler, deadline, *, release=unavailable, close=unavailable,
                 clock=time.monotonic, observe=None, failure=False):
        self.scheduler, self.clock, self.observe = scheduler, clock, observe
        self.deadline = deadline
        self.hooks = {'release': release, 'close': close}
        self.phase = 'cancel'
        self.events = []
        self.results = {}
        self.done = False
        self.started_at = clock()
        self.phase_end = min(deadline, self.started_at + .5)
        if failure:
            scheduler.begin_shutdown(self.phase_end, failure=True)
        else:
            scheduler.begin_shutdown(self.phase_end)
        self._event('cancel', 'started')

    def _event(self, stage, state):
        self.events.append({'stage': stage, 'state': state, 'at': self.clock()})
        if self.observe:
            try:
                self.observe(self.snapshot())
            except Exception:
                pass

    def snapshot(self):
        return {'deadline': self.deadline, 'started_at': self.started_at, 'done': self.done,
                'events': list(self.events), 'stages': dict(self.results), 'replacement_issue': 35}

    def tick(self):
        if self.done:
            return
        if self.phase == 'cancel':
            if not self.scheduler.drain_shutdown(self.phase_end) and self.clock() < self.phase_end:
                return
            self.results['cancel'] = {'state': 'attempted'}
            self._event('cancel', 'finished')
            self.phase = 'release'
            self.phase_end = min(self.deadline, self.clock() + .5)
            self._event('release', 'started')
        # A late bounded callback can still leave time for the close attempt.
        while self.phase in ('release', 'close'):
            if self.clock() >= self.deadline:
                self.results.setdefault(self.phase, {'state': 'deadline', 'confirmed': False})
                break
            try:
                result = self.hooks[self.phase](self.clock(), self.phase_end)
            except Exception as error:
                result = {'state': 'error', 'confirmed': False, 'error': type(error).__name__}
            if result is None and self.clock() < self.phase_end:
                return
            self.results[self.phase] = result or {'state': 'deadline', 'confirmed': False}
            self._event(self.phase, 'finished')
            if self.phase == 'close':
                break
            self.phase, self.phase_end = 'close', self.deadline
            self._event('close', 'started')
        for stage in ('release', 'close'):
            self.results.setdefault(stage, {'state': 'unattempted', 'confirmed': False})
        self.done = True
        self._event('shutdown', 'finished')
