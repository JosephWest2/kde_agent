"""Deadline-owned asynchronous private D-Bus connection and calls."""
from __future__ import annotations
import os
import time
from .contracts import ContractError


class PrivateBus:
    def __init__(self, address, deadline, *, now=time.monotonic):
        from gi.repository import Gio
        self.Gio, self.now = Gio, now
        self.connection = None
        self.closed = False
        self.pending = {}
        self.error = None
        self._begin('connect', deadline)
        Gio.DBusConnection.new_for_address(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, self.pending['connect'][1], self._connected, self.pending['connect'])

    def _begin(self, token, deadline):
        if self.closed or token in self.pending or self.now() >= deadline:
            raise ContractError('timeout', 'Private bus operation deadline expired.', context={'component': token})
        op = (deadline, self.Gio.Cancellable())
        self.pending[token] = op
        return op

    def _valid(self, token, op):
        valid = not self.closed and self.pending.get(token) is op and self.now() < op[0]
        if self.pending.get(token) is op:
            del self.pending[token]
            if not self.closed and self.now() >= op[0]:
                self.error = ContractError('timeout', 'Late private bus operation reply.', context={'component': token})
        return valid

    def _connected(self, _source, result, op):
        connection = None
        try:
            connection = self.Gio.DBusConnection.new_for_address_finish(result)
            if self._valid('connect', op):
                self.connection = connection
                connection.set_exit_on_close(False)
                return
        except Exception:
            if self._valid('connect', op):
                self.error = ContractError('session_failed', 'Private bus authentication failed.', context={'component': 'bus'})
        if connection is not None:
            connection.close(None, None, None)

    def call(self, token, destination, path, interface, method, parameters, signature, deadline, callback, *, fd=False):
        from gi.repository import GLib
        if self.connection is None:
            raise ContractError('session_unavailable', 'Private bus is not connected.')
        op = self._begin(token, deadline)
        def finished(connection, result, _data):
            value = fds = None
            error = None
            try:
                if fd:
                    value, fds = connection.call_with_unix_fd_list_finish(result)
                else:
                    value = connection.call_finish(result)
            except Exception as exc:
                error = exc
            if self._valid(token, op):
                try:
                    callback(value, fds, error)
                except Exception as exc:
                    self.error = exc
            # Gio owns originals; dropping the FD-list closes them even on stale replies.
            fds = None
        args = (destination, path, interface, method, parameters,
                GLib.VariantType.new(signature), self.Gio.DBusCallFlags.NONE,
                max(1, int((deadline - self.now()) * 1000)))
        if fd:
            self.connection.call_with_unix_fd_list(*args, None, op[1], finished, None)
        else:
            self.connection.call(*args, op[1], finished, None)

    def cancel(self, token):
        """Revoke an owner's pending call without poisoning unrelated bus work."""
        op = self.pending.pop(token, None)
        if op is not None:
            op[1].cancel()

    def tick(self):
        if self.error:
            raise self.error
        now = self.now()
        for token, op in tuple(self.pending.items()):
            if now >= op[0]:
                del self.pending[token]
                op[1].cancel()
                self.error = ContractError('timeout', 'Private bus operation timed out.', context={'component': token})
                raise self.error
        if self.connection is not None and self.connection.is_closed():
            raise ContractError('session_failed', 'Private bus disconnected.', context={'component': 'bus'})

    def close(self):
        self.closed = True
        for _, cancellable in self.pending.values():
            cancellable.cancel()
        self.pending.clear()
        if self.connection is not None:
            self.connection.close(None, None, None)
            self.connection = None
