"""Owner-driven systemd watchdog notifications and nonblocking failure."""
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_desktop.contracts import ContractError
from agent_desktop.watchdog import Watchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-watchdog-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'notify')
        self.receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
        self.receiver.bind(self.path)
        self.addCleanup(self.receiver.close)
        self.env = {'NOTIFY_SOCKET': self.path, 'WATCHDOG_USEC': '5000000', 'WATCHDOG_PID': str(os.getpid())}

    def watchdog(self, env=None):
        with patch.dict(os.environ, self.env if env is None else env, clear=True):
            return Watchdog()

    def assert_quiet(self):
        with self.assertRaises(BlockingIOError):
            self.receiver.recv(4096)

    def test_first_healthy_owner_tick_sends_without_waiting_for_ready(self):
        watchdog = self.watchdog()
        self.assert_quiet()
        with patch('agent_desktop.watchdog.time.monotonic', return_value=100.):
            watchdog.tick()
        self.assertEqual(self.receiver.recv(4096), b'WATCHDOG=1')
        self.assertEqual(watchdog.next, 101.)
        self.assert_quiet()

    def test_heartbeat_cadence_has_no_catchup_burst_or_background_sender(self):
        watchdog = self.watchdog()
        with patch('agent_desktop.watchdog.time.monotonic', return_value=10.) as now:
            watchdog.tick()
            self.assertEqual(self.receiver.recv(4096), b'WATCHDOG=1')
            now.return_value = 10.99
            watchdog.tick()
            self.assert_quiet()
            now.return_value = 11.
            watchdog.tick()
            self.assertEqual(self.receiver.recv(4096), b'WATCHDOG=1')
            # Advancing time cannot notify systemd without another owner tick.
            now.return_value = 40.
            self.assert_quiet()
            watchdog.tick()
            self.assertEqual(self.receiver.recv(4096), b'WATCHDOG=1')
            watchdog.tick()
            self.assert_quiet()
            self.assertEqual(watchdog.next, 41.)

    def test_standalone_fixture_has_no_notification_side_effect(self):
        watchdog = self.watchdog({})
        with patch('agent_desktop.watchdog.socket.socket') as create:
            watchdog.tick()
        create.assert_not_called()

    def test_wrong_pid_timeout_and_non_socket_address_rejected(self):
        invalid = [self.env | {'WATCHDOG_PID': str(os.getpid() + 1)},
                   self.env | {'WATCHDOG_PID': ''},
                   self.env | {'WATCHDOG_USEC': '5000001'},
                   self.env | {'WATCHDOG_USEC': ''},
                   self.env | {'NOTIFY_SOCKET': 'relative/path'}]
        for env in invalid:
            with self.subTest(env=env), self.assertRaises(ContractError) as caught:
                self.watchdog(env)
            self.assertEqual(caught.exception.code, 'session_failed')
        self.assert_quiet()

    def test_systemd_abstract_notification_address(self):
        name = 'agent-watchdog-test-' + str(os.getpid()) + '-' + Path(self.temp.name).name
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as receiver:
            receiver.bind('\0' + name)
            watchdog = self.watchdog(self.env | {'NOTIFY_SOCKET': '@' + name})
            watchdog.tick()
            self.assertEqual(receiver.recv(4096), b'WATCHDOG=1')
        self.assert_quiet()

    def test_unavailable_endpoint_fails_instead_of_refreshing_heartbeat(self):
        watchdog = self.watchdog(self.env | {'NOTIFY_SOCKET': self.path + '-absent'})
        started = time.monotonic()
        with self.assertRaises(OSError):
            watchdog.tick()
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual(watchdog.next, 0)

    def test_backpressured_notification_socket_cannot_block_owner(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as filler:
            for _ in range(10000):
                try:
                    filler.sendto(b'occupied', self.path)
                except BlockingIOError:
                    break
            else:
                self.fail('fixture could not fill its private datagram queue')
            watchdog = self.watchdog()
            started = time.monotonic()
            with patch('agent_desktop.watchdog.socket.socket', return_value=filler) as create:
                with self.assertRaises(BlockingIOError):
                    watchdog.tick()
            create.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
            self.assertLess(time.monotonic() - started, .2)
            self.assertEqual(watchdog.next, 0)


if __name__ == '__main__':
    unittest.main()
