"""Public key, type, click, move, scroll and drag: verify the focused target, then emit worker-timed input.

The worker owns every press-to-release interval. Client disconnect, cancellation,
timeout and shutdown all reach ``request_cancel``, which releases immediately on
the owner thread. Input is refused while an earlier release is uncertain.
Focus is verified before the first stroke, then rechecked every RECHECK seconds
while a sequence or hold is still being sent. A recheck that finds the window
gone or unfocused releases everything and fails with what was already sent.

``type`` text the US layout cannot type goes instead as one input-method commit
(TypeTask; input_method.py): no keys, a single effect, then a bounded wait for
the field to report its new text, which decides ``confirmed``.
"""
import math
import time

from .capture import HEIGHT, WIDTH
from .contracts import ContractError
from .keymap import CAPS_LOCK, commit_text, parse_chord, route, text_strokes
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
# --modifiers: the left-hand keys (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT).
MODIFIERS = {'ctrl': 29, 'shift': 42, 'alt': 56}
MODIFIER_GAP = .02    # Modifiers down to the first button press or wheel step, and last release to modifiers up.
DRAG_STEP = .01       # Drag motion cadence: one absolute motion every 10ms while the button is held.
ACTIVATION_WAIT = .2   # A focused text field's input-method context is active within this, or `type` fails.
CONFIRM_WAIT = .25     # After a commit, the field's next surrounding text is awaited this long.
COMMIT_ESTIMATE = .05  # Budget for writing one commit.
DRAG_SETTLE = .02     # Last drag motion to the button release, so the release lands where the motion ended.
MAX_DRAG_STEPS = 200  # 2000ms (the --duration maximum) / DRAG_STEP.


def held(owner):
    return any(device.held for device in owner.devices.values())


def busy(owner, kind=None):
    """Held keys/buttons, or emulation left open by a motion whose press never happened.

    With KIND, only that kind's devices ('keyboard' or 'pointer') count.
    """
    return any((device.held or device.emulating) and (kind is None or device.kind == kind)
               for device in owner.devices.values())


class InputTask:
    # Each effect waits for this request's records (Context.recorded), so the first
    # step may run while the admission and start records are still queued (#96).
    gates_effects = True
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

    def release(self, *, strict=False, kind=None):
        """Release anything held now (with KIND, only that device kind). Strict mode raises if release is unconfirmed."""
        owner = self.input_owner()
        if owner is None:
            return
        # Consult the owner's ledger, not pressed_at: press() records keys as held
        # before each native call, so a partially failed press still needs release.
        if not busy(owner, kind) and not owner.uncertain:
            self.pressed_at = None
            return
        try:
            if busy(owner, kind):
                owner.release() if kind is None else owner.release(kind)
        except Exception:
            pass
        if busy(owner, kind) or owner.uncertain:
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


class TypeTask(InputTask):
    """`type`: keys for text the US layout can type, otherwise one input-method commit.

    The key route is InputTask unchanged (same strokes, pacing and checks); its
    result only adds ``method: keys``. The input-method route checks the
    focused window like keys do, then needs the input-method client's active
    context within ACTIVATION_WAIT, before anything is recorded or sent. The
    commit is one effect: once any of it was written the outcome is unknown on
    any failure, it is never retried, and ``confirmed`` (input_method.verdict)
    only reports whether the field's own text then showed it.
    """

    def __init__(self, request, context, adapter, input_owner, registry, healthy, input_method=lambda: None):
        self.method = route(request.arguments['text'], request.arguments.get('method') or 'auto')
        self.input_method = input_method
        self.client = self.commit = None
        self.activation_from = None
        self.confirm_until = None
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def parse(self, request, input_owner):
        if self.method == 'keys':
            return super().parse(request, input_owner)
        self.text = commit_text(request.arguments['text'])
        self.strokes, self.hold = [], 0

    def estimate(self):
        return super().estimate() if self.method == 'keys' else COMMIT_ESTIMATE + CONFIRM_WAIT

    def progress(self):
        if self.method == 'keys':
            return super().progress()
        return {'window': self.request.arguments.get('window'), 'method': 'input_method',
                'bytes': len(self.text), 'commit_sent': self.sent()}

    def sent(self):
        return self.commit is not None and self.commit.sent

    def intent(self):
        if self.method == 'keys':
            return super().intent()
        return {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'method': 'input_method',
                'bytes': len(self.text)}

    def step(self, now):
        try:
            return super().step(now)
        except ContractError as error:
            if self.sent() and error.partial_result is None:
                error.partial_result = self.progress()
            raise

    def advance(self):
        if self.method == 'keys':
            return super().advance()
        if self.phase == 'target':
            if self.target is not None:
                focus = self.target.step(time.monotonic())
                if focus is None:
                    return None
                self.focus = focus
            self.phase = 'context'
        if self.phase == 'context':
            if not self.context_ready():
                return None
            self.budget()
            self.owner()
            self.context.effects(self.intent(), uncertain=True)
            self.phase = 'intent'
        if self.phase == 'intent':
            if not recorded(self.context):
                return None
            self.budget()
            if self.input_method() is not self.client or not self.client.active():
                raise self.no_context()
            self.healthy()
            if time.monotonic() >= self.deadline:
                raise ContractError('timeout', 'Input deadline expired.')
            self.phase = 'emit'
            self.started_at = time.monotonic()
            self.commit = self.client.commit(self.text)
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        return self.confirm()

    def context_ready(self):
        """True once the input-method context is active; raises if it cannot be (nothing sent yet)."""
        client = self.input_method()
        if client is None or client.state in ('unavailable', 'failed', 'closed'):
            raise ContractError('input_unavailable', 'The private compositor offers no usable input method, '
                                'so only text the US layout can type is supported.',
                                context={'reason': 'input_method_unavailable',
                                         'state': None if client is None else client.state,
                                         'hint': 'Check health.input_method in session status; '
                                                 'see docs/INPUT.md.'})
        now = time.monotonic()
        if now >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')
        if not client.settled:
            return False  # A reconnect still binding; its own bound (INPUT_METHOD_SETUP) ends it.
        self.client = client
        if client.active():
            return True
        if self.activation_from is None:
            self.activation_from = now
        if now - self.activation_from < ACTIVATION_WAIT:
            return False
        raise self.no_context()

    def no_context(self):
        return ContractError('unsupported_input', 'The focused window has no active text input field, '
                             'so the text cannot be committed; nothing was sent.',
                             context={'reason': 'text_input_unavailable', 'waited_seconds': ACTIVATION_WAIT,
                                      'hint': 'Focus a text field of a Wayland application that supports '
                                              'text-input (X11 applications do not), or use ASCII text.'})

    def budget(self):
        remaining = self.deadline - time.monotonic()
        needed = self.estimate()
        if needed > remaining - MARGIN:
            raise ContractError('timeout', 'Not enough time left to send this input; nothing was sent.',
                                context={'phase': 'budget', 'estimated_seconds': round(needed, 3),
                                         'remaining_seconds': round(max(remaining, 0), 3),
                                         'hint': 'Raise --timeout (max 30).'})

    def confirm(self):
        commit = self.commit
        if not commit.flushed:
            if commit.lost:
                raise ContractError('input_unavailable', 'The input-method connection closed during the commit.',
                                    context={'reason': 'input_method_lost'}, outcome='unknown')
            return None
        now = time.monotonic()
        if self.confirm_until is None:
            self.finished_at = commit.flushed_at or now
            self.confirm_until = min(self.finished_at + CONFIRM_WAIT, self.deadline - MARGIN / 2)
        if not commit.settled() and now < self.confirm_until:
            return None
        self.client.finish(commit)
        return self.result()

    def result(self):
        if self.method == 'keys':
            return super().result() | {'method': 'keys'}
        confirmed, reason = self.commit.verdict()
        return {'window': self.focus['window'], 'focused': True, 'dispatched': True,
                'query_artifact': self.focus['query_artifact'], 'focus_rechecks': self.rechecks,
                'started_at': self.started_at, 'finished_at': self.finished_at,
                'method': 'input_method', 'characters': len(self.request.arguments['text']),
                'bytes': len(self.text), 'confirmed': confirmed, 'confirmation_reason': reason}

    def request_cancel(self, reason):
        if self.commit is not None:
            if self.sent() and self.context.work.partial is None:
                self.context.work.partial = self.progress()
            # Recall a commit not yet written; a partly written one closes the connection.
            self.client.abandon(self.commit)
        super().request_cancel(reason)


class PointTask(InputTask):
    """Pointer input at one point, shared by click, move, scroll and drag.

    With --window, x/y are client-area coordinates (a GTK header bar is client
    content; a KWin title bar is not), the window must be active with no KWin
    surface open, and the point must be inside the client area and on screen.
    Pointer events go to the surface under the pointer, not to the keyboard
    focus; requiring the window to be active is what makes sure that surface is
    the window's own (the active window is on top, except for windows KWin keeps
    above it). Without --window they are screen coordinates and nothing about
    windows is checked.

    Click, scroll and drag run in stages: one motion to the point ('position'),
    the --modifiers pressed as one keyboard batch, the command's own input
    ('body'), then, MODIFIER_GAP after the body's last release, the modifiers
    released in reverse ('modifiers_up'). The pointer input under held modifiers
    names them to the input owner, which refuses anything else held. Every
    failure, cancellation, timeout and stop releases pointer devices first, then
    the keyboard, as for any other hold.
    """
    kind = 'pointer'
    budget_hint = 'Raise --timeout (max 3).'
    position_gap = 0  # Positioning motion to the body's first emission, without modifiers.

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.x, self.y = self.anchor(request.arguments)
        self.point = None
        self.moved = False
        self.names = list(request.arguments.get('modifiers', []))
        self.codes = [MODIFIERS[name] for name in self.names]
        self.modifiers_down = False
        self.stage = 'position'
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def anchor(self, arguments):
        return arguments['x'], arguments['y']

    def make_target(self, request, context, adapter, registry, healthy):
        if request.arguments.get('window') is None:
            return None
        return TargetTask(request, context, adapter, registry, healthy, condition='observe',
                          require_focus=True, require_client=True)

    def screen(self, x, y, field=None):
        """Client (or, without --window, screen) x, y as a checked screen point."""
        where = {} if field is None else {'field': field}
        if self.focus is not None:
            client = self.focus['client']
            if not (x < client['width'] and y < client['height']):
                raise ContractError('invalid_arguments', 'Point is outside the window client area.',
                                    context={'reason': 'outside_window', **where, 'x': x, 'y': y, 'client': client})
            x, y = client['x'] + x, client['y'] + y
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            raise ContractError('invalid_arguments', 'Point is outside the screen.',
                                context={'reason': 'outside_screen', **where, 'x': x, 'y': y,
                                         'screen': [WIDTH, HEIGHT]})
        return x, y

    def locate(self):
        self.point = self.screen(self.x, self.y)

    def held_modifiers(self):
        """Keyword arguments naming the modifiers this task holds under its pointer input."""
        return {'modifiers': list(self.codes)} if self.codes else {}

    def sendable(self):
        """Checked before every press, motion and wheel step (never before a release)."""
        self.healthy()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Input deadline expired.')

    def modifier_seconds(self):
        return 2 * (MODIFIER_GAP + STROKE_ESTIMATE) if self.codes else 0

    def intent(self):
        intent = super().intent()
        return intent | {'modifiers': self.names} if self.names else intent

    def emit(self, owner):
        if self.stage == 'done':
            return self.result() if self.watch_focus(finishing=True) else None
        self.watch_focus()
        if time.monotonic() < self.next_at:
            return None
        if self.stage == 'position':
            self.sendable()
            owner.move(*self.point)
            self.moved = True
            if self.codes:
                self.sendable()
                owner.press(self.codes, 'keyboard')
                self.modifiers_down = True
            self.stage = 'body'
            self.next_at = time.monotonic() + (MODIFIER_GAP if self.codes else self.position_gap)
            if time.monotonic() < self.next_at:
                return None
        if self.stage == 'body':
            if not self.body(owner):
                return None
            if self.modifiers_down:
                self.stage = 'modifiers_up'
                self.next_at = time.monotonic() + MODIFIER_GAP
                return None
        # 'modifiers_up' (its gap has passed) or a body without modifiers that just ended.
        self.release(strict=True)  # The modifiers, in reverse, and any emulation still open.
        self.modifiers_down = False
        self.stage = 'done'
        self.finished_at = time.monotonic()
        return self.result() if self.watch_focus(finishing=True) else None

    def body(self, owner):
        """One step of the command's own input; True once it is complete and its buttons are up."""
        raise NotImplementedError

    def pointed(self):
        """Result fields shared by every pointer command; the pointer stays at screen_x, screen_y."""
        return {'window': self.focus and self.focus['window'], 'focused': self.focus is not None or None,
                'dispatched': True, 'query_artifact': self.focus and self.focus['query_artifact'],
                'client': self.focus and self.focus['client'],
                'x': self.x, 'y': self.y, 'screen_x': self.point[0], 'screen_y': self.point[1],
                'focus_rechecks': self.rechecks,
                'started_at': self.started_at, 'finished_at': self.finished_at or time.monotonic()}


class ClickTask(PointTask):
    """Absolute motion to one point, then COUNT clicks of one button there, under any --modifiers."""
    gap = CLICK_GAP

    def __init__(self, request, context, adapter, input_owner, registry, healthy):
        self.button, self.count = request.arguments['button'], request.arguments['count']
        super().__init__(request, context, adapter, input_owner, registry, healthy)

    def parse(self, request, input_owner):
        self.strokes = [[BUTTONS[self.button]]] * self.count
        self.hold = CLICK_HOLD

    def emission(self):
        return self.count * (CLICK_HOLD + CLICK_GAP + STROKE_ESTIMATE) + self.modifier_seconds()

    def body(self, owner):
        if self.pressed_at is None:
            self.sendable()
            owner.press(self.strokes[self.index], 'pointer', **self.held_modifiers())
            self.pressed_at = time.monotonic()
            return False
        if time.monotonic() - self.pressed_at < self.hold:
            return False
        # Under modifiers only the button goes up here; the modifiers stay down between clicks.
        self.release(strict=True, kind='pointer' if self.codes else None)
        self.index += 1
        if self.index == self.count:
            return True
        self.next_at = time.monotonic() + self.gap
        return False

    def result(self):
        return self.pointed() | {'button': self.button, 'count': self.count, 'modifiers': self.names}


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
    held between steps except any --modifiers, and the emulation the motion
    opened is closed after the last step or on any failure, so an interrupted
    scroll leaves nothing open. Long scrolls get the same focus rechecks as typing.
    """
    gap = SCROLL_GAP
    position_gap = SCROLL_GAP
    budget_hint = 'Raise --timeout (max 3) or scroll fewer steps per request.'

    def parse(self, request, input_owner):
        dx, dy = request.arguments['dx'], request.arguments['dy']
        sign = lambda value: (value > 0) - (value < 0)
        self.strokes = [(sign(dx) if step < abs(dx) else 0, sign(dy) if step < abs(dy) else 0)
                        for step in range(max(abs(dx), abs(dy)))]
        self.hold = 0
        self.sent = [0, 0]

    def emission(self):
        return STROKE_ESTIMATE + len(self.strokes) * SCROLL_ESTIMATE + self.modifier_seconds()

    def intent(self):
        intent = {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'motion': list(self.point),
                  'steps_total': len(self.strokes)}
        return intent | {'modifiers': self.names} if self.names else intent

    def progress(self):
        return {'window': self.request.arguments.get('window'), 'pointer_moved': self.moved,
                'steps_sent': self.index, 'steps_total': len(self.strokes),
                'dx_sent': self.sent[0], 'dy_sent': self.sent[1], 'focus_rechecks': self.rechecks}

    def body(self, owner):
        self.sendable()
        dx, dy = self.strokes[self.index]
        owner.scroll(dx, dy, **self.held_modifiers())
        self.index += 1
        self.sent[0] += dx
        self.sent[1] += dy
        self.next_at = time.monotonic() + self.gap
        return self.index == len(self.strokes)

    def result(self):
        result = self.pointed() | {'dx': self.request.arguments['dx'], 'dy': self.request.arguments['dy'],
                                   'steps': len(self.strokes)}
        return result | {'modifiers': self.names} if self.names else result


class DragTask(PointTask):
    """Press at --from, move in a straight line to --to, release there: one window, one button.

    Both ends are client-area points of the same window (--window is required),
    checked like a click's. The motion is linear, one absolute motion every
    DRAG_STEP while the button is held: N steps, where N is the duration over
    DRAG_STEP, at most the larger of the x and y distance in pixels (so each step
    moves), at least 1. Step i is due i * duration / N after the press and the
    last one lands exactly on --to; a late owner turn sends the next step at
    once without skipping any, so every drag sends all N. DRAG_SETTLE after the
    last motion the button goes up. The button's motion names it to the input
    owner, which refuses motion with anything else held.
    """
    budget_hint = 'Raise --timeout (max 3) or shorten --duration.'

    def anchor(self, arguments):
        return tuple(arguments['from'])

    def parse(self, request, input_owner):
        arguments = request.arguments
        self.button, self.duration = arguments['button'], arguments['duration'] / 1000
        self.code = BUTTONS[self.button]
        self.strokes, self.hold = [], 0
        self.path, self.end, self.interval = [], None, 0
        self.pressed = self.released = False
        self.button_at = None

    def locate(self):
        self.point = self.screen(self.x, self.y, 'from')
        self.end = self.screen(*self.request.arguments['to'], 'to')
        dx, dy = self.end[0] - self.point[0], self.end[1] - self.point[1]
        # The pixel-distance cap comes from the integer client points: a window origin
        # may be fractional, which would make dx and dy floats. Motion keeps the exact ones.
        (fx, fy), (tx, ty) = (self.x, self.y), self.request.arguments['to']
        steps = max(1, min(MAX_DRAG_STEPS, math.ceil(round(self.duration / DRAG_STEP, 6)),
                           max(abs(tx - fx), abs(ty - fy))))
        self.path = [(self.point[0] + dx * i / steps, self.point[1] + dy * i / steps) for i in range(1, steps + 1)]
        self.interval = self.duration / steps

    def emission(self):
        # Motion, press, the timed motion, settle, release; one estimate of slack for a late last step.
        return 4 * STROKE_ESTIMATE + self.duration + DRAG_SETTLE + self.modifier_seconds()

    def remaining(self, now):
        if self.released:
            return max(0, self.next_at - now) if self.modifiers_down else 0
        if self.button_at is None:
            return self.emission()
        end = self.button_at + self.duration + DRAG_SETTLE + (MODIFIER_GAP if self.codes else 0)
        return max(0, end - now)

    def intent(self):
        intent = {'window': self.focus and self.focus['window'], 'phase': 'emitting', 'motion': list(self.point),
                  'to': list(self.end), 'button': self.button, 'steps_total': len(self.path)}
        return intent | {'modifiers': self.names} if self.names else intent

    def progress(self):
        return {'window': self.request.arguments.get('window'), 'pointer_moved': self.moved,
                'button_pressed': self.pressed, 'steps_sent': self.index, 'steps_total': len(self.path),
                'button_released': self.released, 'focus_rechecks': self.rechecks}

    def body(self, owner):
        if not self.pressed:
            self.sendable()
            owner.press([self.code], 'pointer', **self.held_modifiers())
            self.pressed = True
            self.button_at = time.monotonic()
            self.next_at = self.button_at + self.interval
            return False
        if self.index < len(self.path):
            self.sendable()
            owner.move(*self.path[self.index], button=self.code, **self.held_modifiers())
            self.index += 1
            self.next_at = (self.button_at + (self.index + 1) * self.interval if self.index < len(self.path)
                            else time.monotonic() + DRAG_SETTLE)
            return False
        # The button goes up, and its emulation ends; any modifiers follow after MODIFIER_GAP.
        self.release(strict=True, kind='pointer')
        self.released = True
        return True

    def result(self):
        return {'window': self.focus['window'], 'focused': True, 'dispatched': True,
                'query_artifact': self.focus['query_artifact'], 'client': self.focus['client'],
                'from': list(self.request.arguments['from']), 'to': list(self.request.arguments['to']),
                'screen_from': list(self.point), 'screen_to': list(self.end),
                'screen_x': self.end[0], 'screen_y': self.end[1],
                'button': self.button, 'duration': self.request.arguments['duration'], 'steps': len(self.path),
                'modifiers': self.names, 'focus_rechecks': self.rechecks,
                'started_at': self.started_at, 'finished_at': self.finished_at or time.monotonic()}
