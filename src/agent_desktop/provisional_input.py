"""M1-derived single GLib input owner; provisional until #35."""
import os
from . import provisional_binding as binding

class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message

EVENTS = {value: key.removeprefix("EI_EVENT_") for key, value in binding.CONSTANTS.items() if key.startswith("EI_EVENT_")}
KEYS = {"A": 30, "W": 17, "SHIFT": 42}

class Input:
    """Single-GLib-context owner. Dispatch is never interpreted as an error code."""
    def __init__(self, owner):
        self.owner = owner
        self.lib = binding.load()
        self.context = None
        self.watch = None
        self.idle_watch = None
        self.seats = set()
        self.devices = {}
        self.held = {}
        self.connected = False
        self.disconnected = False
        self.uncertain = False
        self.resetting = False
        self.epoch = 0
        self.sequence = 0
        self.cookie = None
        self.gating_checks = []

    def ready(self):
        return self.usable_device() and not self.uncertain and not self.resetting

    def usable_device(self):
        return self.connected and any(d["resumed"] for d in self.devices.values())

    def keyboard(self):
        if not self.ready():
            raise Failure("input_unavailable", "No safely usable resumed keyboard")
        available = [key for key, value in self.devices.items() if value["resumed"]]
        if len(available) != 1:
            raise Failure("input_ambiguous", "Expected one resumed keyboard")
        return available[0]

    def setup(self, fd):
        self.disconnected = False
        self.epoch += 1
        self.name = f"issue12-{self.owner.generation}-epoch-{self.epoch}"
        self.context = self.lib.ei_new_sender(None)
        if not self.context:
            os.close(fd)
            raise Failure("input_setup", "ei_new_sender returned NULL")
        try:
            os.set_blocking(fd, False)
            os.set_inheritable(fd, False)
            self.lib.ei_configure_name(self.context, self.name.encode())
        except Exception:
            os.close(fd)  # Not transferred until ei_setup_backend_fd is called.
            raise
        result = self.lib.ei_setup_backend_fd(self.context, fd)
        if result < 0:
            # Audited/pinned libei 1.6.0 only: failed epoll ADD never installs
            # this source, and close-on-remove never runs. The FD is still ours.
            # Close immediately, before logging, unref or any possible FD reuse.
            os.close(fd)
        # On success ownership transferred; only libei may close that descriptor.
        self.owner.log("setup", epoch=self.epoch, result=result, nonblocking=True,
                       fd_ownership="caller_closed_failed_setup" if result < 0 else "libei")
        if result < 0:
            raise Failure("input_setup", f"libei setup errno {-result}")
        self.watch = self.owner.GLib.io_add_watch(self.lib.ei_get_fd(self.context),
                                                 self.owner.GLib.IO_IN | self.owner.GLib.IO_HUP | self.owner.GLib.IO_ERR,
                                                 self.on_fd)
        self.drain()

    def on_fd(self, fd, condition):
        try:
            self.lib.ei_dispatch(self.context)
            self.drain()
        except Exception as exc:
            self.owner.fatal = exc
            self.owner.cancel("dispatch_failure")
        if self.disconnected or self.owner.fatal:
            self.watch = None
            return False
        return True

    def reject_probe(self, cause):
        before = self.owner.emissions
        try:
            self.keyboard()
        except Failure as exc:
            assert exc.code == "input_unavailable"
            self.gating_checks.append({"cause": cause, "epoch": self.epoch, "emissions_unchanged": before == self.owner.emissions})
        else:
            raise Failure("gating_failed", f"Input allowed at {cause}")

    def drain(self):
        for _ in range(256):
            event = self.lib.ei_get_event(self.context)
            if not event:
                return
            try:
                kind = self.lib.ei_event_get_type(event)
                seat = self.lib.ei_event_get_seat(event)
                device = self.lib.ei_event_get_device(event)
                self.owner.log("libei", kind=EVENTS.get(kind, str(kind)), epoch=self.epoch, device=device)
                if kind == 1:
                    self.connected = True
                elif kind == 3:
                    if seat in self.seats:
                        raise Failure("input_protocol", "Duplicate seat")
                    self.seats.add(self.lib.ei_seat_ref(seat))
                    if self.lib.ei_seat_has_capability(seat, 4):
                        binding.capabilities(self.lib.ei_seat_bind_capabilities, seat)
                elif kind == 5:
                    if self.lib.ei_device_has_capability(device, 4):
                        self.lib.ei_device_ref(device)
                        self.devices[device] = {"resumed": False, "emulating": False}
                        self.reject_probe("ADDED")
                elif kind == 8 and device in self.devices:
                    self.devices[device]["resumed"] = True
                elif kind == 7 and device in self.devices:
                    self.devices[device]["resumed"] = False
                    self.devices[device]["emulating"] = False
                    if self.held.get(device):
                        self.uncertain = True
                    self.owner.cancel("device_paused")
                    self.reject_probe("PAUSED")
                elif kind == 6 and device in self.devices:
                    self.devices[device]["resumed"] = False
                    if self.held.get(device):
                        self.uncertain = True
                    self.owner.cancel("device_removed")
                    del self.devices[device]
                    self.lib.ei_device_unref(device)
                elif kind == 4 and seat in self.seats:
                    self.seats.remove(seat)
                    self.lib.ei_seat_unref(seat)
                elif kind == 2:
                    self.connected = False
                    self.disconnected = True
                    if any(self.held.values()):
                        self.uncertain = True
                    self.owner.cancel("disconnected")
            finally:
                self.lib.ei_event_unref(event)
        # Bound work so cancellation and timers remain serviceable on a busy fd.
        if self.idle_watch is None:
            self.idle_watch = self.owner.GLib.idle_add(self.drain_once)

    def drain_once(self):
        self.idle_watch = None
        if self.context:
            self.drain()
        return False

    def press(self, names):
        if not names or len(names) != len(set(names)) or any(name not in KEYS for name in names):
            raise Failure("unsupported_input", "Complete physical-key request must contain distinct supported keys")
        device = self.keyboard()
        if any(self.held.values()):
            raise Failure("input_busy", "Previous held input remains unresolved")
        self.sequence += 1
        self.lib.ei_device_start_emulating(device, self.sequence)
        self.devices[device]["emulating"] = True
        self.held[device] = []
        for name in names:
            code = KEYS[name]
            self.lib.ei_device_keyboard_key(device, code, True)
            self.held[device].append(code)
            self.owner.emit(code, True, device)
        self.lib.ei_device_frame(device, self.lib.ei_now(self.context))

    def release(self):
        for device, codes in self.held.items():
            state = self.devices.get(device)
            if codes and (not self.connected or not state or not state["resumed"] or not state["emulating"]):
                self.uncertain = True
                self.owner.log("release_uncertain", epoch=self.epoch, keys=list(codes))
                continue
            for code in reversed(codes):
                self.lib.ei_device_keyboard_key(device, code, False)
                self.owner.emit(code, False, device)
            if codes:
                self.lib.ei_device_frame(device, self.lib.ei_now(self.context))
                codes.clear()
            if state and state["emulating"]:
                self.lib.ei_device_stop_emulating(device)
                state["emulating"] = False

    def dispose(self):
        if self.idle_watch is not None:
            self.owner.GLib.source_remove(self.idle_watch)
            self.idle_watch = None
        if self.watch is not None:
            self.owner.GLib.source_remove(self.watch)
            self.watch = None
        for device in self.devices:
            self.lib.ei_device_unref(device)
        self.devices.clear()
        for seat in self.seats:
            self.lib.ei_seat_unref(seat)
        self.seats.clear()
        if self.context:
            self.lib.ei_unref(self.context)
            self.context = None
        self.connected = False
        self.cookie = None
