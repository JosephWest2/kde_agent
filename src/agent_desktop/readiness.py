"""Capability gate: a session is ready only after control, window query, resumed input and a screenshot pass.

The same input connection, window adapter and capture adapter then serve public
operations; there is no separate provisional provider. The input-method client
(input_method.py) connects alongside the input connection; whether KWin offers
zwp_input_method_v1 is reported in health, but its absence never fails the session.
"""
from __future__ import annotations
from copy import deepcopy
import os
from pathlib import Path
import time
from importlib.resources import files
from .contracts import ContractError
from .health import fresh
from .private_bus import PrivateBus
from .input_connection import Input
from .input_method import InputMethod
from .owner_time import Budget
from .windows import Adapter

CAPABILITIES = ('window_query', 'input_resumed', 'screenshot')
INPUT_METHOD_SETUP = 1.0  # Connect, registry and bind: a few milliseconds in practice.


class Readiness:
    input_method = None  # The input-method client, once input starts.

    def __init__(self, desktop, generation, binary, deadline):
        from gi.repository import GLib
        self.GLib = GLib
        self.desktop, self.generation, self.binary = desktop, generation, binary
        self.deadline = deadline
        # Health rounds and the KWin name check use Budgets that owner stalls don't use up (owner_time).
        self.owner_clock = getattr(desktop, 'owner_clock', None)
        self.state = 'starting'
        self.phase = 'bus'
        self.health = {key: {'state': 'pending'} for key in CAPABILITIES}
        self.health.update(bus={'state': 'pending'}, compositor={'state': 'pending'},
                           input_method={'state': 'pending'})
        self.error = self.fatal = None
        self.emissions = 0
        self.input = None
        self.input_method = None
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
        # Diagnostic only and never fsynced, but a write can still block under disk
        # load, so it goes through the store's writer like the durable records (#96).
        from .lifecycle import atomic
        from .writer import journal_of
        def failed(error):
            # As when the write ran inline: a failed health record fails the session (next tick).
            if getattr(self, 'record_error', None) is None:
                self.record_error = error
        store = getattr(self.desktop, 'store', None)
        if store is None:
            atomic(self.folder / 'health.json', self.snapshot())
            return
        journal_of(store).submit(atomic, self.folder / 'health.json', self.snapshot(), on_error=failed)

    def snapshot(self):
        return {'state': self.state, 'desktop_ready': self.state == 'ready',
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
        # The query's work budget (windows.Query.work_seconds) starts here, inside adapter.start,
        # and is capped by the startup deadline.
        self.query_deadline = self.deadline
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
        self.input = Input(self.generation, self.GLib, invalidated=self.cancel,
                           failed=self._input_failed, log=self.log)
        self.input.connect(self.bus, self.input_deadline)
        self._input_method_start()

    def _input_method_start(self):
        """A new input-method connection; non-essential, so a failure is only reported in health."""
        path = Path(self.desktop.root) / self.desktop.env.get('WAYLAND_DISPLAY', 'wayland-private')
        client = InputMethod(path, self.GLib)
        self.input_method = client
        deadline = time.monotonic() + INPUT_METHOD_SETUP
        client.start(min(deadline, self.deadline) if self.state == 'starting' else deadline)
        self.health['input_method'] = client.health()
        return client

    def input_method_client(self):
        """The session's input-method client for `type`, reconnecting once a connection was lost.

        None before input starts. A compositor that does not advertise the
        global keeps its 'unavailable' client: that does not change at run time.
        """
        client = self.input_method
        if client is not None and client.state in ('failed', 'closed') and self.state == 'ready':
            client.close()
            client = self._input_method_start()
        return client

    def _input_failed(self, error):
        self.fatal = self.fatal or error

    def _capture_start(self):
        from .capture import Capture
        self.phase = 'screenshot'
        self.capture = Capture(self.desktop, self.generation, self.folder, self.screen, self.deadline)

    def _capture_finish(self):
        try:
            result = self.capture.step()
        except ContractError as error:
            self.fail(ContractError(error.code, error.message + ' Session cleanup required.'), 'screenshot')
        if result is None:
            return
        if time.monotonic() >= self.deadline:
            self.fail(ContractError('timeout', 'Late screenshot acceptance.'), 'screenshot')
        self._passed('screenshot', path=result['path'])
        self.phase = 'health'
        self._health_start()

    def _health_start(self):
        now = time.monotonic()
        self.next_health = now + 1
        self.round = {'deadline': Budget(1, clock=self.owner_clock), 'bus': None, 'compositor': None}
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
            if getattr(self, 'record_error', None) is not None:
                raise self.record_error
            self.desktop.tick()
            if self.fatal:
                self.fail(self.fatal, 'input_resumed')
            self.bus.tick()
            if self.input is not None:
                self.input.tick()
            if self.input_method is not None:
                self.input_method.tick()
                self.health['input_method'] = self.input_method.health()
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
                           self.GLib.Variant('(s)', ('org.kde.KWin',)), '(b)',
                           Budget(1, limit=self.deadline, clock=self.owner_clock), named,
                           destination='org.freedesktop.DBus')
            elif self.phase == 'window_query':
                self._query_finish()
            elif self.phase == 'input_resumed':
                if now >= self.input_deadline:
                    self.fail(ContractError('timeout', 'Resumed input device timed out.'))
                # The input-method client settles (ready, unavailable or failed) within
                # INPUT_METHOD_SETUP; health then says whether `type` can commit text.
                if self.input.ready() and (self.input_method is None or self.input_method.settled):
                    self._passed('input_resumed', resumed=True)
                    self._capture_start()
            elif self.phase == 'screenshot':
                self._capture_finish()
            elif self.phase == 'health':
                if self.input is None or (not self.input.ready() and not self.input.backlog):
                    self.fail(ContractError('session_failed', 'Input device is no longer resumed.'), 'input_resumed')
                if self.round is not None:
                    if self.round['bus'] is False:
                        self.health['compositor'] = {'state': 'unknown'}
                        self.fail(ContractError('session_failed', 'Private bus is unresponsive.'), 'bus')
                    if self.round['bus'] is True and self.round['compositor'] is False:
                        self.fail(ContractError('session_failed', 'Private compositor is unresponsive.'), 'compositor')
                    if self.round['deadline'].expired(now):
                        component = 'compositor' if self.round['bus'] else 'bus'
                        if component == 'bus':
                            self.health['compositor'] = {'state': 'unknown'}
                        self.fail(ContractError('timeout', 'Essential health observation timed out.'), component)
                    if (self.round['bus'] is True and self.round['compositor'] is True
                            and (self.state == 'ready' or self.input.ready())):
                        # Backlog tolerates a transient unavailable input owner
                        # during health observation, but cannot qualify startup.
                        # Retain this completed round and its original deadline
                        # until input has actually drained into a usable state.
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
        if self.input_method is not None:
            self.input_method.close()
        if self.input is not None:
            self.input.dispose()
        self.bus.close()
        self.adapter.close()
        if self.capture is not None:
            self.capture.cancel()
