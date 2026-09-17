#!/usr/bin/python3
"""Bounded real libei compatibility evidence inside the M1 private harness."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "tools" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


binding = module("libei_binding")
windows = module("kdotool_probe")
harness = windows.harness
Failure = harness.Failure
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


class Probe:
    def __init__(self, binary, scenario):
        self.env = dict(os.environ)
        self.generation, self.runtime, self.artifacts, self.manifest, deadline = windows.private_context(self.env)
        self.deadline = deadline - 3
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
        self.dbus, self.GLib = dbus, GLib
        DBusGMainLoop(set_as_default=True)
        self.main_context = GLib.MainContext.default()
        self.bus = dbus.bus.BusConnection(self.env["DBUS_SESSION_BUS_ADDRESS"])
        self.eis = dbus.Interface(self.bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop", introspect=False),
                                  "org.kde.KWin.EIS.RemoteDesktop")
        self.fault = dbus.Interface(self.bus.get_object("org.kde.KWin", "/org/kde/KWin/Issue12EisFault", introspect=False),
                                    "org.kde.KWin.Issue12EisFault")
        self.timeline = []
        self.events = []
        self.received_held = set()
        self.modifiers = None
        self.fixture = (self.artifacts / "fixture-events.jsonl").open("rb")
        self.fixture_buffer = b""
        self.timeline_stream = (self.artifacts / "libei-timeline.jsonl").open("w", buffering=1)
        self.emissions = 0
        self.fatal = None
        self.action = None
        self.actions = []
        self.checks = []
        self.heartbeat_max = 0
        self.heartbeat_last = time.monotonic()
        self.target = None
        self.query_pending = None
        self.query_buffer = b""
        self.next_query = 0
        self.children = []
        self.sources = []
        self.bridge_err = (self.artifacts / "query-bridge.stderr").open("wb")
        self.bridge = subprocess.Popen(["/usr/bin/python", "-I", str(PROJECT / "tools/kdotool_probe.py"), str(binary), "bridge"],
                                       env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.bridge_err)
        self.children.append(self.bridge)
        os.set_blocking(self.bridge.stdout.fileno(), False)
        self.bridge_ready = False
        self.input = Input(self)
        self.result = {"scope": "feasibility", "generation": self.generation, "scenario": scenario,
                       "probe_sha256": harness.digest(__file__), "binding_sha256": harness.digest(binding.__file__),
                       "checks": self.checks, "actions": self.actions,
                       "bounds_seconds": {"connection_lifecycle_reset": 3, "hold": 2, "cancel_dispatch": .1,
                                          "fixture_release": .5, "focus_poll": .1, "query": .5}}
        self.sources.append(GLib.timeout_add(5, self.tick))

    def log(self, event, **fields):
        row = {"event": event, "monotonic_ns": time.monotonic_ns(), "generation": self.generation, **fields}
        self.timeline.append(row)
        self.timeline_stream.write(json.dumps(row) + "\n")
        return row

    def emit(self, code, press, device):
        self.emissions += 1
        row = self.log("emission", code=code, press=press, device=device, epoch=self.input.epoch)
        if self.action:
            self.action.setdefault("emissions", []).append(row)

    def wait(self, predicate, seconds=3, label="condition"):
        end = min(self.deadline, time.monotonic() + seconds)
        while not predicate():
            if self.fatal:
                raise self.fatal
            if time.monotonic() >= end:
                raise Failure("timeout", f"Timed out waiting for {label}")
            self.main_context.iteration(True)
        if self.fatal:
            raise self.fatal
        if time.monotonic() > end:
            raise Failure("timeout", f"Late result for {label}")

    def spin(self, seconds):
        end = time.monotonic() + seconds
        self.wait(lambda: time.monotonic() >= end, seconds + .05, "timer")

    def call(self, method, *args, seconds=3):
        result = {}
        method(*args, reply_handler=lambda *values: result.update(values=values),
               error_handler=lambda error: result.update(error=error), timeout=seconds)
        self.wait(lambda: bool(result), seconds, "D-Bus reply")
        if "error" in result:
            raise Failure("dbus_failed", str(result["error"]))
        return result["values"]

    def request_query(self, operation="query", **kwargs):
        if self.query_pending:
            raise Failure("query_busy", "Only one query is allowed in flight")
        pending = {"operation": operation, "started": time.monotonic()}
        self.query_pending = pending
        self.bridge.stdin.write(json.dumps({"op": operation, **kwargs}).encode() + b"\n")
        self.bridge.stdin.flush()
        return pending

    def query(self, operation="query", **kwargs):
        self.wait(lambda: self.query_pending is None, 2, "previous focus query cleanup")
        pending = self.request_query(operation, **kwargs)
        self.wait(lambda: "result" in pending, 3.6 if operation == "focus" else 2, "focus query")
        value = pending["result"]
        if not value["ok"]:
            raise Failure(value["code"], value.get("message", "Query failed"))
        return value["value"]

    def focused(self, value):
        return self.target in {row["uuid"] for row in value["windows"]} and value["active_uuid"] == self.target

    def tick(self):
        try:
            now = time.monotonic()
            self.heartbeat_max = max(self.heartbeat_max, now - self.heartbeat_last)
            self.heartbeat_last = now
            chunk = self.fixture.read(65536)
            self.fixture_buffer += chunk
            while b"\n" in self.fixture_buffer:
                line, self.fixture_buffer = self.fixture_buffer.split(b"\n", 1)
                row = json.loads(line)
                if row["generation"] != self.generation:
                    raise Failure("receipt_identity", "Wrong fixture generation")
                self.events.append(row)
                if row["event"] == "key":
                    if row["source"] != "wayland":
                        raise Failure("receipt_source", "Not a real Wayland event")
                    if row["state"]:
                        self.received_held.add(row["key"])
                    else:
                        self.received_held.discard(row["key"])
                elif row["event"] == "modifiers":
                    self.modifiers = row["depressed"]
                elif row["event"] == "keyboard_enter" and row["held_count"] == 0:
                    self.received_held.clear()
            try:
                chunk = os.read(self.bridge.stdout.fileno(), 65536)
                self.query_buffer += chunk
            except BlockingIOError:
                pass
            if len(self.query_buffer) > windows.MAX_BYTES:
                raise Failure("query_failed", "Query response too large")
            while b"\n" in self.query_buffer:
                line, self.query_buffer = self.query_buffer.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("ready"):
                    self.bridge_ready = True
                    continue
                pending, self.query_pending = self.query_pending, None
                if not pending:
                    raise Failure("query_failed", "Unsolicited bridge reply")
                pending["result"] = reply
                observed = reply.get("value", {})
                if pending["operation"] == "focus":
                    observed = observed.get("observed", {})
                self.log("focus_observation", ok=reply["ok"], active_uuid=observed.get("active_uuid"),
                         target_present=self.target in {row["uuid"] for row in observed.get("windows", [])},
                         seconds=now - pending["started"], operation=pending["operation"], poll=pending.get("poll", False))
                if pending.get("poll") and self.action and not self.action.get("cancel_reason"):
                    if not reply["ok"] or not self.focused(reply["value"]):
                        self.cancel("focus_lost")
            if self.bridge.poll() is not None:
                raise Failure("query_failed", "Query bridge exited unexpectedly")
            if self.action and not self.action.get("finished_ns"):
                if now >= self.action["deadline"]:
                    self.cancel("action_timeout", int(self.action["deadline"] * 1e9))
                if self.query_pending and now - self.query_pending["started"] >= .5:
                    self.cancel("focus_query_timeout")
                elif not self.query_pending and now >= self.next_query:
                    self.request_query()["poll"] = True
                    self.next_query = now + .1
        except Exception as exc:
            self.fatal = exc
            self.cancel("probe_failure")
        return True

    def connect(self, deadline=None):
        end = min(self.deadline, deadline or time.monotonic() + 3)
        fd, cookie = self.call(self.eis.connectToEIS, self.dbus.Int32(1), seconds=max(.001, end - time.monotonic()))
        self.input.cookie = int(cookie)
        self.input.setup(fd.take())
        self.wait(self.input.usable_device, max(0, end - time.monotonic()), label="resumed keyboard")
        self.log("ready", epoch=self.input.epoch, cookie=self.input.cookie)

    def reset(self):
        started = time.monotonic()
        end = min(self.deadline, started + 3)
        self.cancel("reset")
        self.input.resetting = True
        old_epoch = self.input.epoch
        uncertain = self.input.uncertain
        old_held = {str(d): list(keys) for d, keys in self.input.held.items() if keys}
        if self.input.cookie is not None:
            self.call(self.eis.disconnect, self.dbus.Int32(self.input.cookie), seconds=max(.001, end - time.monotonic()))
        self.input.dispose()
        # Readiness is still withheld until fresh negotiation and neutral receipts.
        self.input.held.clear()
        self.input.uncertain = False
        try:
            self.connect(end)
            self.input.reject_probe("reset_not_yet_neutral")
            self.wait(lambda: not self.received_held and self.modifiers in (None, 0), min(.5, max(0, end - time.monotonic())), "fixture neutral after reset")
        except Exception:
            self.input.uncertain = True
            raise
        finally:
            self.input.resetting = False
        self.checks.append({"kind": "reset", "old_epoch": old_epoch, "new_epoch": self.input.epoch,
                            "prior_uncertain": uncertain, "prior_held": old_held, "fixture_neutral": True,
                            "seconds": time.monotonic() - started})

    def begin(self, names, timeout=2):
        if any(name not in KEYS for name in names):
            raise Failure("unsupported_input", "Unknown physical key")
        value = self.query()
        if not self.focused(value):
            raise Failure("target_not_focused", "Fixture lost focus")
        self.action = {"id": len(self.actions) + 1, "names": names, "started_ns": time.monotonic_ns(),
                       "deadline": time.monotonic() + timeout, "fixture_from": len(self.events)}
        self.actions.append(self.action)
        self.input.press(names)
        self.next_query = time.monotonic() + .1
        codes = {KEYS[name] for name in names}
        self.wait(lambda: codes <= self.received_held, .5, "fixture key press")
        return self.action

    def release(self):
        self.input.release()
        if self.action:
            self.action["finished_ns"] = time.monotonic_ns()
        self.wait(lambda: not self.received_held and self.modifiers in (0, None), .5, "fixture release")
        if self.action:
            self.action["fixture_to"] = len(self.events)
        self.action = None

    def cancel(self, reason, requested_ns=None):
        if self.action and not self.action.get("finished_ns"):
            self.action.update(cancel_reason=reason, cancel_requested_ns=requested_ns or time.monotonic_ns(),
                               cancel_handled_ns=time.monotonic_ns())
            self.input.release()
            self.action["finished_ns"] = time.monotonic_ns()
            self.log("cancel", reason=reason, uncertain=self.input.uncertain)

    def cancellation(self, names, mode="send"):
        action = self.begin(names)
        left, right = socket.socketpair()
        left.setblocking(False)
        receipt = self.artifacts / f"cancel-{action['id']}.json"
        child = subprocess.Popen(["/usr/bin/python", "-I", str(Path(__file__).resolve()), "_cancel",
                                  str(right.fileno()), mode, str(receipt)], pass_fds=(right.fileno(),), env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        right.close()
        completed = False
        def arrived(fd, condition):
            nonlocal completed
            completed = True
            try:
                message = left.recv(128)
                stamp = int(message) if message else json.loads(receipt.read_text())["requested_ns"]
                self.cancel("client_cancel" if message else "client_eof", stamp)
            except Exception as exc:
                self.fatal = exc
            return False
        watch = self.GLib.io_add_watch(left.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP, arrived)
        try:
            self.wait(lambda: bool(action.get("cancel_reason")), .5, "external cancellation")
            self.wait(lambda: not self.received_held and self.modifiers in (0, None), .5, "acknowledged cancellation release")
            releases = [event for event in self.events[action["fixture_from"]:] if event["event"] == "key" and event["state"] == 0]
            requested = action["cancel_requested_ns"]
            action["cancel_dispatch_seconds"] = (action["finished_ns"] - requested) / 1e9
            action["cancel_fixture_release_seconds"] = (max(e["monotonic_ns"] for e in releases) - requested) / 1e9
            assert action["cancel_dispatch_seconds"] <= .1, action
            assert action["cancel_fixture_release_seconds"] <= .5, action
            action["fixture_to"] = len(self.events)
            self.checks.append({"kind": "cancellation", "mode": mode, "action": action["id"], "acknowledged": True})
        finally:
            if not completed:
                self.GLib.source_remove(watch)
            left.close()
            self.action = None

    def initial(self):
        self.wait(lambda: self.bridge_ready, label="query bridge ready")
        value = self.query()
        pid = self.manifest["processes"]["fixture"]["pid"]
        candidates = [row for row in value["windows"] if row["pid"] == pid]
        assert len(candidates) == 1, candidates
        self.target = candidates[0]["uuid"]
        self.result["target"] = candidates[0]
        self.result["initial_focus"] = self.query("focus", uuid=self.target)
        intro = self.dbus.Interface(self.bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop", introspect=False),
                                    "org.freedesktop.DBus.Introspectable")
        (xml,) = self.call(intro.Introspect)
        (self.artifacts / "eis-introspection.xml").write_text(str(xml))
        self.connect()

    def input_scenario(self):
        before = self.emissions
        try:
            self.input.press(["SHIFT", "invalid"])
        except Failure as exc:
            assert exc.code == "unsupported_input"
        else:
            raise AssertionError("Invalid key was accepted")
        assert self.emissions == before
        self.checks.append({"kind": "prevalidation", "no_partial_emission": True})
        for names in (["W"], ["SHIFT", "A"]):
            action = self.begin(names)
            self.spin(.05)
            self.release()
            rows = self.events[action["fixture_from"]:action["fixture_to"]]
            for name in names:
                assert any(e["event"] == "key" and e["key"] == KEYS[name] and e["state"] == 1 for e in rows)
                assert any(e["event"] == "key" and e["key"] == KEYS[name] and e["state"] == 0 for e in rows)
            if "SHIFT" in names:
                assert any(e["event"] == "key" and e["key"] == KEYS["A"] and e["state"] == 1 and e["text"] == "A" for e in rows), rows
            self.wait(lambda: any(e["event"] == "presented" and e.get("source") == "input" for e in self.events[action["fixture_from"]:]), .5, "input presentation")
        self.checks.append({"kind": "key_chord", "press_release_and_presentation": True})

    def cancel_scenario(self):
        self.cancellation(["W"])
        self.cancellation(["SHIFT", "A"])
        self.cancellation(["W"], "eof")
        action = self.begin(["W"], timeout=.15)
        self.wait(lambda: action.get("cancel_reason") == "action_timeout", .5, "action deadline cancellation")
        self.wait(lambda: not self.received_held, .5, "timeout release")
        self.checks.append({"kind": "worker_timeout", "acknowledged": True})
        self.action = None

    def lifecycle_scenario(self, kind):
        if kind == "pause":
            # Reject a wrong generation before any compositor side effect.
            (raw,) = self.call(self.fault.Pause, "0" * 32, self.input.name, self.dbus.UInt32(250))
            assert not json.loads(str(raw))["ok"]
            base = len(self.timeline)
            (raw,) = self.call(self.fault.Pause, self.generation, self.input.name, self.dbus.UInt32(250))
            assert json.loads(str(raw))["ok"]
            self.wait(lambda: any(e.get("kind") == "DEVICE_PAUSED" for e in self.timeline[base:]), label="idle pause")
            self.input.reject_probe("idle_paused")
            self.wait(self.input.ready, label="idle resume")
            assert any(e.get("kind") == "DEVICE_RESUMED" for e in self.timeline[base:])
            self.begin(["W"])
            self.release()
            self.checks.append({"kind": "idle_pause_resume", "acknowledged_after_resume": True, "wrong_generation_rejected": True})
        action = self.begin(["SHIFT", "W"])
        base = len(self.timeline)
        if kind == "removal":
            seat = next(iter(self.input.seats))
            binding.capabilities(self.input.lib.ei_seat_unbind_capabilities, seat)
            self.wait(lambda: not self.input.devices, label="real device removal")
            wanted = "DEVICE_REMOVED"
        elif kind == "disconnect":
            self.call(self.eis.disconnect, self.dbus.Int32(self.input.cookie))
            self.wait(lambda: not self.input.connected, label="server disconnect")
            wanted = "DISCONNECT"
        elif kind == "pause":
            (raw,) = self.call(self.fault.Pause, self.generation, self.input.name, self.dbus.UInt32(250))
            reply = json.loads(str(raw))
            self.result["pause_reply"] = reply
            if not reply.get("ok"):
                raise Failure("pause_failed", str(reply))
            self.wait(lambda: any(e.get("kind") == "DEVICE_PAUSED" for e in self.timeline[base:]), label="real device pause")
            wanted = "DEVICE_PAUSED"
        else:
            raise AssertionError(kind)
        assert any(e.get("kind") == wanted for e in self.timeline[base:])
        assert action.get("cancel_reason"), action
        assert self.input.uncertain, "Held lifecycle loss must retain uncertainty until reset"
        self.input.reject_probe(kind)
        previous = self.emissions
        self.spin(.3 if kind == "pause" else .05)
        assert self.emissions == previous, "Emitted input after lifecycle cancellation"
        if kind == "pause":
            assert any(e.get("kind") == "DEVICE_RESUMED" for e in self.timeline[base:]), "Plugin did not auto-resume"
            assert not self.input.ready(), "Resume incorrectly cleared uncertainty"
        self.checks.append({"kind": kind, "real_event": wanted, "cancelled": True,
                            "uncertain": True, "fixture_held_before_reset": sorted(self.received_held),
                            "no_further_emission": True})
        self.action = None
        self.reset()
        self.begin(["W"])
        self.release()

        if kind == "removal":
            # Also prove same-connection unbind/rebind without held-state ambiguity.
            seat = next(iter(self.input.seats))
            binding.capabilities(self.input.lib.ei_seat_unbind_capabilities, seat)
            self.wait(lambda: not self.input.devices, label="idle keyboard removal")
            binding.capabilities(self.input.lib.ei_seat_bind_capabilities, seat)
            self.wait(self.input.ready, label="rebound resumed keyboard")
            self.begin(["W"])
            self.release()
            self.checks.append({"kind": "rebind", "acknowledged": True})

    def focus_loss_scenario(self):
        action = self.begin(["W"])
        out = (self.artifacts / "secondary.events.jsonl").open("wb")
        err = (self.artifacts / "secondary.stderr").open("wb")
        second = subprocess.Popen([self.manifest["fixture_binary"]], env=self.env,
                                  stdin=subprocess.PIPE, stdout=out, stderr=err)
        self.children.append(second)
        try:
            self.result["secondary_process"] = harness.identity(second.pid)
            self.wait(lambda: bool(action.get("cancel_reason")), .7, "focus loss cancellation")
            assert action["cancel_reason"] == "focus_lost", action
            self.action = None
            # Focus-based release can be received by the new active window.
            self.spin(.05)
            rows = [json.loads(line) for line in (self.artifacts / "secondary.events.jsonl").read_text().splitlines()]
            assert any(e["event"] == "key" and e["key"] == KEYS["W"] and e["state"] == 0 for e in rows), rows
            before = len(self.events)
            self.query("focus", uuid=self.target)
            self.wait(lambda: any(e["event"] == "keyboard_enter" and e["held_count"] == 0 for e in self.events[before:]), .5, "neutral target re-entry")
            self.checks.append({"kind": "focus_loss", "release_received_by_new_focus": True,
                                "original_target_neutral_on_reentry": True,
                                "loss_detection_seconds": (action["cancel_handled_ns"] - action["started_ns"]) / 1e9})
            self.begin(["W"])
            self.release()
        finally:
            if second.poll() is None:
                second.stdin.write(b"close\n")
                second.stdin.flush()
            out.close()
            err.close()

    def failed_reset_scenario(self):
        self.input.uncertain = True
        self.call(self.eis.disconnect, self.dbus.Int32(self.input.cookie))
        self.input.dispose()
        try:
            missing = self.dbus.Interface(self.bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/Missing", introspect=False),
                                          "org.kde.KWin.EIS.RemoteDesktop")
            self.call(missing.connectToEIS, self.dbus.Int32(1), seconds=.5)
        except Failure as exc:
            assert exc.code == "dbus_failed"
            self.checks.append({"kind": "reset_failure", "code": exc.code, "input_unavailable": not self.input.ready()})
        else:
            raise AssertionError("Missing reset endpoint succeeded")
        self.input.reject_probe("failed_reset")
        self.reset()
        self.begin(["W"])
        self.release()

    def slow_query_scenario(self):
        action = self.begin(["W"])
        pending = self.request_query("slow-query")
        pending["poll"] = True
        self.wait(lambda: bool(action.get("cancel_reason")), .7, "slow-query cancellation")
        assert action["cancel_reason"] == "focus_query_timeout", action
        self.wait(lambda: not self.received_held, .5, "release after stalled compositor")
        self.wait(lambda: "result" in pending, 2, "slow query cleanup")
        assert not pending["result"]["ok"] and pending["result"]["code"] == "timeout", pending
        self.checks.append({"kind": "slow_query", "cancelled_while_child_busy": True,
                            "query_error": pending["result"]["code"],
                            "detection_seconds": (action["cancel_handled_ns"] / 1e9) - pending["started"]})
        self.action = None

    def run(self, scenario):
        self.initial()
        if scenario == "input":
            self.input_scenario()
        elif scenario == "cancel":
            self.cancel_scenario()
        elif scenario in ("removal", "disconnect", "pause"):
            self.lifecycle_scenario(scenario)
        elif scenario == "reset-failure":
            self.failed_reset_scenario()
        elif scenario == "focus-loss":
            self.focus_loss_scenario()
        elif scenario == "slow-query":
            self.slow_query_scenario()
        self.result["outcome"] = "passed"

    def close(self):
        try:
            self.cancel("probe_close")
            self.input.release()
            self.input.dispose()
        finally:
            for source in self.sources:
                self.GLib.source_remove(source)
            self.bus.close()
            self.bridge.stdin.close()
            for child in self.children:
                try:
                    child.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=.5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream:
                        stream.close()
            self.result.update(gating_checks=self.input.gating_checks, heartbeat_max_seconds=self.heartbeat_max,
                               emission_count=self.emissions, final_fixture_held=sorted(self.received_held),
                               final_input_uncertain=self.input.uncertain)
            harness.atomic(self.artifacts / "libei-probe.json", self.result)
            self.timeline_stream.close()
            self.fixture.close()
            self.bridge_err.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_cancel":
        fd, mode, receipt = int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
        time.sleep(.05)
        stamp = time.monotonic_ns()
        receipt.write_text(json.dumps({"requested_ns": stamp, "mode": mode}))
        if mode == "send":
            os.write(fd, str(stamp).encode())
        os.close(fd)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("scenario", choices=("input", "cancel", "removal", "disconnect", "pause", "reset-failure", "focus-loss", "slow-query"))
    args = parser.parse_args()
    probe = Probe(args.binary, args.scenario)
    try:
        probe.run(args.scenario)
        return 0
    except Exception as exc:
        probe.result.update(outcome="failed", error={"code": getattr(exc, "code", "assertion"), "message": str(exc)})
        raise
    finally:
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
