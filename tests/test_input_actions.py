"""Keymap, worker-timed key/type emission, screenshot crop and shutdown release."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop import cli
from agent_desktop.contracts import ContractError, make_request, response
from agent_desktop.input_actions import (CLICK_GAP, CLICK_HOLD, DRAG_SETTLE, DRAG_STEP, MODIFIER_GAP, RECHECK,
                                         SCROLL_GAP, ClickTask, DragTask, InputTask, MoveTask, ScrollTask)
from agent_desktop.keymap import KEYS, SHIFT, parse_chord, text_strokes
from agent_desktop.screenshots import crop_rect
from agent_desktop.shutdown import release_input

GEN = 'a' * 32
WINDOW = {'generation': GEN, 'window_id': '2382f322-3657-4566-8870-33e1237ab765'}


class KeymapTests(unittest.TestCase):
    def test_chords_use_physical_codes_in_order(self):
        self.assertEqual(parse_chord('ctrl+shift+t'), [29, 42, 20])
        self.assertEqual(parse_chord('Control_L+Return'), [29, 28])
        self.assertEqual(parse_chord('super+Left'), [125, 105])
        self.assertEqual(parse_chord('w'), [17])
        self.assertEqual(parse_chord('F12'), [88])
        self.assertEqual(parse_chord('KP_Enter'), [96])
        self.assertEqual(parse_chord('ctrl+/'), [29, 53])

    def test_invalid_chords_are_rejected_whole(self):
        for chord in ('', 'ctrl+', 'ctrl++', 'hyper', 'ctrl+ctrl', 'control+ctrl_l', '+'.join('abcdefghi')):
            with self.subTest(chord=chord), self.assertRaises(ContractError) as caught:
                parse_chord(chord)
            self.assertEqual(caught.exception.code, 'unsupported_input')

    def test_shifted_symbol_in_chord_explains_the_alternative(self):
        with self.assertRaises(ContractError) as caught:
            parse_chord('ctrl+!')
        self.assertIn('shift+1', caught.exception.context['hint'])

    def test_us_text_adds_shift_only_where_needed(self):
        self.assertEqual(text_strokes('aA1!'), [[30], [SHIFT, 30], [2], [SHIFT, 2]])
        self.assertEqual(text_strokes(' \n\t'), [[KEYS['space']], [KEYS['return']], [KEYS['tab']]])
        self.assertEqual(text_strokes(''), [])
        printable = ''.join(chr(c) for c in range(32, 127))
        strokes = text_strokes(printable)
        self.assertEqual(len(strokes), len(printable))
        self.assertTrue(all(len(stroke) in (1, 2) for stroke in strokes))

    def test_caps_lock_inverts_letters_only(self):
        self.assertEqual(text_strokes('aA1!', caps_lock=True), [[SHIFT, 30], [30], [2], [SHIFT, 2]])

    def test_non_ascii_lookalikes_are_rejected(self):
        kelvin = '\u212a'
        self.assertEqual(kelvin.lower(), 'k')
        with self.assertRaises(ContractError) as caught:
            text_strokes('o' + kelvin)
        self.assertEqual(caught.exception.context, {'index': 1, 'codepoint': 'U+212A'})
        with self.assertRaises(ContractError):
            parse_chord('ctrl+' + kelvin)

    def test_unsupported_character_rejects_everything_with_its_position(self):
        with self.assertRaises(ContractError) as caught:
            text_strokes('ok café')
        self.assertEqual(caught.exception.context, {'index': 6, 'codepoint': 'U+00E9'})


class Owner:
    """Fake input owner with the ledger fields InputTask relies on."""

    def __init__(self):
        self.device = SimpleNamespace(held=[], emulating=False)
        self.fail_press_before_record = False
        self.devices = {1: self.device}
        self.uncertain = False
        self.retired_held = []
        self.events = []
        self.fail_release = False
        self.fail_press_at = None
        self.fail_scroll_at = None

    def press(self, codes, kind='keyboard'):
        assert not self.device.held
        self.kinds = getattr(self, 'kinds', []) + [kind]
        if self.fail_press_before_record:
            raise ContractError('input_unavailable', 'Input changed during emission.')
        self.device.emulating = True
        for index, code in enumerate(codes):
            self.device.held.append(code)  # Like Input.press: recorded before the native call.
            if self.fail_press_at == index:
                self.uncertain = True
                raise ContractError('input_failed', 'Native press failed.')
        self.events.append(('press', list(codes), time.monotonic()))

    def move(self, x, y):
        self.device.emulating = True  # Like Input.move: emulation opens before motion.
        self.events.append(('move', (x, y), time.monotonic()))

    def scroll(self, dx, dy):
        assert not self.device.held and (dx, dy) != (0, 0) and {dx, dy} <= {-1, 0, 1}
        self.device.emulating = True
        if self.fail_scroll_at == sum(event[0] == 'scroll' for event in self.events):
            self.uncertain = True  # Like Input.scroll: a failed native call leaves the connection uncertain.
            raise ContractError('input_failed', 'Native scroll failed.')
        self.events.append(('scroll', (dx, dy), time.monotonic()))

    def release(self):
        if self.fail_release:
            self.uncertain = True
            return
        self.uncertain = False
        if self.device.held:
            self.events.append(('release', list(reversed(self.device.held)), time.monotonic()))
        else:
            self.events.append(('stop_emulating', [], time.monotonic()))
        self.device.held.clear()
        self.device.emulating = False


class Target:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.result = {'window': WINDOW, 'focused': True, 'query_artifact': 'window-observations/q.json'}
        self.error = None
        self.cancelled = None

    def step(self, now):
        if self.error is not None:
            raise self.error
        return self.result

    def request_cancel(self, reason):
        self.cancelled = reason

    def cleanup(self, now):
        return True


class InputTaskTests(unittest.TestCase):
    def make(self, operation, timeout=None, **arguments):
        request = make_request(operation, arguments={'window': WINDOW, **arguments}, caller_cwd='/',
                               timeout_seconds=timeout)
        self.effects = []
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None)
        context = SimpleNamespace(work=work, effects=lambda partial, uncertain=False: self.effects.append((partial, uncertain)))
        self.owner = Owner()
        with patch('agent_desktop.input_actions.TargetTask', Target):
            task = InputTask(request, context, None, lambda: self.owner, None, lambda: None)
        return task

    def run_task(self, task, limit=5):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            result = task.step(time.monotonic())
            if result is not None:
                return result
            time.sleep(.001)
        self.fail('task did not finish')

    def test_target_requires_focus_and_parse_happens_before_observation(self):
        task = self.make('key', chord='ctrl+s', hold=.01)
        self.assertEqual(task.target.kwargs, {'condition': 'observe', 'require_focus': True})
        with self.assertRaises(ContractError):
            self.make('key', chord='nope', hold=.01)

    def test_key_holds_for_the_requested_time_then_releases_in_reverse(self):
        task = self.make('key', chord='ctrl+shift+t', hold=.05)
        result = self.run_task(task)
        (kind1, codes1, at1), (kind2, codes2, at2) = self.owner.events
        self.assertEqual((kind1, codes1, kind2, codes2), ('press', [29, 42, 20], 'release', [20, 42, 29]))
        self.assertGreaterEqual(at2 - at1, .05)
        self.assertEqual(result['codes'], [29, 42, 20])
        self.assertTrue(result['dispatched'])
        self.assertEqual(self.effects[0][0]['phase'], 'emitting')
        self.assertTrue(self.effects[0][1])

    def test_type_sends_one_stroke_at_a_time(self):
        task = self.make('type', text='Hi!')
        result = self.run_task(task)
        presses = [codes for kind, codes, _ in self.owner.events if kind == 'press']
        self.assertEqual(presses, [[SHIFT, 35], [23], [SHIFT, 2]])
        self.assertEqual([kind for kind, _, _ in self.owner.events], ['press', 'release'] * 3)
        self.assertEqual((result['characters'], result['strokes']), (3, 3))

    def test_cancel_during_hold_releases_immediately(self):
        task = self.make('key', chord='shift', hold=2)
        while not self.owner.device.held:
            task.step(time.monotonic())
        task.request_cancel('cancelled')
        self.assertEqual(self.owner.device.held, [])
        self.assertEqual(self.owner.events[-1][0], 'release')
        self.assertEqual(task.target.cancelled, 'cancelled')
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_unconfirmed_release_keeps_cleanup_pending_and_blocks_later_input(self):
        task = self.make('key', chord='a', hold=.01)
        self.owner.fail_release = True
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.code, 'input_uncertain')
        self.assertFalse(task.cleanup(time.monotonic()))
        later = self.make('type', text='x')
        self.owner.uncertain = True
        with self.assertRaises(ContractError) as caught:
            self.run_task(later)
        self.assertEqual(caught.exception.code, 'input_uncertain')
        self.assertEqual(self.owner.events, [])

    def test_partially_failed_press_is_still_released(self):
        task = self.make('key', chord='ctrl+a', hold=.01)
        self.owner.fail_press_at = 1
        with self.assertRaises(ContractError):
            self.run_task(task)
        self.assertEqual(self.owner.device.held, [])
        self.assertEqual(self.owner.events, [('release', [30, 29], self.owner.events[0][2])])
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_health_check_that_consumes_the_deadline_prevents_emission(self):
        task = self.make('key', chord='a', hold=.01)
        def slow():
            task.deadline = time.monotonic()
        task.healthy = slow
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(self.owner.events, [])

    def test_caps_lock_state_follows_completed_key_presses(self):
        task = self.make('key', chord='caps_lock', hold=.01)
        owner = self.owner
        self.run_task(task)
        self.assertTrue(owner.caps_lock)
        with patch('agent_desktop.input_actions.TargetTask', Target):
            typed = InputTask(make_request('type', arguments={'window': WINDOW, 'text': 'Hi'}, caller_cwd='/'),
                              task.context, None, lambda: owner, None, lambda: None)
        self.assertEqual(typed.strokes, [[35], [SHIFT, 23]])

    def test_text_that_cannot_fit_the_deadline_sends_nothing(self):
        task = self.make('type', text='x' * 400)
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(caught.exception.context['phase'], 'budget')
        self.assertEqual(self.owner.events, [])
        self.assertEqual(self.effects, [])

    def test_focus_failure_sends_nothing(self):
        task = self.make('key', chord='a', hold=.01)
        task.target.error = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual(caught.exception.context['reason'], 'focus_lost')
        self.assertEqual(self.owner.events, [])

    def test_type_timeout_ceiling_is_30_seconds(self):
        self.assertEqual(make_request('type', arguments={'window': WINDOW, 'text': 'x'}, caller_cwd='/',
                                      timeout_seconds=30).timeout_seconds, 30)
        with self.assertRaises(ContractError):
            make_request('type', arguments={'window': WINDOW, 'text': 'x'}, caller_cwd='/', timeout_seconds=31)


class ClickTaskTests(unittest.TestCase):
    CLIENT = {'x': 290, 'y': 100, 'width': 700, 'height': 520}
    run_task = InputTaskTests.run_task

    def click(self, window=True, timeout=None, **arguments):
        if window:
            arguments['window'] = WINDOW
        request = make_request('click', arguments=arguments, caller_cwd='/', timeout_seconds=timeout)
        self.effects = []
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None)
        context = SimpleNamespace(work=work, effects=lambda partial, uncertain=False: self.effects.append((partial, uncertain)))
        self.owner = Owner()
        with patch('agent_desktop.input_actions.TargetTask', Target):
            task = ClickTask(request, context, None, lambda: self.owner, None, lambda: None)
        if task.target is not None:
            task.target.result = dict(task.target.result, client=dict(self.CLIENT))
        return task

    def test_window_click_maps_client_coordinates_and_requires_focus_and_client(self):
        task = self.click(x=107, y=23)
        self.assertEqual(task.target.kwargs, {'condition': 'observe', 'require_focus': True, 'require_client': True})
        result = self.run_task(task)
        self.assertEqual([event[:2] for event in self.owner.events],
                         [('move', (397, 123)), ('press', [0x110]), ('release', [0x110])])
        self.assertEqual(self.owner.kinds, ['pointer'])
        self.assertGreaterEqual(self.owner.events[2][2] - self.owner.events[1][2], CLICK_HOLD)
        self.assertEqual((result['screen_x'], result['screen_y'], result['button'], result['count']),
                         (397, 123, 'left', 1))
        self.assertEqual(result['client'], self.CLIENT)

    def test_double_click_moves_once_and_spaces_the_clicks(self):
        result = self.run_task(self.click(x=5, y=5, button='right', count=2))
        kinds = [event[0] for event in self.owner.events]
        self.assertEqual(kinds, ['move', 'press', 'release', 'press', 'release'])
        self.assertEqual(self.owner.events[1][1], [0x111])
        self.assertGreaterEqual(self.owner.events[3][2] - self.owner.events[2][2], CLICK_GAP)
        self.assertEqual(result['count'], 2)

    def test_point_outside_the_client_area_sends_nothing(self):
        for x, y in ((700, 0), (0, 520), (5000, 5)):
            with self.subTest(x=x, y=y):
                task = self.click(x=x, y=y)
                with self.assertRaises(ContractError) as caught:
                    task.step(time.monotonic())
                self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                                 ('invalid_arguments', 'outside_window'))
                self.assertEqual(self.owner.events, [])
                self.assertEqual(self.effects, [])

    def test_client_point_that_is_offscreen_sends_nothing(self):
        self.CLIENT = {'x': 1000, 'y': 600, 'width': 700, 'height': 520}
        task = self.click(x=400, y=10)
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual(caught.exception.context['reason'], 'outside_screen')
        self.assertEqual(self.owner.events, [])

    def test_screen_click_skips_targeting_and_checks_screen_bounds(self):
        task = self.click(window=False, x=1279, y=719, button='middle')
        self.assertIsNone(task.target)
        result = self.run_task(task)
        self.assertEqual(self.owner.events[0][:2], ('move', (1279, 719)))
        self.assertEqual(self.owner.events[1][1], [0x112])
        self.assertIsNone(result['window'])
        self.assertTrue(task.cleanup(time.monotonic()))
        for x, y in ((1280, 0), (0, 720)):
            with self.subTest(x=x, y=y), self.assertRaises(ContractError) as caught:
                self.click(window=False, x=x, y=y).step(time.monotonic())
            self.assertEqual(caught.exception.context['reason'], 'outside_screen')

    def test_cancel_during_click_hold_releases_the_button(self):
        task = self.click(x=1, y=1)
        task.step(time.monotonic())
        self.assertEqual(self.owner.device.held, [0x110])
        task.request_cancel('client_disconnected')
        self.assertEqual(self.owner.device.held, [])
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_press_failure_after_motion_still_closes_emulation(self):
        task = self.click(x=1, y=1)
        self.owner.fail_press_before_record = True
        with self.assertRaises(ContractError):
            task.step(time.monotonic())
        self.assertEqual([event[0] for event in self.owner.events], ['move', 'stop_emulating'])
        self.assertFalse(self.owner.device.emulating)
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_shutdown_backstop_closes_motion_only_emulation(self):
        owner = Owner()
        owner.move(1, 1)
        self.assertEqual(release_input(owner), {'state': 'released', 'confirmed': True})
        self.assertFalse(owner.device.emulating)

    def test_click_arguments_are_validated(self):
        request = make_request('click', arguments={'x': '3', 'y': 4, 'count': '2'}, caller_cwd='/')
        self.assertEqual((request.arguments['x'], request.arguments['count'], request.arguments['button']),
                         (3, 2, 'left'))
        for bad in ({'count': 0}, {'count': 4}, {'count': '2.0'}, {'count': True}, {'button': 'back'},
                    {'x': -1}, {'x': '1.5'}):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                make_request('click', arguments={'x': 1, 'y': 1, **bad}, caller_cwd='/')


class PointerMotionTests(unittest.TestCase):
    """move and scroll: the point rules are click's; scroll adds paced wheel steps."""
    CLIENT = {'x': 290, 'y': 100, 'width': 700, 'height': 520}
    run_task = InputTaskTests.run_task

    def make(self, operation, window=True, plan=(), timeout=None, **arguments):
        if window:
            arguments['window'] = WINDOW
        request = make_request(operation, arguments=arguments, caller_cwd='/', timeout_seconds=timeout)
        self.effects = []
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None)
        context = SimpleNamespace(work=work, effects=lambda partial, uncertain=False: self.effects.append((dict(partial), uncertain)))
        self.owner = Owner()
        Recheck.created, Recheck.plan = [], list(plan)
        self.patch = patch('agent_desktop.input_actions.TargetTask', Recheck)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        task = {'move': MoveTask, 'scroll': ScrollTask}[operation](request, context, None, lambda: self.owner,
                                                                   None, lambda: None)
        if task.target is not None:
            task.target.result = dict(task.target.result, client=dict(self.CLIENT))
        return task

    def kinds(self):
        return [event[:2] for event in self.owner.events]

    def test_window_move_maps_client_coordinates_requires_focus_and_closes_emulation(self):
        task = self.make('move', x=107, y=23)
        self.assertEqual(task.target.kwargs, {'condition': 'observe', 'require_focus': True, 'require_client': True})
        result = self.run_task(task)
        self.assertEqual(self.kinds(), [('move', (397, 123)), ('stop_emulating', [])])
        self.assertFalse(self.owner.device.emulating)
        self.assertEqual({key: result[key] for key in ('x', 'y', 'screen_x', 'screen_y', 'focused', 'dispatched')},
                         {'x': 107, 'y': 23, 'screen_x': 397, 'screen_y': 123, 'focused': True, 'dispatched': True})
        self.assertEqual(result['client'], self.CLIENT)
        self.assertNotIn('button', result)
        self.assertEqual(self.effects, [({'window': WINDOW, 'phase': 'emitting', 'motion': [397, 123]}, True)])
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_screen_move_and_scroll_skip_targeting_and_check_screen_bounds(self):
        for operation, extra in (('move', {}), ('scroll', {'dy': 1})):
            with self.subTest(operation=operation):
                task = self.make(operation, window=False, x=1279, y=0, **extra)
                self.assertIsNone(task.target)
                result = self.run_task(task)
                self.assertEqual(self.owner.events[0][:2], ('move', (1279, 0)))
                self.assertEqual((result['window'], result['focused'], result['client']), (None, None, None))
                for x, y in ((1280, 0), (0, 720)):
                    with self.assertRaises(ContractError) as caught:
                        self.make(operation, window=False, x=x, y=y, **extra).step(time.monotonic())
                    self.assertEqual(caught.exception.context['reason'], 'outside_screen')
                    self.assertEqual(self.owner.events, [])

    def test_points_outside_the_client_area_send_nothing(self):
        for operation, extra in (('move', {}), ('scroll', {'dy': -2})):
            for x, y in ((700, 0), (0, 520)):
                with self.subTest(operation=operation, x=x, y=y):
                    task = self.make(operation, x=x, y=y, **extra)
                    with self.assertRaises(ContractError) as caught:
                        task.step(time.monotonic())
                    self.assertEqual(caught.exception.context['reason'], 'outside_window')
                    self.assertEqual((self.owner.events, self.effects), ([], []))

    def test_focus_and_compositor_surface_refusals_send_nothing(self):
        refusals = (ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'}),
                    ContractError('target_lost', 'A compositor surface has input.',
                                  context={'reason': 'compositor_surface_open', 'blocking_windows': []}))
        for operation, extra in (('move', {}), ('scroll', {'dy': 3})):
            for refusal in refusals:
                with self.subTest(operation=operation, reason=refusal.context['reason']):
                    task = self.make(operation, x=5, y=5, **extra)
                    task.target.error = refusal
                    with self.assertRaises(ContractError) as caught:
                        task.step(time.monotonic())
                    self.assertIs(caught.exception, refusal)
                    self.assertNotIn('steps_sent', caught.exception.context)
                    self.assertEqual((self.owner.events, self.effects), ([], []))

    def test_scroll_moves_first_then_sends_signed_single_notches_paced_apart(self):
        task = self.make('scroll', x=10, y=20, dy=-3)
        result = self.run_task(task)
        self.assertEqual(self.kinds(), [('move', (300, 120))] + [('scroll', (0, -1))] * 3 + [('stop_emulating', [])])
        times = [event[2] for event in self.owner.events[:4]]
        self.assertTrue(all(b - a >= SCROLL_GAP for a, b in zip(times, times[1:])))
        self.assertEqual((result['dx'], result['dy'], result['steps']), (0, -3, 3))
        self.assertEqual((result['screen_x'], result['screen_y']), (300, 120))
        self.assertEqual(self.effects, [({'window': WINDOW, 'phase': 'emitting', 'motion': [300, 120],
                                          'steps_total': 3}, True)])
        self.assertFalse(self.owner.device.emulating)

    def test_diagonal_scroll_steps_both_axes_until_the_shorter_one_is_done(self):
        self.run_task(self.make('scroll', window=False, x=1, y=1, dx=2, dy=-4))
        self.assertEqual([event[1] for event in self.owner.events if event[0] == 'scroll'],
                         [(1, -1), (1, -1), (0, -1), (0, -1)])
        self.run_task(self.make('scroll', window=False, x=1, y=1, dx=-1))
        self.assertEqual([event[1] for event in self.owner.events if event[0] == 'scroll'], [(-1, 0)])

    def test_scroll_budget_covers_fifty_steps_inside_the_default_timeout(self):
        task = self.make('scroll', x=1, y=1, dx=50, dy=-50)
        self.assertEqual(len(task.strokes), 50)
        self.assertLess(task.estimate() + .1, 3)
        self.assertGreater(task.estimate(), 50 * SCROLL_GAP)
        with self.assertRaises(ContractError) as caught:
            self.make('scroll', x=1, y=1, dy=50, timeout=.5).step(time.monotonic())
        self.assertEqual((caught.exception.code, caught.exception.context['phase']), ('timeout', 'budget'))
        self.assertIn('fewer steps', caught.exception.context['hint'])
        self.assertEqual(self.owner.events, [])

    def test_native_failure_midway_reports_steps_sent_and_closes_emulation(self):
        task = self.make('scroll', x=1, y=1, dx=-1, dy=5)
        self.owner.fail_scroll_at = 2
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        context = caught.exception.context
        self.assertEqual({key: context[key] for key in ('steps_sent', 'steps_total', 'dx_sent', 'dy_sent', 'pointer_moved')},
                         {'steps_sent': 2, 'steps_total': 5, 'dx_sent': -1, 'dy_sent': 2, 'pointer_moved': True})
        self.assertEqual(self.effects[0][1], True)  # Marked uncertain before the motion: outcome "unknown".
        self.assertEqual(self.owner.events[-1][0], 'stop_emulating')
        self.assertFalse(self.owner.device.emulating)
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_focus_loss_mid_scroll_stops_with_progress(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        task = self.make('scroll', plan=[{'error': lost}], x=1, y=1, dy=40)
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        context = caught.exception.context
        self.assertEqual((caught.exception.code, context['reason']), ('target_lost', 'focus_lost'))
        self.assertTrue(0 < context['steps_sent'] < 40)
        self.assertEqual((context['dy_sent'], context['dx_sent'], context['focus_rechecks']),
                         (context['steps_sent'], 0, 0))
        self.assertEqual(self.owner.events[-1][0], 'stop_emulating')
        self.assertFalse(self.owner.device.emulating)

    def test_long_scroll_is_rechecked_and_succeeds_while_focus_holds(self):
        result = self.run_task(self.make('scroll', plan=[{}] * 10, x=1, y=1, dy=30))
        self.assertGreaterEqual(result['focus_rechecks'], 1)
        self.assertEqual(sum(event[0] == 'scroll' for event in self.owner.events), 30)

    def test_cancel_mid_scroll_stops_and_closes_emulation_with_progress(self):
        task = self.make('scroll', window=False, x=1, y=1, dy=20)
        task.context.work.error = ContractError('cancelled', 'Request interrupted.')
        end = time.monotonic() + 5
        while sum(event[0] == 'scroll' for event in self.owner.events) < 2 and time.monotonic() < end:
            task.step(time.monotonic()); time.sleep(.001)
        task.request_cancel('client_disconnected')
        self.assertFalse(self.owner.device.emulating)
        self.assertEqual(task.context.work.error.context['steps_sent'], 2)
        self.assertEqual(task.context.work.error.context['dy_sent'], 2)
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_motion_failure_reports_that_nothing_was_scrolled(self):
        task = self.make('scroll', x=1, y=1, dy=2)
        self.owner.move = Mock(side_effect=ContractError('input_unavailable', 'Input changed during emission.'))
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual((caught.exception.context['steps_sent'], caught.exception.context['pointer_moved']), (0, False))


class Recheck(Target):
    """A focus recheck: answers after DELAY seconds, or raises ERROR when it answers."""
    created = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = time.monotonic()
        self.delay, self.clean = .01, True
        Recheck.created.append(self)
        if len(Recheck.created) > 1:  # The first instance is the initial target check.
            plan = Recheck.plan.pop(0) if Recheck.plan else {}
            self.delay = plan.get('delay', .01)
            self.error = plan.get('error')
            self.clean = plan.get('clean', True)

    def step(self, now):
        if self is not Recheck.created[0] and time.monotonic() - self.started < self.delay:
            return None
        return super().step(now)

    def cleanup(self, now):
        return self.clean or self.cancelled is not None and time.monotonic() - self.started > .05


class FocusRecheckTests(unittest.TestCase):
    run_task = InputTaskTests.run_task

    def make(self, operation, plan, **arguments):
        Recheck.created, Recheck.plan = [], list(plan)
        request = make_request(operation, arguments={'window': WINDOW, **arguments}, caller_cwd='/')
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None)
        context = SimpleNamespace(work=work, effects=lambda partial, uncertain=False: None)
        self.owner = Owner()
        with patch('agent_desktop.input_actions.TargetTask', Recheck):
            task = InputTask(request, context, None, lambda: self.owner, None, lambda: None)
        self.patch = patch('agent_desktop.input_actions.TargetTask', Recheck)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return task

    def test_long_hold_is_rechecked_and_focus_loss_releases_early(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        task = self.make('key', [{}, {'error': lost}], chord='shift+w', hold=1.5)
        started = time.monotonic()
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        elapsed = time.monotonic() - started
        self.assertEqual((caught.exception.code, caught.exception.context['reason']), ('target_lost', 'focus_lost'))
        self.assertEqual(caught.exception.context['focus_rechecks'], 1)
        self.assertEqual(self.owner.device.held, [])
        self.assertLess(elapsed, 3 * RECHECK)  # Released at the second recheck, not after 1.5s.
        self.assertEqual([event[0] for event in self.owner.events], ['press', 'release'])

    def test_focus_loss_mid_typing_stops_with_progress(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        task = self.make('type', [{'error': lost}], text='a' * 100)
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        sent = caught.exception.context['strokes_sent']
        self.assertGreater(sent, 0)
        self.assertLess(sent, 100)
        self.assertEqual(self.owner.device.held, [])

    def test_compositor_surface_mid_typing_reports_progress_after_uncertain_effects(self):
        # KWin's window menu opening during `type` is caught by a recheck, not
        # before the first stroke: progress is kept and effects were already
        # marked uncertain, so the scheduler reports outcome "unknown".
        blocked = ContractError('target_lost', 'A compositor surface (such as the window menu) has input.',
                                context={'reason': 'compositor_surface_open', 'blocking_windows': []})
        task = self.make('type', [{'error': blocked}], text='a' * 100)
        effects = []
        task.context.effects = lambda partial, uncertain=False: effects.append((dict(partial), uncertain))
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        context = caught.exception.context
        self.assertEqual((caught.exception.code, context['reason']), ('target_lost', 'compositor_surface_open'))
        self.assertTrue(0 < context['strokes_sent'] < 100)
        self.assertEqual(context['strokes_total'], 100)
        # The error leaves the outcome to the scheduler, which keeps the "unknown"
        # that the uncertain effect record set before the first stroke.
        self.assertEqual(caught.exception.outcome, 'not_started')
        self.assertEqual([(partial['phase'], partial['strokes_total'], uncertain) for partial, uncertain in effects],
                         [('emitting', 100, True)])
        self.assertEqual(self.owner.device.held, [])

    def test_short_input_needs_no_recheck(self):
        result = self.run_task(self.make('key', [], chord='a', hold=.01))
        self.assertEqual(result['focus_rechecks'], 0)
        self.assertEqual(len(Recheck.created), 1)

    def test_result_waits_for_an_in_flight_recheck_to_finish(self):
        # A slow recheck outlives the hold; the result waits for it rather than cancelling it.
        task = self.make('key', [{'delay': .3}], chord='a', hold=.6)
        result = self.run_task(task)
        recheck = Recheck.created[1]
        self.assertIsNone(recheck.cancelled)
        self.assertEqual(result['focus_rechecks'], 1)
        self.assertGreaterEqual(time.monotonic() - recheck.started, .3)
        self.assertEqual(len(Recheck.created), 2)

    def test_late_focus_loss_is_reported_even_after_every_stroke(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        # The recheck starts at .25s and answers at .65s, after the .55s hold ended.
        task = self.make('key', [{'delay': .4, 'error': lost}], chord='a', hold=.55)
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        context = caught.exception.context
        self.assertEqual((context['strokes_sent'], context['key_held']), (context['strokes_total'], False))
        self.assertEqual(self.owner.device.held, [])

    def test_slow_typing_keeps_rechecking_until_near_the_end(self):
        # Typing at twice the estimated pace must not stop rechecking at the estimated end.
        task = self.make('type', [{}] * 20, text='a' * 60)
        task.gap = .02
        self.run_task(task)
        elapsed = time.monotonic() - task.started_at
        self.assertGreater(elapsed, 1.5 * task.emission())
        last_start = Recheck.created[-1].started - task.started_at
        self.assertGreater(last_start, elapsed - 3 * RECHECK)

    def test_vanished_window_is_target_lost(self):
        task = self.make('key', [{}], chord='a', hold=.6)
        self.run_task(task)  # The recheck carries the window already seen.
        self.assertEqual(Recheck.created[1].selected, WINDOW)

    def test_no_recheck_starts_when_input_is_about_to_end(self):
        result = self.run_task(self.make('key', [], chord='a', hold=.6))
        # One recheck at .25s; none at ~.5s because the hold ends within RECHECK of it.
        self.assertEqual(result['focus_rechecks'], 1)
        self.assertEqual(len(Recheck.created), 2)

    def test_budget_includes_one_recheck_query(self):
        task = self.make('key', [], chord='a', hold=1)
        self.assertAlmostEqual(task.estimate(), task.emission() + .25)
        self.assertEqual(self.make('key', [], chord='a', hold=.1).estimate(), self.make('key', [], chord='a', hold=.1).emission())

    def test_cancel_cancels_and_reaps_the_recheck(self):
        task = self.make('key', [{'delay': 60, 'clean': False}], chord='a', hold=1)
        end = time.monotonic() + RECHECK + .1
        while time.monotonic() < end:
            task.step(time.monotonic()); time.sleep(.005)
        task.request_cancel('client_disconnected')
        self.assertEqual(Recheck.created[1].cancelled, 'client_disconnected')
        self.assertEqual(self.owner.device.held, [])
        self.assertFalse(task.cleanup(time.monotonic()) and time.monotonic() - Recheck.created[1].started < .05)
        time.sleep(.06)
        self.assertTrue(task.cleanup(time.monotonic()))


class LayeredOwner:
    """Fake input owner with a keyboard and a pointer device and Input's ledger rules.

    Pointer input may only run under exactly the modifiers (and, for motion,
    the button) its caller names; release() goes pointer first. A lost device
    keeps what it holds and makes input uncertain, as Input.release does.
    """

    def __init__(self):
        self.keyboard = SimpleNamespace(kind='keyboard', held=[], emulating=False, resumed=True)
        self.pointer = SimpleNamespace(kind='pointer', held=[], emulating=False, resumed=True)
        self.devices = {1: self.keyboard, 2: self.pointer}
        self.uncertain = False
        self.retired_held = []
        self.events = []

    def check(self, modifiers=(), button=None):
        if (self.keyboard.held != list(modifiers) or self.pointer.held != ([] if button is None else [button])
                or self.uncertain or self.retired_held):
            raise ContractError('input_unavailable', 'Previous input remains unresolved.')

    def press(self, codes, kind='keyboard', *, modifiers=()):
        assert not modifiers or kind == 'pointer'
        self.check(modifiers)
        device = self.pointer if kind == 'pointer' else self.keyboard
        device.emulating = True
        device.held.extend(codes)
        self.events.append(('press', kind, list(codes), time.monotonic()))

    def move(self, x, y, *, modifiers=(), button=None):
        self.check(modifiers, button)
        self.pointer.emulating = True
        self.events.append(('move', (x, y), list(modifiers), time.monotonic()))

    def scroll(self, dx, dy, *, modifiers=()):
        self.check(modifiers)
        self.pointer.emulating = True
        self.events.append(('scroll', (dx, dy), list(modifiers), time.monotonic()))

    def release(self, kind=None):
        for device in (self.pointer, self.keyboard):
            if kind is not None and device.kind != kind or not (device.held or device.emulating):
                continue
            if not device.resumed:
                self.uncertain = True  # KWin can no longer take this device's release.
                continue
            self.events.append(('release', device.kind, list(reversed(device.held)), time.monotonic()))
            device.held.clear()
            device.emulating = False

    def kinds(self):
        return [event[:3] for event in self.events]


class LayeredPointerTests(unittest.TestCase):
    """drag, and click and scroll with --modifiers, against Input's ledger rules."""
    CLIENT = {'x': 290, 'y': 100, 'width': 700, 'height': 520}
    run_task = InputTaskTests.run_task

    def make(self, operation, plan=(), timeout=None, **arguments):
        request = make_request(operation, arguments={'window': WINDOW, **arguments}, caller_cwd='/',
                               timeout_seconds=timeout)
        self.effects = []
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None)
        context = SimpleNamespace(work=work, effects=lambda partial, uncertain=False: self.effects.append((dict(partial), uncertain)))
        self.owner = LayeredOwner()
        Recheck.created, Recheck.plan = [], list(plan)
        self.patch = patch('agent_desktop.input_actions.TargetTask', Recheck)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        task = {'click': ClickTask, 'scroll': ScrollTask, 'drag': DragTask}[operation](
            request, context, None, lambda: self.owner, None, lambda: None)
        task.target.result = dict(task.target.result, client=dict(self.CLIENT))
        return task

    def drag_until(self, task, motions):
        end = time.monotonic() + 5
        while sum(event[0] == 'move' for event in self.owner.events) < motions and time.monotonic() < end:
            self.assertIsNone(task.step(time.monotonic()))
            time.sleep(.001)

    def assert_all_released(self):
        self.assertEqual((self.owner.keyboard.held, self.owner.pointer.held), ([], []))
        self.assertFalse(self.owner.keyboard.emulating or self.owner.pointer.emulating)
        releases = [event[1] for event in self.owner.events if event[0] == 'release']
        self.assertEqual(releases[-2:], ['pointer', 'keyboard'])  # The button before the modifiers.

    def test_drag_presses_modifiers_then_button_moves_linearly_and_releases_in_reverse(self):
        task = self.make('drag', **{'from': [10, 20], 'to': [110, 70]}, duration=100, modifiers=['ctrl', 'shift'])
        result = self.run_task(task)
        start, end = (300, 120), (400, 170)
        path = [(300 + 10 * i, 120 + 5 * i) for i in range(1, 11)]  # 100ms / 10ms = 10 steps.
        self.assertEqual(self.owner.kinds(),
                         [('move', start, []), ('press', 'keyboard', [29, 42]), ('press', 'pointer', [0x110])]
                         + [('move', point, [29, 42]) for point in path]
                         + [('release', 'pointer', [0x110]), ('release', 'keyboard', [42, 29])])
        times = [event[-1] for event in self.owner.events]
        self.assertGreaterEqual(times[2] - times[1], MODIFIER_GAP)
        steps = times[3:13]
        for index, at in enumerate(steps, 1):
            self.assertGreaterEqual(at - times[2], index * DRAG_STEP - .001)  # Never early.
        self.assertLess(steps[-1] - times[2], .1 + .05)
        self.assertGreaterEqual(times[13] - times[12], DRAG_SETTLE)
        self.assertGreaterEqual(times[14] - times[13], MODIFIER_GAP)
        self.assertEqual({key: result[key] for key in ('from', 'to', 'screen_from', 'screen_to', 'screen_x', 'screen_y',
                                                       'button', 'duration', 'steps', 'modifiers', 'focused')},
                         {'from': [10, 20], 'to': [110, 70], 'screen_from': [300, 120], 'screen_to': [400, 170],
                          'screen_x': 400, 'screen_y': 170, 'button': 'left', 'duration': 100, 'steps': 10,
                          'modifiers': ['ctrl', 'shift'], 'focused': True})
        self.assertEqual(self.effects, [({'window': WINDOW, 'phase': 'emitting', 'motion': [300, 120], 'to': [400, 170],
                                          'button': 'left', 'steps_total': 10, 'modifiers': ['ctrl', 'shift']}, True)])
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_drag_without_modifiers_and_step_count_rules(self):
        # (from, to, duration ms) -> steps: duration / 10ms, at most the major-axis distance, at least 1, at most 200.
        for ends, duration, steps in ((([0, 0], [300, 0]), 300, 30), (([0, 0], [5, 2]), 300, 5),
                                      (([0, 0], [0, 500]), 2000, 200), (([50, 50], [40, 45]), 0, 1),
                                      (([0, 0], [600, 1]), 5, 1), (([0, 0], [600, 1]), 15, 2)):
            with self.subTest(ends=ends, duration=duration):
                task = self.make('drag', **{'from': ends[0], 'to': ends[1]}, duration=duration, button='middle',
                                 timeout=3)
                task.step(time.monotonic())
                self.assertEqual(len(task.path), steps)
                self.assertEqual(task.path[-1], (290 + ends[1][0], 100 + ends[1][1]))
        result = self.run_task(self.make('drag', **{'from': [50, 50], 'to': [40, 45]}, duration=0, button='middle'))
        self.assertEqual(self.owner.kinds(), [('move', (340, 150), []), ('press', 'pointer', [0x112]),
                                              ('move', (330, 145), []), ('release', 'pointer', [0x112])])
        self.assertEqual((result['steps'], result['modifiers']), (1, []))

    def test_drag_ends_must_be_inside_the_client_area_and_on_screen(self):
        for ends, field, reason in ((([10, 10], [700, 10]), 'to', 'outside_window'),
                                    (([10, 520], [10, 10]), 'from', 'outside_window')):
            with self.subTest(field=field):
                task = self.make('drag', **{'from': ends[0], 'to': ends[1]})
                with self.assertRaises(ContractError) as caught:
                    task.step(time.monotonic())
                self.assertEqual((caught.exception.code, caught.exception.context['reason'],
                                  caught.exception.context['field']), ('invalid_arguments', reason, field))
                self.assertEqual((self.owner.events, self.effects), ([], []))
        self.CLIENT = {'x': 1000, 'y': 600, 'width': 700, 'height': 520}
        task = self.make('drag', **{'from': [10, 10], 'to': [300, 10]})
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual((caught.exception.context['reason'], caught.exception.context['field']), ('outside_screen', 'to'))

    def test_drag_budget_fits_the_longest_drag_and_refuses_one_that_cannot_fit(self):
        task = self.make('drag', **{'from': [0, 0], 'to': [600, 400]}, duration=2000, modifiers=['alt'])
        task.step(time.monotonic())
        self.assertLess(task.estimate() + .1, 3)
        self.assertGreater(task.estimate(), 2)
        task.request_cancel('cancelled')
        with self.assertRaises(ContractError) as caught:
            self.make('drag', **{'from': [0, 0], 'to': [600, 400]}, duration=2000, timeout=2).step(time.monotonic())
        self.assertEqual((caught.exception.code, caught.exception.context['phase']), ('timeout', 'budget'))
        self.assertIn('--duration', caught.exception.context['hint'])
        self.assertEqual(self.owner.events, [])

    def test_focus_loss_mid_drag_releases_button_then_modifiers_with_progress(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        task = self.make('drag', plan=[{'error': lost}], **{'from': [0, 0], 'to': [600, 400]}, duration=1000,
                         modifiers=['shift'])
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        context = caught.exception.context
        self.assertEqual((caught.exception.code, context['reason']), ('target_lost', 'focus_lost'))
        moves = sum(event[0] == 'move' for event in self.owner.events) - 1
        self.assertEqual((context['steps_sent'], context['steps_total']), (moves, 100))
        self.assertTrue(10 < moves < 100)
        self.assertEqual((context['pointer_moved'], context['button_pressed'], context['button_released']),
                         (True, True, False))
        self.assert_all_released()
        self.assertEqual(self.owner.events[-1][2], [42])

    def test_cancellation_and_session_stop_mid_drag_release_everything_at_once(self):
        for reason in ('client_disconnected', 'shutdown'):
            with self.subTest(reason=reason):
                task = self.make('drag', **{'from': [0, 0], 'to': [600, 400]}, duration=1000,
                                 modifiers=['ctrl', 'alt'])
                task.context.work.error = ContractError('cancelled', 'Request interrupted.')
                self.drag_until(task, 4)
                task.request_cancel(reason)
                self.assert_all_released()
                self.assertEqual(self.owner.events[-1][2], [56, 29])
                self.assertEqual(task.context.work.error.context['steps_sent'], 3)
                self.assertTrue(task.cleanup(time.monotonic()))
                # Shutdown's backstop then finds nothing left to release.
                self.assertEqual(release_input(self.owner), {'state': 'nothing_held', 'confirmed': True})

    def test_shutdown_backstop_releases_a_drag_button_before_its_modifiers(self):
        owner = LayeredOwner()
        owner.press([29]); owner.press([0x111], 'pointer', modifiers=[29])
        self.assertEqual(release_input(owner), {'state': 'released', 'confirmed': True})
        self.assertEqual([event[:3] for event in owner.events[-2:]],
                         [('release', 'pointer', [0x111]), ('release', 'keyboard', [29])])

    def test_timeout_mid_drag_releases_everything(self):
        task = self.make('drag', **{'from': [0, 0], 'to': [600, 400]}, duration=1000, modifiers=['ctrl'])
        self.drag_until(task, 5)
        task.deadline = time.monotonic()
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual((caught.exception.context['steps_sent'], caught.exception.context['button_pressed']), (4, True))
        self.assert_all_released()

    def test_device_loss_mid_drag_leaves_input_uncertain_and_blocks_later_input(self):
        task = self.make('drag', **{'from': [0, 0], 'to': [600, 400]}, duration=1000, modifiers=['ctrl'])
        self.drag_until(task, 3)
        self.owner.pointer.resumed = False  # Paused or removed: Input marks it uncertain and the session fails.
        self.owner.uncertain = True
        task.request_cancel('shutdown')
        self.assertEqual(self.owner.keyboard.held, [])     # The modifiers still came up.
        self.assertEqual(self.owner.pointer.held, [0x110])  # The button's release cannot be confirmed.
        self.assertFalse(task.cleanup(time.monotonic()))
        self.assertEqual(release_input(self.owner), {'state': 'uncertain', 'confirmed': False})
        lost = self.owner
        later = self.make('drag', **{'from': [0, 0], 'to': [10, 10]})
        later.input_owner = lambda: lost
        with self.assertRaises(ContractError) as caught:
            later.step(time.monotonic())
        self.assertEqual(caught.exception.code, 'input_uncertain')

    def test_modifier_click_keeps_modifiers_down_across_the_clicks(self):
        result = self.run_task(self.make('click', x=5, y=6, count=2, button='right', modifiers=['alt', 'ctrl']))
        self.assertEqual(self.owner.kinds(),
                         [('move', (295, 106), []), ('press', 'keyboard', [56, 29])]
                         + [('press', 'pointer', [0x111]), ('release', 'pointer', [0x111])] * 2
                         + [('release', 'keyboard', [29, 56])])
        times = [event[-1] for event in self.owner.events]
        self.assertGreaterEqual(times[2] - times[1], MODIFIER_GAP)
        self.assertGreaterEqual(times[3] - times[2], CLICK_HOLD)
        self.assertGreaterEqual(times[4] - times[3], CLICK_GAP)
        self.assertGreaterEqual(times[6] - times[5], MODIFIER_GAP)
        self.assertEqual((result['modifiers'], result['count'], result['button']), (['alt', 'ctrl'], 2, 'right'))
        self.assertEqual(self.effects[0][0]['modifiers'], ['alt', 'ctrl'])

    def test_modifier_scroll_sends_wheel_steps_under_the_held_modifiers(self):
        result = self.run_task(self.make('scroll', x=5, y=6, dy=-2, modifiers=['ctrl']))
        self.assertEqual(self.owner.kinds(),
                         [('move', (295, 106), []), ('press', 'keyboard', [29])]
                         + [('scroll', (0, -1), [29])] * 2 + [('release', 'pointer', []), ('release', 'keyboard', [29])])
        self.assertEqual(result['modifiers'], ['ctrl'])

    def test_focus_loss_mid_modifier_scroll_releases_the_modifiers(self):
        lost = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        task = self.make('scroll', plan=[{'error': lost}], x=1, y=1, dy=40, modifiers=['ctrl', 'shift'])
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertTrue(0 < caught.exception.context['steps_sent'] < 40)
        self.assert_all_released()


class ShutdownReleaseTests(unittest.TestCase):
    def test_backstop_releases_held_keys_and_reports_uncertainty(self):
        self.assertEqual(release_input(None)['state'], 'not_connected')
        owner = Owner()
        self.assertEqual(release_input(owner), {'state': 'nothing_held', 'confirmed': True})
        owner.press([42])
        self.assertEqual(release_input(owner), {'state': 'released', 'confirmed': True})
        owner.press([42])
        owner.fail_release = True
        self.assertEqual(release_input(owner), {'state': 'uncertain', 'confirmed': False})


class ScreenshotTests(unittest.TestCase):
    def test_crop_rounds_outward_and_clips_to_the_output(self):
        self.assertEqual(crop_rect({'x': 290, 'y': 100, 'width': 700, 'height': 520}), [290, 100, 700, 520])
        self.assertEqual(crop_rect({'x': 10.5, 'y': 20.2, 'width': 100, 'height': 50}), [10, 20, 101, 51])
        self.assertEqual(crop_rect({'x': -40, 'y': 600, 'width': 200, 'height': 300}), [0, 600, 160, 120])
        self.assertIsNone(crop_rect({'x': 1280, 'y': 0, 'width': 100, 'height': 100}))
        self.assertIsNone(crop_rect({'x': -200, 'y': -200, 'width': 100, 'height': 100}))

    def test_window_without_client_geometry_fails_cleanly(self):
        from agent_desktop.screenshots import ScreenshotTask
        request = make_request('screenshot', arguments={'window': WINDOW}, caller_cwd='/')
        context = SimpleNamespace(work=SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + 3)))
        with patch('agent_desktop.screenshots.TargetTask', Target):
            task = ScreenshotTask(request, context, None, None, None, lambda: None, lambda: 'Virtual-1', Path('/nonexistent'))
        task.target.result = {'window': WINDOW, 'client': None, 'frame': {'x': 0, 'y': 0, 'width': 9, 'height': 9},
                              'focused': False, 'query_artifact': 'q'}
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('capture_failed', 'client_geometry_unavailable'))

    def test_interrupted_copy_leaves_no_partial_file_and_keeps_the_capture(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'image.png'
            source.write_bytes(b'png-bytes')
            payload = response('r' * 32, 'screenshot', session='default', generation=GEN,
                               result={'capture_id': 'capture-1', 'path': str(source)})
            real_open = open
            def interrupting(path, mode='r', *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if 'x' in mode:
                    handle.write = Mock(side_effect=KeyboardInterrupt)
                return handle
            with patch('builtins.open', interrupting):
                failed = cli.copy_output(payload, str(Path(root) / 'out.png'))
            self.assertEqual(failed['error']['code'], 'cancelled')
            self.assertEqual(failed['error']['partial_result']['path'], str(source))
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['image.png'])

    def test_cli_copies_capture_to_output_file_or_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'image.png'
            source.write_bytes(b'png-bytes')
            payload = response('r' * 32, 'screenshot', session='default', generation=GEN,
                               result={'capture_id': 'capture-1', 'path': str(source)})
            copied = cli.copy_output(payload, str(Path(root) / 'out.png'))
            self.assertEqual(Path(copied['result']['output']).read_bytes(), b'png-bytes')
            folder = Path(root) / 'dir'
            folder.mkdir()
            copied = cli.copy_output(payload, str(folder))
            self.assertEqual(copied['result']['output'], str(folder / 'capture-1.png'))
            failed = cli.copy_output(payload, str(Path(root) / 'missing' / 'out.png'))
            self.assertFalse(failed['ok'])
            self.assertEqual(failed['error']['code'], 'artifact_failed')
            self.assertEqual(failed['error']['partial_result']['path'], str(source))
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['dir', 'image.png', 'out.png'])


if __name__ == '__main__':
    unittest.main()
