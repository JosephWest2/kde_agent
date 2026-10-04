"""Public screenshot: the full private output, or one window's frame cropped from it.

A failed or cancelled public capture fails only its request, not the session.
"""
import math
import time

from .capture import HEIGHT, WIDTH, Capture
from .contracts import ContractError
from .targeting import TargetTask


def crop_rect(frame):
    """Frame rectangle -> integer [x, y, w, h] clipped to the output, or None if offscreen."""
    left, top = math.floor(frame['x']), math.floor(frame['y'])
    right, bottom = math.ceil(frame['x'] + frame['width']), math.ceil(frame['y'] + frame['height'])
    left, top, right, bottom = max(left, 0), max(top, 0), min(right, WIDTH), min(bottom, HEIGHT)
    if right <= left or bottom <= top:
        return None
    return [left, top, right - left, bottom - top]


class ScreenshotTask:
    cleanup_seconds = 1.5

    def __init__(self, request, context, desktop, adapter, registry, healthy, screen, folder):
        self.request, self.context = request, context
        self.desktop, self.healthy, self.screen, self.folder = desktop, healthy, screen, folder
        self.deadline = context.work.admission.deadline
        self.window = request.arguments.get('window')
        self.target = None if self.window is None else TargetTask(
            request, context, adapter, registry, healthy, condition='observe')
        self.observed = self.capture = None

    def step(self, now):
        if self.capture is None:
            crop = None
            if self.target is not None:
                observed = self.target.step(time.monotonic())
                if observed is None:
                    return None
                self.observed = observed
                crop = crop_rect(observed['frame'])
                if crop is None:
                    raise ContractError('capture_failed', 'Window is entirely outside the screen.',
                                        context={'window': observed['window'], 'frame': observed['frame']})
            if time.monotonic() >= self.deadline:
                raise ContractError('timeout', 'Screenshot deadline expired.')
            self.healthy()
            self.folder.mkdir(mode=0o700, exist_ok=True)
            self.capture = Capture(self.desktop, self.request.expected_generation, self.folder,
                                   self.screen(), self.deadline, crop=crop)
        result = self.capture.step()
        if result is None:
            return None
        if self.observed is not None:
            result = result | {'window': self.observed['window'], 'frame': self.observed['frame'],
                               'focused': self.observed['focused'],
                               'query_artifact': self.observed['query_artifact']}
        return result

    def request_cancel(self, reason):
        if self.target is not None:
            self.target.request_cancel(reason)
        if self.capture is not None:
            self.capture.cancel()

    def cleanup(self, now):
        self.desktop.children.poll()
        done = self.capture is None or self.capture.done()
        return done and (self.target is None or self.target.cleanup(now))
