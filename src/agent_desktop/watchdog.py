"""systemd main-process watchdog; called only by the progressing GLib owner."""
import os
import socket
import time
from .contracts import ContractError


class Watchdog:
    def __init__(self):
        self.address = os.environ.get('NOTIFY_SOCKET')
        self.next = 0
        if not self.address:
            return  # Standalone/internal fixture, not a watchdog service.
        if (os.environ.get('WATCHDOG_USEC') != '5000000'
                or os.environ.get('WATCHDOG_PID') != str(os.getpid())
                or not self.address.startswith(('/', '@'))):
            raise ContractError('session_failed', 'Invalid service watchdog environment.')
        if self.address.startswith('@'):
            self.address = '\0' + self.address[1:]

    def tick(self):
        now = time.monotonic()
        if self.address and now >= self.next:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as sock:
                sock.sendto(b'WATCHDOG=1', self.address)
            self.next = now + 1
