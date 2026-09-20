"""Owned private desktop foundation; construction is not production readiness."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import time
from xml.sax.saxutils import escape

from .contracts import ContractError
from .environment import DEFAULTS, check_overrides, compose, executable
from .runtime import check_directory

CONSTRUCTION_SECONDS = 30.0


def dispose(generation_root):
    """Caller must hold the generation name lock and prove cgroup quiescence."""
    check_directory(generation_root)
    root = generation_root / 'desktop'
    try:
        check_directory(root)
    except FileNotFoundError:
        return
    # Python's fd-based rmtree does not follow directory symlinks. Retain the
    # generation claim and lifecycle metadata outside this disposable subtree.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ContractError('session_unavailable', 'Safe settings disposal is unavailable.')
    shutil.rmtree(root)


class Desktop:
    def __init__(self, generation_root, children, store, *, now=None):
        self.root = Path(generation_root) / 'desktop'
        self.children, self.store = children, store
        self.phase = 'new'
        self.bus = self.probe = self.compositor = None
        self.deadline = (time.monotonic() if now is None else now) + CONSTRUCTION_SECONDS
        check_directory(self.root.parent)
        for parent in self.root.parents:
            if parent.is_symlink():
                raise ContractError('session_unavailable', 'Private settings path is unsafe.')
        self.private = {
            'HOME': str(self.root / 'home'), 'XDG_RUNTIME_DIR': str(self.root),
            'XDG_CONFIG_HOME': str(self.root / 'home/config'),
            'XDG_DATA_HOME': str(self.root / 'home/data'),
            'XDG_CACHE_HOME': str(self.root / 'home/cache'),
            'XDG_STATE_HOME': str(self.root / 'home/state'),
            'XDG_CONFIG_DIRS': str(self.root / 'empty'), 'TMPDIR': str(self.root / 'tmp'),
            'WAYLAND_DISPLAY': 'wayland-private',
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(self.root / 'bus'),
            'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=' + str(self.root / 'no-system-bus'),
        }
        self.env = compose(DEFAULTS, {}, self.private)
        self.compositor_env = self.env | {'KWIN_SCREENSHOT_NO_PERMISSION_CHECKS': '1'}
        for name in ('bus', 'wayland-private', 'no-system-bus'):
            if len(os.fsencode(self.root / name)) >= 108:
                raise ContractError('session_unavailable', 'Private endpoint path is too long.')
        self.root.mkdir(mode=0o700)  # Exclusive; never reuse an old desktop.
        for name in ('home', 'home/config', 'home/data', 'home/cache', 'home/state', 'empty', 'tmp'):
            (self.root / name).mkdir(mode=0o700)
        config = ('<busconfig><type>session</type><listen>' +
                  escape(self.env['DBUS_SESSION_BUS_ADDRESS']) +
                  '</listen><auth>EXTERNAL</auth><policy context="default">'
                  '<allow send_destination="*"/><allow receive_sender="*"/>'
                  '<allow own="*"/></policy></busconfig>')
        fd = os.open(self.root / 'bus.conf', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(config)
        self.bus = self._start('bus', ['/usr/bin/dbus-daemon', '--nofork',
                                      '--config-file=' + str(self.root / 'bus.conf')])
        self.phase = 'bus'

    def _start(self, source, argv, *, env=None):
        with self.store.open_log(source) as stream:
            return self.children.start(argv, env=self.env if env is None else env,
                                       cwd=str(self.root), stdout=stream, stderr=stream)

    def _socket(self, name):
        try:
            info = (self.root / name).lstat()
        except FileNotFoundError:
            return False
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise ContractError('session_failed', 'Private desktop endpoint is unsafe.')
        os.chmod(self.root / name, 0o600, follow_symlinks=False)
        return True

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        self.children.poll(now)
        for child in (self.bus, self.compositor):
            if child is not None and child.returncode is not None:
                raise ContractError('session_failed', 'An essential private desktop child exited.')
        if self.phase != 'constructed' and now >= self.deadline:
            raise ContractError('timeout', 'Private desktop construction timed out.')
        if self.phase == 'bus' and self._socket('bus'):
            self.probe = self._start('bus', ['/usr/bin/dbus-send',
                '--address=' + self.env['DBUS_SESSION_BUS_ADDRESS'], '--type=method_call',
                '--print-reply', '--reply-timeout=1000', '--dest=org.freedesktop.DBus',
                '/org/freedesktop/DBus', 'org.freedesktop.DBus.Hello'])
            self.phase = 'bus_probe'
        elif self.phase == 'bus_probe' and self.probe.returncode is not None:
            if self.probe.returncode != 0:
                raise ContractError('session_failed', 'Private bus observation failed.')
            self.compositor = self._start('compositor', ['/usr/bin/kwin_wayland', '--virtual',
                '--width', '1280', '--height', '720', '--scale', '1', '--output-count', '1',
                '--socket', self.env['WAYLAND_DISPLAY'], '--no-lockscreen',
                '--no-global-shortcuts', '--no-kactivities'],
                env=self.compositor_env)
            self.phase = 'compositor'
        elif self.phase == 'compositor' and self._socket(self.env['WAYLAND_DISPLAY']):
            # Check the real clock after observation too; a delayed filesystem
            # observation may not turn an expired construction into success.
            if time.monotonic() >= self.deadline:
                raise ContractError('timeout', 'Private desktop construction timed out.')
            self.phase = 'constructed'

    def launch(self, argv, cwd, overrides, *, stdout, stderr):
        """Internal application/adapter seam, not a public launch implementation."""
        check_overrides(overrides)
        if self.phase != 'constructed':
            raise ContractError('session_unavailable', 'Private desktop is not constructed.')
        self.tick()  # Check current essential child lifetimes before spawning.
        env = compose(DEFAULTS, overrides, self.private)
        selected = executable(argv[0], cwd, env)
        return self.children.start(argv, executable=selected['executable'], cwd=cwd,
                                   env=env, stdout=stdout, stderr=stderr)
