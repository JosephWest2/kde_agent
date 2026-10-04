"""Public key, type and click: verify the focused target, then emit worker-timed strokes.

The worker owns every press-to-release interval. Client disconnect, cancellation,
timeout and shutdown all reach ``request_cancel``, which releases immediately on
the owner thread. Input is refused while an earlier release is uncertain.
Focus is verified once before the first stroke; mid-sequence rechecks are #67.
"""
import time

from .capture import HEIGHT, WIDTH
from .contracts import ContractError
from .keymap import CAPS_LOCK, parse_chord, text_strokes
from .targeting import TargetTask

TYPE_HOLD = .004      # Press-to-release for each typed character.
TYPE_GAP = .004       # Release-to-next-press.
STROKE_ESTIMATE = .015  # Per-character budget; measured ~10ms with the 5ms owner tick.
MARGIN = .1
CLICK_HOLD = .02      # Button press-to-release.
CLICK_GAP = .06       # Between the clicks of a multi-click; far inside toolkit double-click times.
BUTTONS = {'left': 0x110, 'right': 0x111, 'middle': 0x112}


def held(owner):
    return any(device.held for device in owner.devices.values())


class InputTask:
    cleanup_seconds = 1.5
    kind = 'keyboard'
    gap = TYPE_GAP

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.request, self.context, self.healthy = request, context, healthy
        self.input_owner = input_owner
        self.deadline = context.work.admission.deadline
        # Parse everything before any observation or emission.
        self.parse(request, input_owner)
        self.target = self.make_target(request, context, adapter, registry, healthy)
        self.phase = 'target'
        self.focus = None
        self.index = 0
        self.pressed_at = None
        self.next_at = 0
        self.started_at = self.finished_at = None

    def parse(self, request, input_owner):
        arguments = request.arguments
        if request.operation == 'key':
            self.strokes = [parse_chord(arguments['chord'])]
            self.hold = arguments['hold']
        else:
            # Only this toolkit sends input to the private desktop, so the Caps
            # Lock state is whatever completed `key caps_lock` presses left it.
            owner = input_owner()
            self.strokes = text_strokes(arguments['text'], caps_lock=getattr(owner, 'caps_lock', False))
            self.hold = TYPE_HOLD

    def make_target(self, request, context, adapter, registry, healthy):
        return TargetTask(request, context, adapter, registry, healthy, condition='observe', require_focus=True)

    def estimate(self):
        return len(self.strokes) * STROKE_ESTIMATE + (self.hold if self.request.operation == 'key' else 0)

    def prepare(self, owner):
        """Called once on the owner thread, after targeting and before the first stroke."""

    def owner(self):
        owner = self.input_owner()
        if owner is None:
            raise ContractError('input_unavailable', 'No input connection.')
        if owner.uncertain or owner.retired_held:
            raise ContractError('input_uncertain', 'An earlier key release could not be confirmed; '
                                'stop the session to recover.')
        return owner

    def locate(self):
        """Validate target-dependent arguments before anything is sent."""

    def progress(self):
        return {'window': self.request.arguments.get('window'), 'strokes_sent': self.index,
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
            if self.target is not None:
                focus = self.target.step(time.monotonic())
                if focus is None:
                    return None
                self.focus = focus
            self.locate()
            remaining = self.deadline - time.monotonic()
            needed = self.estimate()
            if needed > remaining - MARGIN:
                raise ContractError('timeout', 'Not enough time left to send this input; nothing was sent.',
                                    context={'phase': 'budget', 'estimated_seconds': round(needed, 3),
                                             'remaining_seconds': round(max(remaining, 0), 3),
                                             'hint': 'Raise --timeout (max 30) or split the text.'})
            self.owner()
            # Durable intent before the first native emission.
            self.context.effects({'window': self.focus and self.focus['window'], 'phase': 'emitting',
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
            if self.index == 0:
                self.prepare(owner)
            owner.press(self.strokes[self.index], self.kind)
            self.pressed_at = time.monotonic()
            return None
        if now - self.pressed_at < self.hold:
            return None
        self.release(strict=True)
        if CAPS_LOCK in self.strokes[self.index]:
            owner.caps_lock = not getattr(owner, 'caps_lock', False)
        self.index += 1
        self.next_at = time.monotonic() + self.gap
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
        if self.target is not None:
            self.target.request_cancel(reason)
        error = getattr(self.context.work, 'error', None)
        if error is not None and self.phase == 'emit':
            error.context.update(self.progress())

    def cleanup(self, now):
        self.release()
        owner = self.input_owner()
        released = owner is None or not held(owner)
        return released and (self.target is None or self.target.cleanup(now))


class ClickTask(InputTask):
    """Absolute motion to one point, then COUNT clicks of one button there.

    With --window, x/y are client-area coordinates (a GTK header bar is client
    content; a KWin title bar is not), the window must be active, and the point
    must be inside the client area and on screen. Without --window they are
    screen coordinates and nothing about windows is checked.
    """
    kind = 'pointer'
    gap = CLICK_GAP

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        arguments = request.arguments
        self.x, self.y = arguments['x'], arguments['y']
        self.button, self.count = arguments['button'], arguments['count']
        self.point = None
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def make_target(self, request, context, adapter, registry, healthy):
        if request.arguments.get('window') is None:
            return None
        return TargetTask(request, context, adapter, registry, healthy, condition='observe',
                          require_focus=True, require_client=True)

    def parse(self, request, input_owner):
        self.strokes = [[BUTTONS[self.button]]] * self.count
        self.hold = CLICK_HOLD

    def estimate(self):
        return self.count * (CLICK_HOLD + CLICK_GAP + STROKE_ESTIMATE)

    def locate(self):
        if self.focus is None:
            x, y = self.x, self.y
        else:
            client = self.focus['client']
            if not (self.x < client['width'] and self.y < client['height']):
                raise ContractError('invalid_arguments', 'Point is outside the window client area.',
                                    context={'reason': 'outside_window', 'x': self.x, 'y': self.y,
                                             'client': client})
            x, y = client['x'] + self.x, client['y'] + self.y
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            raise ContractError('invalid_arguments', 'Point is outside the screen.',
                                context={'reason': 'outside_screen', 'x': x, 'y': y,
                                         'screen': [WIDTH, HEIGHT]})
        self.point = (x, y)

    def prepare(self, owner):
        owner.move(*self.point)

    def result(self):
        return {'window': self.focus and self.focus['window'], 'focused': self.focus is not None or None,
                'dispatched': True, 'query_artifact': self.focus and self.focus['query_artifact'],
                'client': self.focus and self.focus['client'],
                'x': self.x, 'y': self.y, 'screen_x': self.point[0], 'screen_y': self.point[1],
                'button': self.button, 'count': self.count,
                'started_at': self.started_at, 'finished_at': self.finished_at or time.monotonic()}
