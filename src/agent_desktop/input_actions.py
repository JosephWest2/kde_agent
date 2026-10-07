"""Public key, type, click, move and scroll: verify the focused target, then emit worker-timed input.

The worker owns every press-to-release interval. Client disconnect, cancellation,
timeout and shutdown all reach ``request_cancel``, which releases immediately on
the owner thread. Input is refused while an earlier release is uncertain.
Focus is verified before the first stroke, then rechecked every RECHECK seconds
while a sequence or hold is still being sent. A recheck that finds the window
gone or unfocused releases everything and fails with what was already sent.
"""
import time

from .capture import HEIGHT, WIDTH
from .contracts import ContractError
from .keymap import CAPS_LOCK, parse_chord, text_strokes
from .targeting import TargetTask
from .writer import recorded

TYPE_HOLD = .004      # Press-to-release for each typed character.
TYPE_GAP = .004       # Release-to-next-press.
STROKE_ESTIMATE = .015  # Per-character budget; measured ~10ms with the 5ms owner tick.
MARGIN = .1
RECHECK = .25         # Focus recheck interval during long holds and typing.
RECHECK_QUERY = .5    # A window query caps itself at .5s; input may end with one in flight.
CLICK_HOLD = .02      # Button press-to-release.
CLICK_GAP = .06       # Between the clicks of a multi-click; far inside toolkit double-click times.
SCROLL_GAP = .02      # Between wheel steps, and from the motion to the first step: a brisk wheel spin.
SCROLL_ESTIMATE = .03  # Per-step budget.
BUTTONS = {'left': 0x110, 'right': 0x111, 'middle': 0x112}


def held(owner):
    return any(device.held for device in owner.devices.values())


def busy(owner):
    """Held keys/buttons, or emulation left open by a motion whose press never happened."""
    return any(device.held or device.emulating for device in owner.devices.values())


class InputTask:
    cleanup_seconds = 1.5
    kind = 'keyboard'
    gap = TYPE_GAP
    budget_hint = 'Raise --timeout (max 30) or split the text.'

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
        self.recheck = None
        self.next_recheck = None
        self.rechecks = 0
        self.adapter, self.registry = adapter, registry

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

    def watch_focus(self, *, finishing=False):
        """Advance the periodic focus recheck; raises if the window lost focus.

        Returns True when no recheck is in flight. A running recheck is always
        finished rather than cancelled: cancelling a window query costs more
        than letting it complete, and a result must not leave one behind.
        """
        if self.target is None:
            return True
        now = time.monotonic()
        if self.recheck is None:
            # Skip a recheck that could only finish after the input does.
            if finishing or now < self.next_recheck or self.remaining(now) < RECHECK:
                return True
            self.recheck = self.make_target(self.request, self.context, self.adapter, self.registry, self.healthy)
            # The window was already seen, so its disappearance is target_lost, not target_not_found.
            self.recheck.selected = self.focus['window']
        if self.recheck.step(now) is None:
            return False
        self.rechecks += 1
        self.recheck = None
        self.next_recheck = time.monotonic() + RECHECK
        return True

    def emission(self):
        return len(self.strokes) * STROKE_ESTIMATE + (self.hold if self.request.operation == 'key' else 0)

    def remaining(self, now):
        """Time still needed to send everything, at the pace actually achieved so far."""
        if not self.strokes:
            return 0
        pace = self.emission() / len(self.strokes)
        if self.index:
            pace = max(pace, (now - self.started_at) / self.index)
        if self.pressed_at is None:
            return (len(self.strokes) - self.index) * pace
        held = max(0, self.pressed_at + self.hold - now)
        return held + (len(self.strokes) - self.index - 1) * pace

    def estimate(self):
        # A recheck only starts with at least RECHECK of input left, so one
        # still running at the end overruns it by at most RECHECK_QUERY - RECHECK.
        emission = self.emission()
        return emission + (RECHECK_QUERY - RECHECK if self.target is not None and emission > RECHECK else 0)

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
                'strokes_total': len(self.strokes), 'key_held': self.pressed_at is not None,
                'focus_rechecks': self.rechecks}

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
                                             'hint': self.budget_hint})
            self.owner()
            # Durable intent before the first native emission.
            self.context.effects(self.intent(), uncertain=True)
            self.phase = 'intent'
        if self.phase == 'intent':
            # The intent record is written by the writer thread; nothing is sent
            # until it is durable. Its wait used up time, so the budget is checked again.
            if not recorded(self.context):
                return None
            remaining = self.deadline - time.monotonic()
            needed = self.estimate()
            if needed > remaining - MARGIN:
                raise ContractError('timeout', 'Not enough time left to send this input; nothing was sent.',
                                    context={'phase': 'budget', 'estimated_seconds': round(needed, 3),
                                             'remaining_seconds': round(max(remaining, 0), 3),
                                             'hint': self.budget_hint})
            self.phase = 'emit'
            self.started_at = time.monotonic()
            self.next_recheck = self.started_at + RECHECK
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        return self.emit(self.owner())

    def intent(self):
        return {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'strokes_total': len(self.strokes)}

    def emit(self, owner):
        """One emission step: press, hold and release one stroke at a time."""
        if self.index == len(self.strokes):
            return self.result() if self.watch_focus(finishing=True) else None
        self.watch_focus()
        now = time.monotonic()
        if self.pressed_at is None:
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
            return self.result() if self.watch_focus(finishing=True) else None
        return None

    def release(self, *, strict=False):
        """Release anything held now. Strict mode raises if release is unconfirmed."""
        owner = self.input_owner()
        if owner is None:
            return
        # Consult the owner's ledger, not pressed_at: press() records keys as held
        # before each native call, so a partially failed press still needs release.
        if not busy(owner) and not owner.uncertain:
            self.pressed_at = None
            return
        try:
            if busy(owner):
                owner.release()
        except Exception:
            pass
        if busy(owner) or owner.uncertain:
            if strict:
                raise ContractError('input_uncertain', 'Key release could not be confirmed.',
                                    outcome='unknown')
            return
        self.pressed_at = None

    def result(self):
        base = {'window': self.focus['window'], 'focused': True, 'dispatched': True,
                'query_artifact': self.focus['query_artifact'], 'focus_rechecks': self.rechecks,
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
        if self.recheck is not None:
            self.recheck.request_cancel(reason)
        error = getattr(self.context.work, 'error', None)
        if error is not None and self.phase == 'emit':
            error.context.update(self.progress())

    def cleanup(self, now):
        self.release()
        owner = self.input_owner()
        released = owner is None or not busy(owner)
        rechecked = self.recheck is None or self.recheck.cleanup(now)
        return released and rechecked and (self.target is None or self.target.cleanup(now))


class PointTask(InputTask):
    """Pointer input at one point, shared by click, move and scroll.

    With --window, x/y are client-area coordinates (a GTK header bar is client
    content; a KWin title bar is not), the window must be active with no KWin
    surface open, and the point must be inside the client area and on screen.
    Pointer events go to the surface under the pointer, not to the keyboard
    focus; requiring the window to be active is what makes sure that surface is
    the window's own (the active window is on top, except for windows KWin keeps
    above it). Without --window they are screen coordinates and nothing about
    windows is checked.
    """
    kind = 'pointer'
    budget_hint = 'Raise --timeout (max 3).'

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.x, self.y = request.arguments['x'], request.arguments['y']
        self.point = None
        self.moved = False
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def make_target(self, request, context, adapter, registry, healthy):
        if request.arguments.get('window') is None:
            return None
        return TargetTask(request, context, adapter, registry, healthy, condition='observe',
                          require_focus=True, require_client=True)

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

    def pointed(self):
        """Result fields shared by every pointer command; the pointer stays at screen_x, screen_y."""
        return {'window': self.focus and self.focus['window'], 'focused': self.focus is not None or None,
                'dispatched': True, 'query_artifact': self.focus and self.focus['query_artifact'],
                'client': self.focus and self.focus['client'],
                'x': self.x, 'y': self.y, 'screen_x': self.point[0], 'screen_y': self.point[1],
                'focus_rechecks': self.rechecks,
                'started_at': self.started_at, 'finished_at': self.finished_at or time.monotonic()}


class ClickTask(PointTask):
    """Absolute motion to one point, then COUNT clicks of one button there."""
    gap = CLICK_GAP

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.button, self.count = request.arguments['button'], request.arguments['count']
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def parse(self, request, input_owner):
        self.strokes = [[BUTTONS[self.button]]] * self.count
        self.hold = CLICK_HOLD

    def emission(self):
        return self.count * (CLICK_HOLD + CLICK_GAP + STROKE_ESTIMATE)

    def prepare(self, owner):
        owner.move(*self.point)
        self.moved = True

    def result(self):
        return self.pointed() | {'button': self.button, 'count': self.count}


class MoveTask(PointTask):
    """Absolute motion to one point and nothing else: hover. Nothing is pressed."""

    def parse(self, request, input_owner):
        self.strokes, self.hold = [], 0

    def emission(self):
        return STROKE_ESTIMATE

    def intent(self):
        return {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'motion': list(self.point)}

    def progress(self):
        return {'window': self.request.arguments.get('window'), 'pointer_moved': self.moved}

    def emit(self, owner):
        self.healthy()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        owner.move(*self.point)
        self.moved = True
        # Close the emulation the motion opened; the pointer stays where it is.
        self.release(strict=True)
        self.finished_at = time.monotonic()
        return self.result()

    def result(self):
        return self.pointed()


class ScrollTask(PointTask):
    """Absolute motion to one point, then discrete wheel steps there, SCROLL_GAP apart.

    Each step is one notch on every axis that still has steps left, so
    --dx 1 --dy 3 sends (dx, dy) = (1, 1), (0, 1), (0, 1). Positive dy is down
    and positive dx is right, as for a wheel turned toward the user without
    natural scrolling. A step and its frame are one native batch, nothing is
    held between steps, and the emulation the motion opened is closed after the
    last step or on any failure, so an interrupted scroll leaves nothing open.
    Long scrolls get the same focus rechecks as typing.
    """
    gap = SCROLL_GAP
    budget_hint = 'Raise --timeout (max 3) or scroll fewer steps per request.'

    def parse(self, request, input_owner):
        dx, dy = request.arguments['dx'], request.arguments['dy']
        sign = lambda value: (value > 0) - (value < 0)
        self.strokes = [(sign(dx) if step < abs(dx) else 0, sign(dy) if step < abs(dy) else 0)
                        for step in range(max(abs(dx), abs(dy)))]
        self.hold = 0
        self.sent = [0, 0]

    def emission(self):
        return STROKE_ESTIMATE + len(self.strokes) * SCROLL_ESTIMATE

    def intent(self):
        return {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'motion': list(self.point),
                'steps_total': len(self.strokes)}

    def progress(self):
        return {'window': self.request.arguments.get('window'), 'pointer_moved': self.moved,
                'steps_sent': self.index, 'steps_total': len(self.strokes),
                'dx_sent': self.sent[0], 'dy_sent': self.sent[1], 'focus_rechecks': self.rechecks}

    def emit(self, owner):
        if self.index == len(self.strokes):
            return self.result() if self.watch_focus(finishing=True) else None
        self.watch_focus()
        if time.monotonic() < self.next_at:
            return None
        self.healthy()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        if not self.moved:
            owner.move(*self.point)  # The wheel goes to the surface under the pointer.
            self.moved = True
        else:
            dx, dy = self.strokes[self.index]
            owner.scroll(dx, dy)
            self.index += 1
            self.sent[0] += dx
            self.sent[1] += dy
        self.next_at = time.monotonic() + self.gap
        if self.index < len(self.strokes):
            return None
        self.finished_at = time.monotonic()
        self.release(strict=True)
        return self.result() if self.watch_focus(finishing=True) else None

    def result(self):
        return self.pointed() | {'dx': self.request.arguments['dx'], 'dy': self.request.arguments['dy'],
                                 'steps': len(self.strokes)}
