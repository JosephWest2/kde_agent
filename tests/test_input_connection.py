"""Production libei lifecycle, real ABI/FD ownership, and reentrant fault coverage."""
import ctypes
import os
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from gi.repository import GLib
from agent_desktop.input_connection import Input, Device, Failure
from agent_desktop import libei_binding
import host_facilities


class InputTests(unittest.TestCase):
    def make(self, native=False):
        self.log, self.cancel, self.failed = Mock(), Mock(), Mock()
        self.lib = libei_binding.load() if native else Mock()
        if not native:
            self.lib.ei_get_event.return_value = None
        with patch('agent_desktop.input_connection.binding.load', return_value=self.lib):
            value = Input('e'*32, GLib, invalidated=self.cancel, failed=self.failed, log=self.log)
        self.addCleanup(value.dispose)
        if not native:
            value.context = 1
            value.connected = True
            value.seats = {42}
            value.devices = {9: Device(9, 1, 42, resumed=True)}
            value.serial = 1
            self.lib.ei_event_get_seat.return_value = 42
            self.lib.ei_event_get_device.return_value = 9
            self.lib.ei_device_get_seat.return_value = 42
            self.lib.ei_device_ref.side_effect = lambda p: p
            self.lib.ei_seat_ref.side_effect = lambda p: p
        return value

    def events(self, kinds):
        self.lib.ei_get_event.side_effect = list(range(100, 100 + len(kinds))) + [None]
        self.lib.ei_event_get_type.side_effect = kinds

    def test_pause_257_cannot_be_overtaken_between_batches(self):
        value = self.make()
        self.events([99]*256 + [7])
        value.drain()
        self.assertTrue(value.backlog)
        with self.assertRaises(Failure): value.press([17])
        self.lib.ei_device_keyboard_key.assert_not_called()
        value.drain_once()
        self.assertFalse(value.ready())
        self.cancel.assert_called_once_with('device_paused')
        self.assertEqual(self.lib.ei_event_unref.call_count, 257)

    def test_idle_added_other_device_does_not_falsely_fail(self):
        value = self.make()
        self.events([5])
        self.lib.ei_event_get_device.return_value = 10
        value.drain()
        self.assertTrue(value.ready())
        self.events([8]); value.drain()
        self.assertFalse(value.ready())  # Two resumed keyboards are ambiguous.

    def test_removal_pointer_reuse_preserves_unique_identity_and_ledger(self):
        value = self.make()
        value.press([42, 17])
        self.events([6, 5, 8]); value.drain()
        self.assertNotEqual(value.devices[9].identity, 1)
        self.assertTrue(value.uncertain)
        self.assertEqual(value.retired_held[0]['keys'], [42, 17])
        with self.assertRaises(Failure): value.press([17])
        self.lib.ei_device_unref.assert_called_once_with(9)

    def test_pause_resume_retains_uncertainty(self):
        value = self.make(); value.press([17])
        self.cancel.side_effect = lambda cause: value.release()
        self.events([7, 8]); value.drain()
        self.assertTrue(value.devices[9].resumed)
        self.assertTrue(value.uncertain)
        self.assertFalse(value.ready())

    def test_dispose_from_event_callback_does_not_touch_freed_context(self):
        value = self.make(); self.events([7, 8])
        self.cancel.side_effect = lambda cause: value.dispose()
        value.drain()
        self.assertEqual(self.lib.ei_get_event.call_count, 1)
        self.lib.ei_event_unref.assert_called_once_with(100)
        self.assertIsNone(value.idle_watch)

    def test_replace_from_callback_stale_continuation_leaves_new_sources(self):
        value = self.make(); old_epoch, old_context = value.epoch, value.context
        def replace(cause):
            value.dispose()
            value.context = 2
            value.watch = GLib.timeout_add(60000, lambda: True)
            value.idle_watch = GLib.timeout_add(60000, lambda: True)
        self.cancel.side_effect = replace
        self.events([7]); value.drain()
        sources = (value.watch, value.idle_watch)
        self.assertFalse(value.on_fd(7, GLib.IO_HUP, old_epoch, old_context))
        self.assertFalse(value.drain_once(old_epoch, old_context))
        self.assertEqual(sources, (value.watch, value.idle_watch))

    def test_seat_removal_before_device_is_protocol_failure(self):
        value = self.make(); self.events([4])
        value.on_fd(7, GLib.IO_IN)
        self.assertEqual(value.error.context['cause'], 'input_protocol')
        self.assertFalse(value.ready())

    def test_hup_without_disconnect_gates_and_cancels(self):
        value = self.make(); value.press([17])
        self.assertFalse(value.on_fd(7, GLib.IO_HUP))
        self.assertFalse(value.ready()); self.assertTrue(value.uncertain)
        self.cancel.assert_called_once_with('connection_io_failure')

    def test_entire_numeric_batch_prevalidated_and_bounded(self):
        value = self.make()
        for codes in ([17, True], [17, 'a'], [17, -1], [17, 17], [], list(range(1, 34)), [768]):
            with self.subTest(codes=codes), self.assertRaises(Failure): value.press(codes)
        self.lib.ei_device_start_emulating.assert_not_called()

    def pointer(self, value, regions=((0, 0, 1280, 720),)):
        value.devices[11] = Device(11, 2, 42, resumed=True, kind='pointer')
        rows = [SimpleNamespace(rect=r) for r in regions]
        self.lib.ei_device_get_region.side_effect = lambda device, index: rows[index] if index < len(rows) else None
        for position, name in enumerate(('x', 'y', 'width', 'height')):
            getattr(self.lib, f'ei_region_get_{name}').side_effect = lambda region, p=position: region.rect[p]
        return value.devices[11]

    def test_seat_binds_pointer_capabilities_only_when_offered(self):
        pointer = ['EI_DEVICE_CAP_POINTER_ABSOLUTE', 'EI_DEVICE_CAP_BUTTON']
        for offered, expected in (({4, 2, 16, 32}, ['EI_DEVICE_CAP_KEYBOARD', *pointer, 'EI_DEVICE_CAP_SCROLL']),
                                  ({4, 2, 32}, ['EI_DEVICE_CAP_KEYBOARD', *pointer]),
                                  ({4, 2}, ['EI_DEVICE_CAP_KEYBOARD']),
                                  ({4, 16}, ['EI_DEVICE_CAP_KEYBOARD'])):
            with self.subTest(offered=offered):
                value = self.make()
                value.seats = set()
                self.lib.ei_seat_has_capability.side_effect = lambda seat, cap: cap in offered
                self.events([3])
                with patch('agent_desktop.input_connection.binding.capabilities') as bind:
                    value.drain()
                self.assertEqual(bind.call_args.args[2], expected)

    def test_device_kinds_keyboard_pointer_and_ignored(self):
        for caps, kind, scroll in (({4}, 'keyboard', False), ({4, 16}, 'keyboard', False),
                                   ({2, 16, 32}, 'pointer', True), ({2, 32}, 'pointer', False),
                                   ({1, 16, 32}, None, None), ({2}, None, None)):
            with self.subTest(caps=caps):
                value = self.make()
                self.lib.ei_device_has_capability.side_effect = lambda device, cap: cap in caps
                self.lib.ei_event_get_device.return_value = 10
                self.events([5]); value.drain()
                self.assertEqual(value.devices[10].kind if 10 in value.devices else None, kind)
                self.assertEqual(value.devices[10].scroll if 10 in value.devices else None, scroll)

    def test_keyboard_and_pointer_are_ready_independently(self):
        value = self.make()
        self.assertFalse(value.ready('pointer'))
        self.pointer(value)
        self.assertTrue(value.ready())
        self.assertTrue(value.ready('pointer'))
        value.devices[11].resumed = False
        self.assertTrue(value.ready())
        with self.assertRaises(Failure): value.move(1, 1)

    def test_move_checks_regions_then_emulates_once_and_click_uses_buttons(self):
        value = self.make(); self.pointer(value)
        with self.assertRaises(Failure) as caught: value.move(1280, 10)
        self.assertEqual(caught.exception.code, 'unsupported_input')
        for bad in ((float('nan'), 1), (True, 1), ('1', 1)):
            with self.subTest(bad=bad), self.assertRaises(Failure): value.move(*bad)
        self.lib.ei_device_start_emulating.assert_not_called()
        value.move(397.0, 123)
        self.lib.ei_device_pointer_motion_absolute.assert_called_once_with(11, 397.0, 123.0)
        value.press([0x110], 'pointer')
        self.assertEqual(self.lib.ei_device_start_emulating.call_count, 1)
        self.lib.ei_device_button_button.assert_called_once_with(11, 0x110, True)
        self.lib.ei_device_keyboard_key.assert_not_called()
        self.assertEqual(value.devices[11].held, [0x110])
        value.release()
        self.lib.ei_device_button_button.assert_called_with(11, 0x110, False)
        self.lib.ei_device_stop_emulating.assert_called_once_with(11)
        self.assertEqual(value.devices[11].held, [])

    def test_scroll_step_is_one_discrete_batch_and_frame_on_a_scroll_capable_pointer(self):
        value = self.make(); device = self.pointer(value)
        for bad in ((0, 0), (2, 0), (0, -2), (True, 0), (0, 1.0), (0, '1'), (None, 1)):
            with self.subTest(bad=bad), self.assertRaises(Failure) as caught: value.scroll(*bad)
            self.assertEqual(caught.exception.code, 'unsupported_input')
        with self.assertRaises(Failure) as caught: value.scroll(0, 1)
        self.assertEqual(caught.exception.code, 'input_unavailable')
        self.assertFalse(caught.exception.context['devices'][1]['scroll'])
        self.lib.ei_device_start_emulating.assert_not_called()
        device.scroll = True
        value.move(5, 5)
        value.scroll(0, 1); value.scroll(-1, -1); value.scroll(1, 0)
        self.assertEqual(self.lib.ei_device_start_emulating.call_count, 1)
        self.assertEqual(self.lib.ei_device_scroll_discrete.call_args_list,
                         [call(11, 0, 120), call(11, -120, -120), call(11, 120, 0)])
        native = [c[0] for c in self.lib.mock_calls if c[0] in ('ei_device_scroll_discrete', 'ei_device_frame')]
        self.assertEqual(native, ['ei_device_frame'] + ['ei_device_scroll_discrete', 'ei_device_frame'] * 3)
        self.assertEqual(device.held, [])
        value.release()
        self.lib.ei_device_stop_emulating.assert_called_once_with(11)
        self.assertFalse(device.emulating)
        self.lib.ei_device_scroll_stop.assert_not_called()
        self.lib.ei_device_scroll_cancel.assert_not_called()

    def test_scroll_never_runs_while_anything_is_held_and_a_native_failure_is_uncertain(self):
        value = self.make(); device = self.pointer(value); device.scroll = True
        value.press([42])
        with self.assertRaises(Failure) as caught: value.scroll(0, 1)
        self.assertEqual(caught.exception.code, 'input_unavailable')
        value.release()
        self.lib.ei_device_scroll_discrete.assert_not_called()
        self.lib.ei_device_scroll_discrete.side_effect = RuntimeError('native')
        with self.assertRaises(RuntimeError): value.scroll(0, -1)
        self.assertTrue(value.uncertain)
        self.assertFalse(value.ready('pointer'))

    def test_pointer_press_accepts_only_buttons_and_never_while_keys_are_held(self):
        value = self.make(); self.pointer(value)
        for codes in ([30], [0x113], [0x110, 0x110]):
            with self.subTest(codes=codes), self.assertRaises(Failure): value.press(codes, 'pointer')
        value.press([42])
        with self.assertRaises(Failure): value.press([0x110], 'pointer')
        with self.assertRaises(Failure): value.move(5, 5)
        self.lib.ei_device_button_button.assert_not_called()

    def test_motion_without_button_still_stops_emulation_on_release(self):
        value = self.make(); self.pointer(value)
        value.move(5, 5)
        self.assertTrue(value.devices[11].emulating)
        value.release()
        self.lib.ei_device_stop_emulating.assert_called_once_with(11)
        self.assertFalse(value.devices[11].emulating)

    def test_failed_stop_after_motion_is_uncertain_and_keeps_evidence(self):
        value = self.make(); self.pointer(value)
        value.move(5, 5)
        self.lib.ei_device_stop_emulating.side_effect = RuntimeError('native')
        value.release()
        self.assertTrue(value.uncertain)
        self.assertTrue(value.devices[11].emulating)
        self.assertFalse(value.ready('pointer'))

    def test_removed_pointer_with_held_button_is_retired_and_blocks_input(self):
        value = self.make(); self.pointer(value)
        value.move(5, 5); value.press([0x110], 'pointer')
        self.lib.ei_event_get_device.return_value = 11
        self.events([6]); value.drain()
        self.assertTrue(value.uncertain)
        self.assertEqual(value.retired_held[0]['keys'], [0x110])
        with self.assertRaises(Failure): value.press([17])

    def layered(self, pointer_first=False):
        """Keyboard 9 and pointer 11, in either announcement order; ctrl+shift down, then the left button."""
        value = self.make()
        if pointer_first:
            keyboard = value.devices.pop(9)
            self.pointer(value)
            value.devices[9] = keyboard
        else:
            self.pointer(value)
        value.press([29, 42])
        value.press([0x110], 'pointer', modifiers=[29, 42])
        return value

    def test_pointer_input_runs_only_under_the_exact_modifiers_and_button_its_caller_holds(self):
        value = self.make(); self.pointer(value); value.devices[11].scroll = True
        value.press([29, 42])
        for refused in (lambda: value.press([0x110], 'pointer'),                      # modifiers not named
                        lambda: value.press([0x110], 'pointer', modifiers=[29]),      # not all of them
                        lambda: value.press([0x110], 'pointer', modifiers=[42, 29]),  # not in press order
                        lambda: value.press([30]),                                    # another keyboard batch
                        lambda: value.move(5, 5),
                        lambda: value.move(5, 5, modifiers=[29, 42], button=0x110),   # no button is down yet
                        lambda: value.scroll(0, 1)):
            with self.assertRaises(Failure) as caught: refused()
            self.assertEqual(caught.exception.code, 'input_unavailable')
        for unsupported in (lambda: value.press([30], modifiers=[29, 42]),            # only pointer presses stack
                            lambda: value.press([0x110], 'pointer', modifiers=[29, 30]),
                            lambda: value.press([0x110], 'pointer', modifiers=[29, 29]),
                            lambda: value.move(5, 5, modifiers=[29, 42], button=0x113),
                            lambda: value.move(5, 5, modifiers=[29, 42], button=True)):
            with self.assertRaises(Failure) as caught: unsupported()
            self.assertEqual(caught.exception.code, 'unsupported_input')
        self.lib.ei_device_button_button.assert_not_called()
        self.lib.ei_device_pointer_motion_absolute.assert_not_called()
        value.scroll(0, 1, modifiers=[29, 42])
        value.press([0x110], 'pointer', modifiers=[29, 42])
        self.lib.ei_device_button_button.assert_called_once_with(11, 0x110, True)
        value.move(7, 8, modifiers=[29, 42], button=0x110)
        self.lib.ei_device_pointer_motion_absolute.assert_called_once_with(11, 7.0, 8.0)
        for refused in (lambda: value.move(9, 9, modifiers=[29, 42]),                 # the button is not named
                        lambda: value.move(9, 9, modifiers=[29, 42], button=0x111),
                        lambda: value.move(9, 9, button=0x110),
                        lambda: value.scroll(0, 1, modifiers=[29, 42]),               # never while a button is down
                        lambda: value.press([0x111], 'pointer', modifiers=[29, 42])):
            with self.assertRaises(Failure) as caught: refused()
            self.assertEqual(caught.exception.code, 'input_unavailable')
        value.retired_held.append({'epoch': 0, 'identity': 7, 'keys': [30]})
        with self.assertRaises(Failure): value.move(9, 9, modifiers=[29, 42], button=0x110)

    def test_buttons_are_released_before_modifiers_whatever_the_device_order(self):
        for pointer_first in (False, True):
            with self.subTest(pointer_first=pointer_first):
                value = self.layered(pointer_first)
                self.lib.reset_mock()
                value.release()
                sent = [(c[0], c.args) for c in self.lib.mock_calls
                        if c[0] in ('ei_device_button_button', 'ei_device_keyboard_key', 'ei_device_stop_emulating')]
                self.assertEqual(sent, [('ei_device_button_button', (11, 0x110, False)), ('ei_device_stop_emulating', (11,)),
                                        ('ei_device_keyboard_key', (9, 42, False)), ('ei_device_keyboard_key', (9, 29, False)),
                                        ('ei_device_stop_emulating', (9,))])
                self.assertEqual([d.held for d in value.devices.values()], [[], []])
                self.assertFalse(value.uncertain)

    def test_release_of_one_kind_keeps_the_other_held(self):
        value = self.layered()
        value.release('pointer')
        self.assertEqual((value.devices[11].held, value.devices[11].emulating), ([], False))
        self.assertEqual((value.devices[9].held, value.devices[9].emulating), ([29, 42], True))
        self.lib.ei_device_keyboard_key.assert_has_calls([call(9, 29, True), call(9, 42, True)])
        self.assertEqual(self.lib.ei_device_keyboard_key.call_count, 2)
        value.press([0x110], 'pointer', modifiers=[29, 42])  # The next click of a multi-click.
        value.release('keyboard')
        self.assertEqual(value.devices[11].held, [0x110])
        self.assertEqual(value.devices[9].held, [])

    def test_a_failed_button_release_still_releases_the_modifiers(self):
        value = self.layered()
        def button(device, code, press):
            if not press:
                raise RuntimeError('native')
        self.lib.ei_device_button_button.side_effect = button
        with self.assertRaises(RuntimeError): value.release()
        self.assertTrue(value.uncertain)
        self.assertEqual(value.devices[11].held, [0x110])
        self.assertEqual(value.devices[9].held, [])
        self.lib.ei_device_keyboard_key.assert_has_calls([call(9, 42, False), call(9, 29, False)])

    def test_device_loss_mid_drag_releases_what_it_can_and_leaves_input_uncertain(self):
        # Pause and removal of the pointer, and loss of the whole connection, while
        # ctrl+shift and the left button are held: KWin can no longer take the
        # button's release, so input is uncertain; the keyboard still releases.
        for kinds, cause in (([7], 'device_paused'), ([6], 'device_removed'), ([2], 'disconnected')):
            with self.subTest(cause=cause):
                value = self.layered()
                value.move(9, 9, modifiers=[29, 42], button=0x110)
                self.lib.ei_event_get_device.return_value = 11
                self.events(kinds); value.drain()
                self.cancel.assert_called_once_with(cause)
                self.assertTrue(value.uncertain)
                # Readiness fails the session; cancelling the drag task then releases.
                value.release()
                if cause == 'disconnected':
                    self.assertEqual(value.devices[9].held, [29, 42])  # Nothing can be sent at all.
                    self.lib.ei_device_keyboard_key.assert_has_calls([call(9, 29, True), call(9, 42, True)])
                    self.assertEqual(self.lib.ei_device_keyboard_key.call_count, 2)
                else:
                    self.assertEqual(value.devices[9].held, [])
                    self.lib.ei_device_keyboard_key.assert_has_calls([call(9, 42, False), call(9, 29, False)])
                if cause == 'device_removed':
                    self.assertEqual(value.retired_held[0]['keys'], [0x110])
                else:
                    self.assertEqual(value.devices[11].held, [0x110])
                self.lib.ei_device_button_button.assert_called_once_with(11, 0x110, True)
                with self.assertRaises(Failure): value.press([30])
                self.cancel.reset_mock(); self.lib.reset_mock()
                self.lib.ei_get_event.return_value = None
                self.lib.ei_event_get_seat.return_value = 42
                self.lib.ei_device_get_seat.return_value = 42

    def test_native_exception_preserves_attempted_press(self):
        value = self.make()
        self.lib.ei_device_keyboard_key.side_effect = RuntimeError('native seam')
        with self.assertRaises(RuntimeError): value.press([17])
        self.assertEqual(value.devices[9].held, [17]); self.assertTrue(value.uncertain)

    def test_emit_exception_cannot_lose_pressed_ledger(self):
        value = self.make(); value.emit = Mock(side_effect=RuntimeError('diagnostic seam'))
        with self.assertRaises(RuntimeError): value.press([17])
        self.assertEqual(value.devices[9].held, [17]); self.assertTrue(value.uncertain)

    def test_reentrant_emission_observer_cannot_poison_replacement(self):
        value = self.make()
        def replace(*args):
            value.dispose()
            value.context = 2
            value.uncertain = False
            raise RuntimeError('old diagnostic failed after replacement')
        value.emit = replace
        with self.assertRaises(RuntimeError): value.press([17])
        self.assertFalse(value.uncertain)
        self.assertEqual(value.context, 2)
        self.assertEqual(value.retired_held[0]['keys'], [17])

    def test_setup_preparation_failure_closes_fd_and_context(self):
        value = self.make(); value.dispose(); value.name = 'test'
        descriptor = os.open('/dev/null', os.O_RDONLY)
        self.lib.ei_new_sender.return_value = 7
        with patch('agent_desktop.input_connection.os.set_blocking', side_effect=OSError('prep')):
            with self.assertRaises(OSError): value._setup(descriptor)
        with self.assertRaises(OSError): os.fstat(descriptor)
        self.assertIsNone(value.context)
        self.lib.ei_unref.assert_called_with(7)

    def test_duplicate_device_event_references_are_balanced(self):
        value = self.make(); self.events([5])
        value.on_fd(7, GLib.IO_IN)
        self.assertEqual(value.error.code, 'input_failed')
        self.lib.ei_device_ref.assert_not_called()
        self.lib.ei_event_unref.assert_called_once()

    def test_late_async_reply_is_rejected_before_fd_duplication(self):
        value = self.make(); value.dispose(); bus = Mock()
        value.connect(bus, time.monotonic()+3)
        callback = bus.call.call_args.args[8]
        fds = Mock(); value.deadline = time.monotonic()-1
        callback(SimpleNamespace(unpack=lambda: (0, 123)), fds, None)
        self.assertEqual(value.error.code, 'timeout'); fds.get.assert_not_called()

    def test_normal_release_frames_all_keys_and_clears_ledger(self):
        value = self.make(); value.press([42, 17]); value.release()
        self.assertEqual(value.devices[9].held, [])
        self.assertEqual(self.lib.ei_device_frame.call_count, 2)
        self.assertFalse(value.uncertain)

    def test_stale_reply_never_duplicates_descriptor(self):
        value = self.make(); value.dispose()
        bus = Mock(); value.connect(bus, time.monotonic()+3)
        callback = bus.call.call_args.args[8]
        value.dispose()
        fds = Mock(); callback(Mock(), fds, None)
        fds.get.assert_not_called()
        self.assertIsNone(value.context)

    def test_dispose_cancels_pending_bus_token_and_new_owner_has_unique_epoch(self):
        first = self.make(); first.dispose(); bus = Mock()
        first.connect(bus, time.monotonic()+3)
        old_token, old_epoch = first.token, first.epoch
        first.dispose()
        bus.cancel.assert_called_with(old_token)
        second = self.make(); second.dispose(); second.connect(bus, time.monotonic()+3)
        self.assertNotEqual(old_epoch, second.epoch)
        self.assertNotEqual(old_token, second.token)

    def test_malformed_reply_fail_closed_without_fd_duplication(self):
        for reply in ((1, 4), (0, True), (0, -1), (0, 'cookie')):
            value = self.make(); value.dispose(); bus = Mock()
            value.connect(bus, time.monotonic()+3)
            callback = bus.call.call_args.args[8]
            fds = Mock(); fds.get_length.return_value = 1
            callback(SimpleNamespace(unpack=lambda: reply), fds, None)
            self.assertIsNotNone(value.error); fds.get.assert_not_called()

    def test_successful_reply_transfers_duplicate_and_keeps_private_bus(self):
        value = self.make(); value.dispose(); bus = Mock()
        value.connect(bus, time.monotonic()+3)
        callback = bus.call.call_args.args[8]
        fds = Mock(); fds.get_length.return_value = 1; fds.get.return_value = 87
        with patch.object(value, '_setup') as setup:
            callback(SimpleNamespace(unpack=lambda: (0, 123)), fds, None)
            setup.assert_called_once_with(87)
        self.assertIs(value.bus, bus); self.assertEqual(value.cookie, 123)

    def test_failed_native_setup_closes_its_own_fd_exactly_once(self):
        host_facilities.require(self, host_facilities.libei())
        # Real epoll EPERM. libei 1.6.0 leaves the FD caller-owned on failure,
        # so setup closes it; a descriptor reusing the number survives disposal.
        with patch('agent_desktop.input_connection.binding.load', wraps=libei_binding.load):
            value = Input('e'*32, GLib, invalidated=lambda cause: None, failed=lambda error: None)
        value.name = 'ownership-test'
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(8):
            with tempfile.TemporaryFile() as regular:
                fd = os.dup(regular.fileno())
                with self.assertRaises(Failure): value._setup(fd)
                with self.assertRaises(OSError): os.fstat(fd)
                reused = os.open('/dev/null', os.O_RDONLY)
                try:
                    value.dispose(); os.fstat(reused)
                finally: os.close(reused)
        self.assertEqual(before, len(os.listdir('/proc/self/fd')))

    def test_failed_setup_never_closes_a_descriptor_libei_already_replaced(self):
        host_facilities.require(self, host_facilities.libei())
        # Simulates a libei that closes the FD on failure and reuses the number
        # internally: setup must notice the different file and leave it open.
        value = Input('e'*32, GLib, invalidated=lambda cause: None, failed=lambda error: None)
        value.name = 'ownership-test'
        replacement = []
        def closing_setup(context, fd):
            os.close(fd)
            replacement.append(os.open('/dev/null', os.O_RDONLY))
            return -1
        with tempfile.TemporaryFile() as regular:
            fd = os.dup(regular.fileno())
            with patch.object(value.lib, 'ei_setup_backend_fd', side_effect=closing_setup):
                with self.assertRaises(Failure): value._setup(fd)
        try:
            self.assertEqual(replacement, [fd])
            os.fstat(fd)
            value.dispose(); os.fstat(fd)
        finally:
            os.close(replacement[0])

    def test_successful_native_setup_transfers_fd_once(self):
        host_facilities.require(self, host_facilities.libei())
        value = Input('e'*32, GLib, invalidated=lambda cause: None, failed=lambda error: None)
        value.name = 'ownership-test'
        left, right = socket.socketpair()
        fd = left.detach()
        try:
            value._setup(fd); os.fstat(fd)
            value.dispose()
            with self.assertRaises(OSError): os.fstat(fd)
        finally:
            right.close(); value.dispose()
