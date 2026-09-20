"""Real M1-derived capability gate. Production replacement: M7.1 / issue #35."""
from __future__ import annotations
import json
from copy import deepcopy
import math
import stat
import re
import os
from pathlib import Path
import sys
import time
import uuid
from importlib.resources import files
from .contracts import ContractError
from .health import fresh
from .private_bus import PrivateBus
from .provisional_input import Input
from .windows import Adapter

PROVIDER = {'provider': 'm1-provisional', 'release_qualified': False, 'replacement_issue': 35,
            'desktop_operations_supported': False}
CAPABILITIES = ('window_query', 'input_resumed', 'screenshot')


class Readiness:
    def __init__(self, desktop, generation, binary, deadline):
        from gi.repository import GLib
        self.GLib = GLib
        self.desktop, self.generation, self.binary = desktop, generation, binary
        self.deadline = deadline
        self.state = 'starting'
        self.phase = 'bus'
        self.health = {key: {'state': 'pending'} for key in CAPABILITIES}
        self.health.update(bus={'state': 'pending'}, compositor={'state': 'pending'})
        self.error = self.fatal = None
        self.emissions = 0
        self.input = None
        self.folder = desktop.store.path / 'readiness'
        self.folder.mkdir(mode=0o700)
        self.bus = PrivateBus(desktop.env['DBUS_SESSION_BUS_ADDRESS'], min(deadline, time.monotonic() + 3))
        self.adapter = Adapter(desktop, generation, binary)
        self.query = self.capture = None
        self.round = None
        self.name_pending = False
        self.next_name = 0
        self.next_health = 0
        self.observed_at = time.monotonic()
        self._record()

    def log(self, event, **fields):
        # M1 input diagnostics are bounded to essential state, never raw addresses.
        if event == 'setup':
            self.health['input_resumed']['epoch'] = fields.get('epoch')

    def cancel(self, cause):
        if self.state == 'ready':
            self.fatal = ContractError('session_failed', 'Resumed input capability was lost.',
                                       context={'component': 'input_resumed', 'cause': cause})

    def _record(self):
        from .lifecycle import atomic
        atomic(self.folder / 'health.json', self.snapshot())

    def snapshot(self):
        return {**PROVIDER, 'state': self.state, 'desktop_ready': self.state == 'ready',
                'observed_at': self.observed_at, 'health': deepcopy(self.health),
                'failure': None if self.error is None else self.error.payload()}

    def fail(self, error, component=None):
        if self.error is None:
            component = component or getattr(error, 'context', {}).get('component') or self.phase
            self.error = ContractError(getattr(error, 'code', 'session_failed')
                if isinstance(error, ContractError) else 'session_failed',
                getattr(error, 'message', 'Private capability probe failed.'),
                context={'component': component})
            self.state = 'failed'
            self.health.setdefault(component, {})['state'] = 'failed'
            self.health[component]['code'] = self.error.code
            self._record()
        raise self.error

    def _passed(self, component, **details):
        self.health[component] = {'state': 'passed', 'observed_at': time.monotonic(), **details}

    def _call(self, token, path, interface, method, parameters, signature, deadline, callback, *, destination='org.kde.KWin', fd=False):
        self.bus.call(token, destination, path, interface, method, parameters, signature, deadline, callback, fd=fd)

    def _query_start(self):
        self.phase = 'window_query'
        self.query_id = 'readiness-' + self.generation
        self.query_deadline = min(self.deadline, time.monotonic() + .5)
        self.query = self.adapter.start(self.query_id, self.query_deadline)

    def _query_finish(self):
        try:
            result = self.query.step()
        except Exception as error:
            self.query.cancel(error if isinstance(error, ContractError) else 'window_query_failed')
            raise
        if result is None:
            return
        self.screen = self.query.decoder.output.name
        self.window_count = len(result['windows'])
        self._passed('window_query', windows=self.window_count, script_unloaded=True)
        self._input_start()

    def cleanup_query(self, deadline):
        if self.adapter.active is None:
            return True
        self.adapter.active.cancel('cancelled')
        self.desktop.children.poll()
        return self.adapter.active.cleanup(deadline)

    def _input_start(self):
        self.phase = 'input_resumed'
        self.input_deadline = min(self.deadline, time.monotonic() + 3)
        self.input = Input(self)
        def connected(value, fds, error):
            if error or value is None or fds is None:
                self.fail(ContractError('session_failed', 'Private EIS connection failed.'), 'input_resumed')
            reply = value.unpack()
            if (not isinstance(reply, tuple) or len(reply) != 2 or type(reply[0]) is not int
                    or reply[0] < 0 or reply[0] >= fds.get_length() or fds.get_length() != 1):
                self.fail(ContractError('session_failed', 'Invalid EIS FD reply.'), 'input_resumed')
            descriptor = fds.get(reply[0])  # Duplicate; Input.setup owns/cleans this from entry.
            self.input.setup(descriptor)
            self.input.cookie = reply[1]
        self._call('input_resumed', '/org/kde/KWin/EIS/RemoteDesktop', 'org.kde.KWin.EIS.RemoteDesktop',
                   'connectToEIS', self.GLib.Variant('(i)', (1,)), '(hi)', self.input_deadline, connected, fd=True)

    def _capture_start(self):
        from .lifecycle import atomic
        self.phase = 'screenshot'
        self.capture_deadline = min(self.deadline, time.monotonic() + 3)
        self.capture_id = 'capture-' + uuid.uuid4().hex
        self.capture_folder = self.folder / self.capture_id
        self.capture_folder.mkdir(mode=0o700)
        config = self.capture_folder / 'request.json'
        atomic(config, dict(generation=self.generation, request_id=self.capture_id,
            deadline=self.capture_deadline, screen=self.screen, output_dir=str(self.capture_folder),
            runtime_dir=str(self.desktop.root), bus_address=self.desktop.env['DBUS_SESSION_BUS_ADDRESS']))
        with self.desktop.store.open_log('worker') as log:
            self.capture = self.desktop.launch([sys.executable, '-I', '-m', 'agent_desktop.provisional_capture', str(config)],
                                              str(self.desktop.root), {}, stdout=log, stderr=log)

    def _capture_finish(self):
        if time.monotonic() >= self.capture_deadline:
            self.fail(ContractError('timeout', 'Screenshot probe timed out; session cleanup required.'), 'screenshot')
        if self.capture.returncode is None:
            return
        path = self.capture_folder / 'result.json'
        if self.capture.returncode != 0 or not path.exists() or path.stat().st_size > 32768:
            self.fail(ContractError('capture_failed', 'Screenshot probe failed; session cleanup required.'), 'screenshot')
        from .runtime import check_file
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            check_file(fd)
            raw = os.read(fd, 32769)
            if len(raw) > 32768:
                self.fail(ContractError('capture_failed', 'Oversized screenshot receipt.'), 'screenshot')
            result = json.loads(raw)
        finally:
            os.close(fd)
        image_info = (self.capture_folder / 'image.png').lstat()
        completed = result.get('completed_at')
        if (type(result.get('schema')) is not int or result.get('schema') != 1 or result.get('provider') != 'm1-provisional'
                or result.get('session_stop_required') is not False
                or type(completed) not in (int, float) or not math.isfinite(completed)
                or completed >= self.capture_deadline or type(result.get('raw_bytes')) is not int or result.get('raw_bytes') != 3686400
                or not isinstance(result.get('png_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', result['png_sha256'])
                or type(result.get('png_bytes')) is not int or result.get('png_bytes') != image_info.st_size or image_info.st_uid != os.getuid()
                or image_info.st_mode & 0o077 or not stat.S_ISREG(image_info.st_mode)
                or result.get('generation') != self.generation or result.get('request_id') != self.capture_id
                or result.get('ok') is not True or result.get('eof') is not True
                or result.get('dimensions') != [1280, 720] or result.get('deadline') != self.capture_deadline
                or result.get('path') != str(self.capture_folder / 'image.png')
                or result.get('completed_at', self.capture_deadline) >= self.capture_deadline
                or not (self.capture_folder / 'image.png').is_file()):
            self.fail(ContractError('capture_failed', 'Screenshot result could not be accepted.'), 'screenshot')
        if time.monotonic() >= min(self.deadline, self.capture_deadline):
            self.fail(ContractError('timeout', 'Late screenshot acceptance.'), 'screenshot')
        self._passed('screenshot', path=result['path'])
        self.phase = 'health'
        self._health_start()

    def _health_start(self):
        now = time.monotonic()
        self.next_health = now + 1
        self.round = {'deadline': now + 1, 'bus': None, 'compositor': None}
        current = self.round
        def reply(component):
            def accept(value, _fds, error):
                if self.round is current:
                    current[component] = error is None and value is not None
            return accept
        self._call('bus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetId', None, '(s)',
                   current['deadline'], reply('bus'), destination='org.freedesktop.DBus')
        self._call('compositor', '/KWin', 'org.kde.KWin', 'supportInformation', None, '(s)',
                   current['deadline'], reply('compositor'))

    def tick(self):
        if self.error is not None:
            raise self.error
        try:
            self.desktop.tick()
            if self.fatal:
                self.fail(self.fatal, 'input_resumed')
            self.bus.tick()
            now = time.monotonic()
            if self.state == 'ready':
                for component in ('bus', 'compositor'):
                    if not fresh(self.health.get(component, {}).get('observed_at'), now):
                        self.fail(ContractError('timeout', 'Essential health observation expired.'), component)
            if self.state == 'starting' and now >= self.deadline:
                self.fail(ContractError('timeout', 'Shared startup deadline expired.'))
            if self.phase == 'bus' and self.bus.connection is not None and not self.name_pending and now >= self.next_name:
                self.name_pending = True
                def named(value, _fds, error):
                    self.name_pending = False
                    self.next_name = time.monotonic() + .05
                    if error:
                        self.fail(ContractError('session_failed', 'Private bus name observation failed.'), 'bus')
                    if value.unpack() == (True,):
                        self._query_start()
                self._call('kwin_registration', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner',
                           self.GLib.Variant('(s)', ('org.kde.KWin',)), '(b)', min(self.deadline, now + 1), named,
                           destination='org.freedesktop.DBus')
            elif self.phase == 'window_query':
                self._query_finish()
            elif self.phase == 'input_resumed':
                if now >= self.input_deadline:
                    self.fail(ContractError('timeout', 'Resumed input device timed out.'))
                if self.input.ready():
                    self._passed('input_resumed', resumed=True)
                    self._capture_start()
            elif self.phase == 'screenshot':
                self._capture_finish()
            elif self.phase == 'health':
                if self.input is None or not self.input.ready():
                    self.fail(ContractError('session_failed', 'Input device is no longer resumed.'), 'input_resumed')
                if self.round is not None:
                    if self.round['bus'] is False:
                        self.health['compositor'] = {'state': 'unknown'}
                        self.fail(ContractError('session_failed', 'Private bus is unresponsive.'), 'bus')
                    if self.round['bus'] is True and self.round['compositor'] is False:
                        self.fail(ContractError('session_failed', 'Private compositor is unresponsive.'), 'compositor')
                    if now >= self.round['deadline']:
                        component = 'compositor' if self.round['bus'] else 'bus'
                        if component == 'bus':
                            self.health['compositor'] = {'state': 'unknown'}
                        self.fail(ContractError('timeout', 'Essential health observation timed out.'), component)
                    if self.round['bus'] is True and self.round['compositor'] is True:
                        self._passed('bus')
                        self._passed('compositor')
                        self.round = None
                        if self.state != 'ready':
                            self.state = 'ready'
                            self._record()
                elif now >= self.next_health:
                    self._health_start()
            self.observed_at = time.monotonic()
        except Exception as error:
            if getattr(error, 'context', {}).get('component') == 'bus':
                self.health['compositor'] = {'state': 'unknown'}
            self.fail(error)

    def close(self):
        self.bus.close()
        if self.input is not None:
            self.input.dispose()
        self.adapter.close()
        for child in (self.capture,):
            if child is not None and child.returncode is None:
                child.abort()
