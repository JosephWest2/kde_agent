"""Public key and type: verify the focused target, then emit worker-timed strokes.

The worker owns every press-to-release interval. Client disconnect, cancellation,
timeout and shutdown all reach ``request_cancel``, which releases immediately on
the owner thread. Input is refused while an earlier release is uncertain.
Focus is verified once before the first stroke; mid-sequence rechecks are #67.
"""
import time

from .contracts import ContractError
from .keymap import CAPS_LOCK, parse_chord, text_strokes
from .targeting import TargetTask

TYPE_HOLD = .004      # Press-to-release for each typed character.
TYPE_GAP = .004       # Release-to-next-press.
STROKE_ESTIMATE = .015  # Per-character budget; measured ~10ms with the 5ms owner tick.
MARGIN = .1


def held(owner):
    return any(device.held for device in owner.devices.values())


class InputTask:
    cleanup_seconds = 1.5

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.request, self.context, self.healthy = request, context, healthy
        self.input_owner = input_owner
        self.deadline = context.work.admission.deadline
        arguments = request.arguments
        # Parse everything before any observation or emission.
        if request.operation == 'key':
            self.strokes = [parse_chord(arguments['chord'])]
            self.hold = arguments['hold']
        else:
            # Only this toolkit sends input to the private desktop, so the Caps
            # Lock state is whatever completed `key caps_lock` presses left it.
            owner = input_owner()
            self.strokes = text_strokes(arguments['text'], caps_lock=getattr(owner, 'caps_lock', False))
            self.hold = TYPE_HOLD
        self.target = TargetTask(request, context, adapter, registry, healthy,
                                 condition='observe', require_focus=True)
        self.phase = 'target'
        self.focus = None
        self.index = 0
        self.pressed_at = None
        self.next_at = 0
        self.started_at = self.finished_at = None

    def owner(self):
        owner = self.input_owner()
        if owner is None:
            raise ContractError('input_unavailable', 'No input connection.')
        if owner.uncertain or owner.retired_held:
            raise ContractError('input_uncertain', 'An earlier key release could not be confirmed; '
                                'stop the session to recover.')
        return owner

    def progress(self):
        return {'window': self.request.arguments['window'], 'strokes_sent': self.index,
                'strokes_total': len(self.strokes), 'key_held': self.pressed_at is not None}

    def step(self, now):
        try:
            return self.advance()
        except ContractError as error:
            self.release()
            if self.phase == 'emit':
                error.context.update(self.progress())
            raise

    def advance(self):
        if self.phase == 'target':
            focus = self.target.step(time.monotonic())
            if focus is None:
                return None
            self.focus = focus
            remaining = self.deadline - time.monotonic()
            needed = len(self.strokes) * STROKE_ESTIMATE + (self.hold if self.request.operation == 'key' else 0)
            if needed > remaining - MARGIN:
                raise ContractError('timeout', 'Not enough time left to send this input; nothing was sent.',
                                    context={'phase': 'budget', 'estimated_seconds': round(needed, 3),
                                             'remaining_seconds': round(max(remaining, 0), 3),
                                             'hint': 'Raise --timeout (max 30) or split the text.'})
            self.owner()
            # Durable intent before the first native emission.
            self.context.effects({'window': self.focus['window'], 'phase': 'emitting',
                                  'strokes_total': len(self.strokes)}, uncertain=True)
            self.phase = 'emit'
            self.started_at = time.monotonic()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        owner = self.owner()
        now = time.monotonic()
        if self.pressed_at is None:
            if self.index == len(self.strokes):
                return self.result()
            if now < self.next_at:
                return None
            self.healthy()
            if time.monotonic() >= self.deadline:
                raise ContractError('timeout', 'Input deadline expired.')
            owner.press(self.strokes[self.index])
            self.pressed_at = time.monotonic()
            return None
        if now - self.pressed_at < self.hold:
            return None
        self.release(strict=True)
        if CAPS_LOCK in self.strokes[self.index]:
            owner.caps_lock = not getattr(owner, 'caps_lock', False)
        self.index += 1
        self.next_at = time.monotonic() + TYPE_GAP
        if self.index == len(self.strokes):
            self.finished_at = time.monotonic()
            return self.result()
        return None

    def release(self, *, strict=False):
        """Release anything held now. Strict mode raises if release is unconfirmed."""
        owner = self.input_owner()
        if owner is None:
            return
        # Consult the owner's ledger, not pressed_at: press() records keys as held
        # before each native call, so a partially failed press still needs release.
        if not held(owner) and not owner.uncertain:
            self.pressed_at = None
            return
        try:
            if held(owner):
                owner.release()
        except Exception:
            pass
        if held(owner) or owner.uncertain:
            if strict:
                raise ContractError('input_uncertain', 'Key release could not be confirmed.',
                                    outcome='unknown')
            return
        self.pressed_at = None

    def result(self):
        base = {'window': self.focus['window'], 'focused': True, 'dispatched': True,
                'query_artifact': self.focus['query_artifact'],
                'started_at': self.started_at, 'finished_at': self.finished_at or time.monotonic()}
        if self.request.operation == 'key':
            return base | {'chord': self.request.arguments['chord'], 'codes': self.strokes[0],
                           'hold': self.hold}
        return base | {'characters': len(self.request.arguments['text']), 'strokes': len(self.strokes)}

    def request_cancel(self, reason):
        # Stop emission and release BEFORE anything else.
        self.release()
        self.target.request_cancel(reason)
        error = getattr(self.context.work, 'error', None)
        if error is not None and self.phase == 'emit':
            error.context.update(self.progress())

    def cleanup(self, now):
        self.release()
        owner = self.input_owner()
        released = owner is None or not held(owner)
        return released and self.target.cleanup(now)
