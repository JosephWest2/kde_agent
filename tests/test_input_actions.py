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
from agent_desktop.input_actions import InputTask
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
        self.device = SimpleNamespace(held=[])
        self.devices = {1: self.device}
        self.uncertain = False
        self.retired_held = []
        self.events = []
        self.fail_release = False
        self.fail_press_at = None

    def press(self, codes):
        assert not self.device.held
        for index, code in enumerate(codes):
            self.device.held.append(code)  # Like Input.press: recorded before the native call.
            if self.fail_press_at == index:
                self.uncertain = True
                raise ContractError('input_failed', 'Native press failed.')
        self.events.append(('press', list(codes), time.monotonic()))

    def release(self):
        if self.fail_release:
            self.uncertain = True
            return
        self.uncertain = False
        self.events.append(('release', list(reversed(self.device.held)), time.monotonic()))
        self.device.held.clear()


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

    def test_window_without_frame_geometry_fails_cleanly(self):
        from agent_desktop.screenshots import ScreenshotTask
        request = make_request('screenshot', arguments={'window': WINDOW}, caller_cwd='/')
        context = SimpleNamespace(work=SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + 3)))
        with patch('agent_desktop.screenshots.TargetTask', Target):
            task = ScreenshotTask(request, context, None, None, None, lambda: None, lambda: 'Virtual-1', Path('/nonexistent'))
        task.target.result = {'window': WINDOW, 'frame': None, 'focused': False, 'query_artifact': 'q'}
        with self.assertRaises(ContractError) as caught:
            task.step(time.monotonic())
        self.assertEqual((caught.exception.code, caught.exception.context['reason']), ('capture_failed', 'frame_unavailable'))

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
