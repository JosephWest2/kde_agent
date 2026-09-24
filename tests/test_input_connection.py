"""Production libei lifecycle, real ABI/FD ownership, and reentrant fault coverage."""
import ctypes
import os
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gi.repository import GLib
from agent_desktop.input_connection import Input, Device, Failure
from agent_desktop import libei_binding


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

    def test_failed_native_setup_closes_owned_fd_before_reuse(self):
        # Real epoll EPERM, then reuse the number before disposal: no double close.
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

    def test_successful_native_setup_transfers_fd_once(self):
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
