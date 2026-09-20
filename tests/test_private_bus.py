"""Real private Gio bootstrap deadlines and stale native descriptor ownership."""
import gc
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import weakref
from unittest.mock import Mock

from gi.repository import Gio, GLib

from agent_desktop.contracts import ContractError
from agent_desktop.private_bus import PrivateBus


class PrivateBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-bus-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bus'
        self.address = 'unix:path=' + str(self.path)
        self.context = GLib.MainContext.default()

    def pump(self):
        # A broken fixture must not create an unbounded owner callback loop.
        for _ in range(30):
            if not self.context.pending():
                break
            self.context.iteration(False)

    def until(self, condition, seconds=2, step=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if step:
                step()
            self.pump()
            if condition():
                return
            time.sleep(.002)
        self.fail('private fixture did not reach its bounded condition')

    def real_bus(self):
        process = subprocess.Popen(['/usr/bin/dbus-daemon', '--session', '--nofork', '--nopidfile',
                                    '--address=' + self.address], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        self.addCleanup(cleanup)
        self.until(self.path.exists)
        bus = PrivateBus(self.address, time.monotonic() + 2)
        self.addCleanup(bus.close)
        self.until(lambda: bus.connection is not None, step=bus.tick)
        return bus

    def test_async_private_connect_and_get_id(self):
        bus = self.real_bus()
        result = []
        bus.call('id', 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                 'GetId', None, '(s)', time.monotonic() + 1,
                 lambda value, fds, error: result.append((value, fds, error)))
        self.assertIn('id', bus.pending)
        self.until(lambda: bool(result), step=bus.tick)
        value, fds, error = result[0]
        self.assertIsNone(error)
        self.assertIsNone(fds)
        self.assertRegex(value.unpack()[0], r'^[0-9a-f]{32}$')
        self.assertNotIn('id', bus.pending)

    def stalled_server(self, *, authenticate):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.path))
        listener.listen(1)
        listener.setblocking(False)
        self.addCleanup(listener.close)
        clients, commands, raw = [], [], bytearray()
        def step():
            if not clients:
                try:
                    client, _ = listener.accept()
                except BlockingIOError:
                    return
                client.setblocking(False)
                clients.append(client)
                self.addCleanup(client.close)
            try:
                data = clients[0].recv(16384)
            except BlockingIOError:
                return
            raw.extend(data)
            if not authenticate or b'BEGIN' in b''.join(commands):
                return
            while b'\r\n' in raw:
                command, _, rest = raw.partition(b'\r\n')
                commands.append(bytes(command))
                raw[:] = rest
                if command.lstrip(b'\0') == b'AUTH':
                    clients[0].sendall(b'REJECTED EXTERNAL\r\n')
                elif b'AUTH' in command:
                    clients[0].sendall(b'OK 0123456789abcdef0123456789abcdef\r\n')
                elif b'NEGOTIATE_UNIX_FD' in command:
                    clients[0].sendall(b'AGREE_UNIX_FD\r\n')
                elif b'BEGIN' in command:
                    break
        return step, clients, commands, raw

    def assert_stalled_deadline(self, *, authenticate):
        step, clients, commands, raw = self.stalled_server(authenticate=authenticate)
        start = time.monotonic()
        bus = PrivateBus(self.address, start + .25)
        self.addCleanup(bus.close)
        error = None
        while time.monotonic() - start < 1:
            step()
            self.pump()
            try:
                bus.tick()
            except ContractError as caught:
                error = caught
                break
            time.sleep(.002)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 'timeout', (commands, bytes(raw)))
        self.assertEqual(error.context['component'], 'connect')
        self.assertLess(time.monotonic() - start, .6)
        self.assertTrue(clients, 'fixture actually accepted the explicit private connection')
        self.assertIsNone(bus.connection)
        if authenticate:
            self.assertTrue(any(b'BEGIN' in command for command in commands))
            self.assertIn(b'Hello', raw, 'Gio reached the stalled message-bus Hello handshake')
        else:
            self.assertIn(b'AUTH', raw)
        # Cancellation completion must not erase timeout or revive readiness.
        for _ in range(10):
            step()
            self.pump()
            time.sleep(.002)
        with self.assertRaises(ContractError) as sticky:
            bus.tick()
        self.assertIs(sticky.exception, error)
        self.assertIsNone(bus.connection)

    def test_accepting_socket_with_stalled_authentication_is_finite(self):
        self.assert_stalled_deadline(authenticate=False)

    def test_authenticated_socket_with_stalled_hello_is_finite(self):
        self.assert_stalled_deadline(authenticate=True)

    def mock_bus(self):
        # Skip actual bootstrap only for callback ownership races.
        bus = PrivateBus.__new__(PrivateBus)
        bus.Gio = Gio
        bus.now = Mock(return_value=10.)
        bus.connection = Mock()
        bus.connection.is_closed.return_value = False
        bus.closed = False
        bus.pending = {}
        bus.error = None
        return bus

    def test_late_completion_before_owner_tick_becomes_sticky_timeout(self):
        bus = self.mock_bus()
        accepted = Mock()
        bus.call('health', 'x.y', '/x', 'x.y', 'Ping', None, '(s)', 11., accepted)
        finished = bus.connection.call.call_args.args[-2]
        bus.connection.call_finish.return_value = GLib.Variant('(s)', ('late',))
        bus.now.return_value = 11.01
        finished(bus.connection, object(), None)
        accepted.assert_not_called()
        self.assertNotIn('health', bus.pending)
        for _ in range(2):
            with self.assertRaises(ContractError) as caught:
                bus.tick()
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertEqual(caught.exception.context['component'], 'health')

    def fd_result(self, connection):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        fds = Gio.UnixFDList.new()
        fds.append(write_fd)
        os.close(write_fd)
        reference = weakref.ref(fds)
        # The finish result owns the sole list reference until the callback runs.
        pending = [(GLib.Variant('(h)', (0,)), fds)]
        del fds
        connection.call_with_unix_fd_list_finish.side_effect = lambda _: pending.pop()
        return read_fd, reference

    def assert_stale_fd_reaped(self, *, cancel):
        bus = self.mock_bus()
        accepted = Mock()
        bus.call('eis', 'x.y', '/x', 'x.y', 'Connect', None, '(h)', 11., accepted, fd=True)
        connection = bus.connection
        finished = connection.call_with_unix_fd_list.call_args.args[-2]
        read_fd, reference = self.fd_result(connection)
        if cancel:
            bus.close()
        else:
            bus.now.return_value = 12.
        finished(connection, object(), None)
        gc.collect()
        connection.call_with_unix_fd_list_finish.assert_called_once()
        accepted.assert_not_called()
        self.assertIsNone(reference(), 'stale native FD-list wrapper was released')
        os.set_blocking(read_fd, False)
        self.assertEqual(os.read(read_fd, 1), b'', 'original returned FD closed: pipe reached EOF')

    def test_late_eis_reply_releases_native_fd_list_without_installing(self):
        self.assert_stale_fd_reaped(cancel=False)

    def test_cancelled_eis_reply_releases_native_fd_list_without_installing(self):
        self.assert_stale_fd_reaped(cancel=True)

    def test_rejected_native_reply_signature_does_not_return_fd(self):
        bus = self.real_bus()
        result = []
        bus.call('invalid-eis', 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                 'GetId', None, '(h)', time.monotonic() + 1,
                 lambda value, fds, error: result.append((value, fds, error)), fd=True)
        self.until(lambda: bool(result), step=bus.tick)
        value, fds, error = result[0]
        self.assertIsNone(value)
        self.assertIsNone(fds)
        self.assertIsNotNone(error)

    def test_late_connect_completion_is_closed_and_never_installed(self):
        bus = self.mock_bus()
        bus.connection = None
        bus.Gio = Mock()
        op = bus._begin('connect', 11.)
        late_connection = Mock()
        bus.Gio.DBusConnection.new_for_address_finish.return_value = late_connection
        bus.now.return_value = 12.
        bus._connected(None, object(), op)
        self.assertIsNone(bus.connection)
        late_connection.close.assert_called_once()
        with self.assertRaises(ContractError) as caught:
            bus.tick()
        self.assertEqual(caught.exception.code, 'timeout')


if __name__ == '__main__':
    unittest.main()
