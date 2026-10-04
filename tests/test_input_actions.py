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
from agent_desktop.input_actions import CLICK_GAP, CLICK_HOLD, RECHECK, ClickTask, InputTask
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

    def test_no_recheck_starts_when_input_is_about_to_end(self):
        result = self.run_task(self.make('key', [], chord='a', hold=.6))
        # One recheck at .25s; none at ~.5s because the hold ends within RECHECK of it.
        self.assertEqual(result['focus_rechecks'], 1)
        self.assertEqual(len(Recheck.created), 2)

    def test_budget_includes_one_recheck_query(self):
        task = self.make('key', [], chord='a', hold=1)
        self.assertAlmostEqual(task.estimate(), task.emission() + .5)
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
