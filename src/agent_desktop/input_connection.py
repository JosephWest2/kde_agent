"""Private EIS connection, owned entirely by the worker's GLib thread.

This module owns connection/device lifetime and small numeric emission batches.
Public request validation, targeting, finite action scheduling and reset policy
belong to the action layer. No method spins a nested GLib loop or waits for I/O.
"""
from __future__ import annotations

import math
import os
import itertools
import threading
import time
from dataclasses import dataclass, field
from . import libei_binding as binding
from .contracts import ContractError

EVENTS = {v: k.removeprefix('EI_EVENT_') for k, v in binding.CONSTANTS.items() if k.startswith('EI_EVENT_')}
BATCH = 256
MAX_KEYS = 32
KEYBOARD, POINTER_ABSOLUTE, BUTTON = 4, 2, 32
BUTTONS = frozenset({0x110, 0x111, 0x112})  # BTN_LEFT, BTN_RIGHT, BTN_MIDDLE
MAX_REGIONS = 16
# KWin's RemoteDesktop EIS request flags (not libei capabilities): 1 keyboard,
# 2 pointer, 4 touch. KWin then offers a keyboard device and a separate
# absolute-pointer device with buttons and one region per output.
EIS_KEYBOARD_AND_POINTER = 3
EPOCHS = itertools.count(1)



def _identity(fd):
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino

class Failure(ContractError):
    def __init__(self, code, message, **kwargs):
        if code in ('input_setup', 'input_protocol'):
            kwargs['context'] = {**kwargs.get('context', {}), 'cause': code}
            code = 'input_failed'
        super().__init__(code, message, **kwargs)


@dataclass
class Device:
    pointer: int
    identity: int
    seat: int
    resumed: bool = False
    emulating: bool = False
    held: list[int] = field(default_factory=list)  # Key codes or button codes.
    kind: str = 'keyboard'  # 'keyboard' or 'pointer' (absolute motion + buttons)


class Input:
    def __init__(self, generation, GLib, *, invalidated, failed, log=lambda *a, **k: None,
                 emit=lambda *a: None, name_prefix='agent-desktop'):
        self.generation, self.GLib = generation, GLib
        self.invalidated, self.failed, self.log, self.emit = invalidated, failed, log, emit
        self.name_prefix = name_prefix
        self.thread = threading.get_ident()
        self.lib = binding.load()  # Reject an unaudited binary before requesting an FD.
        self.context = self.watch = self.idle_watch = None
        self.seats, self.devices = set(), {}
        self.connected = self.disconnected = self.backlog = False
        self.uncertain = self.resetting = False
        self.error = None
        self.epoch = self.sequence = self.serial = 0
        self.cookie = None
        self.pending = False
        self.bus = self.token = None
        self.deadline = None
        self.retired_held = []

    def _owner(self):
        if threading.get_ident() != self.thread:
            raise RuntimeError('libei must only be used by its GLib owner thread')

    def _current(self, epoch, context):
        return self.epoch == epoch and self.context == context and context is not None

    def _resumed(self, kind):
        return [d for d in self.devices.values() if d.resumed and d.kind == kind]

    def usable_device(self, kind='keyboard'):
        return self.connected and not self.error and len(self._resumed(kind)) == 1

    def ready(self, kind='keyboard'):
        """Session readiness is keyboard readiness; the pointer is checked when used."""
        return bool(self.usable_device(kind) and not (self.uncertain or self.resetting or self.backlog or self.pending))

    def snapshot(self):
        return dict(epoch=self.epoch, connected=self.connected, ready=self.ready(),
                    pointer_ready=self.ready('pointer'), backlog=self.backlog,
                    uncertain=self.uncertain, pending=self.pending,
                    devices=[dict(identity=d.identity, kind=d.kind, resumed=d.resumed, held=list(d.held))
                             for d in self.devices.values()], retired_held=list(self.retired_held))

    def device(self, kind='keyboard'):
        self._owner()
        if not self.ready(kind):
            raise Failure('input_unavailable', f'No safely usable resumed {kind} device.', context=self.snapshot())
        return self._resumed(kind)[0]

    def keyboard(self):
        return self.device('keyboard')

    def regions(self, device):
        """The pointer device's regions as (x, y, width, height), in compositor logical coordinates."""
        found = []
        for index in range(MAX_REGIONS):
            region = self.lib.ei_device_get_region(device.pointer, index)
            if not region:
                break
            found.append(tuple(int(getter(region)) for getter in (
                self.lib.ei_region_get_x, self.lib.ei_region_get_y,
                self.lib.ei_region_get_width, self.lib.ei_region_get_height)))
        return found

    def connect(self, bus, deadline):
        """Negotiate on an explicitly supplied, retained private bus; never discover one."""
        self._owner()
        if self.context is not None or self.pending or self.error:
            raise Failure('input_unavailable', 'Dispose the previous connection before negotiation.')
        self.epoch = next(EPOCHS)
        epoch = self.epoch
        self.pending = True
        self.disconnected = False
        self.deadline = min(deadline, time.monotonic() + 3)
        self.name = f'{self.name_prefix}-{self.generation}-epoch-{epoch}'
        self.bus = bus  # Keep the originating D-Bus connection alive.
        def connected(value, fds, error):
            if epoch != self.epoch or not self.pending:
                return  # Gio retains/closes the original FD-list, including stale replies.
            self.pending = False
            try:
                if time.monotonic() >= self.deadline:
                    raise Failure('timeout', 'Private EIS negotiation expired.')
                if error or value is None or fds is None:
                    raise Failure('input_unavailable', 'Private EIS negotiation failed.')
                reply = value.unpack()
                if (not isinstance(reply, tuple) or len(reply) != 2 or type(reply[0]) is not int
                        or reply[0] != 0 or fds.get_length() != 1
                        or type(reply[1]) is not int or not 0 <= reply[1] <= 0x7fffffff):
                    raise Failure('input_unavailable', 'Invalid private EIS descriptor reply.')
                self.cookie = reply[1]
                self._setup(fds.get(reply[0]))  # Duplicate ownership transfers at entry.
            except Exception as exc:
                if epoch == self.epoch:
                    self._fatal(exc)
        try:
            self.token = 'input_resumed-' + str(epoch)
            bus.call(self.token, 'org.kde.KWin', '/org/kde/KWin/EIS/RemoteDesktop',
                     'org.kde.KWin.EIS.RemoteDesktop', 'connectToEIS', self.GLib.Variant('(i)', (EIS_KEYBOARD_AND_POINTER,)),
                     '(hi)', self.deadline, connected, fd=True)
        except Exception as exc:
            self.pending = False
            self._fatal(exc)
            raise

    def tick(self):
        self._owner()
        if self.error:
            raise self.error
        if self.deadline is not None:
            if self.ready():
                self.deadline = None
            elif time.monotonic() >= self.deadline:
                self._fatal(Failure('timeout', 'Resumed input device timed out.'))
                raise self.error

    def _setup(self, fd):
        self._owner()
        owned = True
        setup_epoch = self.epoch
        try:
            self.context = self.lib.ei_new_sender(None)
            if not self.context:
                raise Failure('input_setup', 'ei_new_sender returned NULL.')
            os.set_blocking(fd, False)
            os.set_inheritable(fd, False)
            self.lib.ei_configure_name(self.context, self.name.encode())
            identity = _identity(fd)
            result = self.lib.ei_setup_backend_fd(self.context, fd)
            if result < 0:
                # libei 1.6.0 leaves the descriptor caller-owned on failure, but
                # that is not an API promise. Close it only if the number still
                # refers to the same open file: on this single-threaded owner,
                # nothing else can have reused the number since the call
                # started, so a match means libei did not close it.
                owned = False
                if _identity(fd) == identity:
                    os.close(fd)
                raise Failure('input_setup', f'libei setup errno {-result}.')
            owned = False  # Only libei closes successful setup descriptors.
            epoch, context = self.epoch, self.context
            self.watch = self.GLib.io_add_watch(self.lib.ei_get_fd(context), self.GLib.PRIORITY_DEFAULT,
                self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR,
                lambda fd, condition: self.on_fd(fd, condition, epoch, context))
            self.log('setup', epoch=epoch, result=result, fd_ownership='libei', nonblocking=True)
            if self._current(epoch, context):
                self.drain(epoch, context)
        except Exception:
            if owned:
                os.close(fd)
            if self.epoch == setup_epoch:
                self._dispose_resources()
            raise

    def _remove_sources(self):
        for name in ('watch', 'idle_watch'):
            source = getattr(self, name)
            setattr(self, name, None)
            if source is not None:
                self.GLib.source_remove(source)

    def _fatal(self, error):
        if self.error is not None:
            return
        self.error = error
        self.connected = False
        self.uncertain = True
        for device in self.devices.values():
            device.resumed = False
        self._remove_sources()
        # Both callbacks may release, dispose or replace this connection.
        epoch = self.epoch
        try:
            self.invalidated('dispatch_failure')
        finally:
            if self.epoch == epoch:
                self.failed(error)

    def on_fd(self, fd, condition, epoch=None, context=None):
        self._owner()
        epoch = self.epoch if epoch is None else epoch
        context = self.context if context is None else context
        if not self._current(epoch, context) or self.error:
            return False
        try:
            self.lib.ei_dispatch(context)
            self.drain(epoch, context)
            if self._current(epoch, context) and condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
                self._lost('connection_io_failure', disconnect=True)
        except Exception as exc:
            if self._current(epoch, context):
                self._fatal(exc)
        if not self._current(epoch, context):
            return False
        if self.disconnected or self.error:
            self.watch = None
            return False
        return True

    def _lost(self, cause, device=None, disconnect=False):
        affected = list(self.devices.values()) if device is None else [device]
        for item in affected:
            item.resumed = item.emulating = False
            if item.held:
                self.uncertain = True
        if disconnect:
            self.connected, self.disconnected = False, True
        # State is unusable before reentrant action cancellation/release.
        self.invalidated(cause)

    def drain(self, epoch=None, context=None):
        self._owner()
        epoch = self.epoch if epoch is None else epoch
        context = self.context if context is None else context
        if not self._current(epoch, context) or self.error:
            return
        self.backlog = True  # Gate emission until the known queue has been emptied.
        for _ in range(BATCH):
            event = self.lib.ei_get_event(context)
            if not event:
                self.backlog = False
                return
            try:
                kind = self.lib.ei_event_get_type(event)
                seat = self.lib.ei_event_get_seat(event)
                pointer = self.lib.ei_event_get_device(event)
                if kind == 1:
                    self.connected = True
                elif kind == 3:
                    if not seat or seat in self.seats:
                        raise Failure('input_protocol', 'Invalid or duplicate seat.')
                    self.seats.add(self.lib.ei_seat_ref(seat))
                    if self.lib.ei_seat_has_capability(seat, KEYBOARD):
                        caps = ['EI_DEVICE_CAP_KEYBOARD']
                        if (self.lib.ei_seat_has_capability(seat, POINTER_ABSOLUTE)
                                and self.lib.ei_seat_has_capability(seat, BUTTON)):
                            caps += ['EI_DEVICE_CAP_POINTER_ABSOLUTE', 'EI_DEVICE_CAP_BUTTON']
                        binding.capabilities(self.lib.ei_seat_bind_capabilities, seat, caps)
                elif kind == 5 and (device_kind := self._kind(pointer)) is not None:
                    parent = self.lib.ei_device_get_seat(pointer)
                    if not pointer or pointer in self.devices or parent not in self.seats:
                        raise Failure('input_protocol', 'Invalid or duplicate device.')
                    self.serial += 1
                    self.lib.ei_device_ref(pointer)
                    self.devices[pointer] = Device(pointer, self.serial, parent, kind=device_kind)
                elif kind == 8 and pointer in self.devices:
                    self.devices[pointer].resumed = True
                elif kind == 7 and pointer in self.devices:
                    self._lost('device_paused', self.devices[pointer])
                elif kind == 6 and pointer in self.devices:
                    device = self.devices.pop(pointer)
                    if device.held:
                        self.retired_held.append(dict(epoch=epoch, identity=device.identity, keys=list(device.held)))
                    self.lib.ei_device_unref(pointer)
                    self._lost('device_removed', device)
                elif kind == 4 and seat in self.seats:
                    # Headers promise device-removal precedes seat-removal.
                    if any(d.seat == seat for d in self.devices.values()):
                        raise Failure('input_protocol', 'Seat removed before its devices.')
                    self.seats.remove(seat)
                    self.lib.ei_seat_unref(seat)
                elif kind == 2:
                    self._lost('disconnected', disconnect=True)
                if self._current(epoch, context):
                    self.log('libei', kind=EVENTS.get(kind, str(kind)), epoch=epoch,
                             device=self.devices[pointer].identity if pointer in self.devices else None)
            finally:
                self.lib.ei_event_unref(event)
            if not self._current(epoch, context) or self.error:
                return
        if self.idle_watch is None:
            self.idle_watch = self.GLib.idle_add(lambda: self.drain_once(epoch, context))

    def _kind(self, pointer):
        if not pointer:
            return None
        if self.lib.ei_device_has_capability(pointer, KEYBOARD):
            return 'keyboard'
        if (self.lib.ei_device_has_capability(pointer, POINTER_ABSOLUTE)
                and self.lib.ei_device_has_capability(pointer, BUTTON)):
            return 'pointer'
        return None  # Relative pointers, touch etc. are never bound or referenced.

    def drain_once(self, epoch=None, context=None):
        self._owner()
        epoch = self.epoch if epoch is None else epoch
        context = self.context if context is None else context
        if not self._current(epoch, context) or self.error:
            return False
        self.idle_watch = None
        try:
            self.drain(epoch, context)
        except Exception as exc:
            if self._current(epoch, context):
                self._fatal(exc)
        return False

    def _start(self, device):
        if not device.emulating:
            self.sequence = (self.sequence + 1) & 0xffffffff
            self.lib.ei_device_start_emulating(device.pointer, self.sequence)
            device.emulating = True

    def _send(self, device, code, is_press):
        if device.kind == 'pointer':
            self.lib.ei_device_button_button(device.pointer, code, is_press)
        else:
            self.lib.ei_device_keyboard_key(device.pointer, code, is_press)

    def move(self, x, y):
        """Absolute pointer motion to compositor logical (x, y), inside a device region."""
        self._owner()
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)):
            raise Failure('unsupported_input', 'Pointer coordinates must be finite numbers.')
        device = self.device('pointer')
        if any(d.held for d in self.devices.values()) or self.retired_held:
            raise Failure('input_unavailable', 'Previous input remains unresolved.')
        regions = self.regions(device)
        if not any(rx <= x < rx + width and ry <= y < ry + height for rx, ry, width, height in regions):
            raise Failure('unsupported_input', 'Point is outside every pointer region.',
                          context={'x': x, 'y': y, 'regions': [list(r) for r in regions]})
        epoch, context = self.epoch, self.context
        try:
            self._start(device)
            self.lib.ei_device_pointer_motion_absolute(device.pointer, float(x), float(y))
            self.lib.ei_device_frame(device.pointer, self.lib.ei_now(context))
        except Exception:
            if self._current(epoch, context):
                self.uncertain = True
            raise

    def press(self, codes, kind='keyboard'):
        """One prevalidated finite numeric batch. The action layer must schedule release."""
        self._owner()
        valid = (lambda code: code in BUTTONS) if kind == 'pointer' else (lambda code: 1 <= code <= 0x2ff)
        if (not isinstance(codes, (list, tuple)) or not 1 <= len(codes) <= MAX_KEYS
                or any(type(code) is not int or not valid(code) for code in codes)
                or len(set(codes)) != len(codes)):
            raise Failure('unsupported_input', 'Expected 1–32 distinct supported evdev key or button codes.')
        device = self.device(kind)
        if any(d.held for d in self.devices.values()) or self.retired_held:
            raise Failure('input_unavailable', 'Previous input remains unresolved.')
        epoch, context = self.epoch, self.context
        try:
            self._start(device)
            for code in codes:
                if (not self._current(epoch, context) or not self.ready(kind)
                        or self.devices.get(device.pointer) is not device):
                    raise Failure('input_unavailable', 'Input changed during emission.')
                device.held.append(code)  # Conservative before a possibly failing native call.
                self._send(device, code, True)
            self.lib.ei_device_frame(device.pointer, self.lib.ei_now(context))
            for code in codes:
                if not self._current(epoch, context):
                    raise Failure('input_unavailable', 'Connection replaced during emission observation.')
                self.emit(code, True, device.identity)
        except Exception:
            if self._current(epoch, context):
                self.uncertain = True
            raise

    def release(self):
        self._owner()
        epoch, context = self.epoch, self.context
        for device in list(self.devices.values()):
            if not self._current(epoch, context):
                return
            if not device.held:
                if device.emulating and device.resumed and self.connected and not self.error:
                    # A pointer motion with no button pressed still left emulation open.
                    try:
                        self.lib.ei_device_stop_emulating(device.pointer)
                    except Exception:
                        if self._current(epoch, context):
                            self.uncertain = True
                            self.log('stop_emulating_uncertain', epoch=epoch, identity=device.identity)
                        continue
                    device.emulating = False
                continue
            if not self.connected or self.backlog or not device.resumed or not device.emulating or self.error:
                self.uncertain = True
                self.log('release_uncertain', epoch=epoch, identity=device.identity, keys=list(device.held))
                continue
            codes = list(reversed(device.held))
            try:
                for code in codes:
                    self._send(device, code, False)
                self.lib.ei_device_frame(device.pointer, self.lib.ei_now(context))
                self.lib.ei_device_stop_emulating(device.pointer)
                device.emulating = False
                device.held.clear()
                for code in codes:
                    if not self._current(epoch, context):
                        return
                    self.emit(code, False, device.identity)
            except Exception:
                if self._current(epoch, context):
                    self.uncertain = True
                raise

    def _dispose_resources(self):
        self._remove_sources()
        devices, seats, context = self.devices, self.seats, self.context
        self.devices, self.seats, self.context = {}, set(), None
        self.connected = self.pending = self.backlog = False
        for device in devices.values():
            if device.held:
                self.uncertain = True
                self.retired_held.append(dict(epoch=self.epoch, identity=device.identity, keys=list(device.held)))
            self.lib.ei_device_unref(device.pointer)
        for seat in seats:
            self.lib.ei_seat_unref(seat)
        if context:
            self.lib.ei_unref(context)

    def dispose(self):
        self._owner()
        if self.bus is not None and self.token is not None:
            self.bus.cancel(self.token)
        self.token = None
        self._dispose_resources()
        self.epoch = next(EPOCHS)  # Invalidate pending replies and captured callbacks.
        self.cookie = self.deadline = None
        # Uncertainty and held evidence survive disposal. A reset policy decides
        # when an explicitly replaced connection can safely supersede the owner.
