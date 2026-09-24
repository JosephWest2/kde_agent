"""Real lifecycle scenarios exercising the installed production async EIS owner.

Run with an installed-wheel interpreter inside tools/private_harness.py. This
reuses M1 fixture/observational scenario logic, never its Input implementation.
The pause plugin's exact test selector is supplied through name_prefix only here.
"""
import importlib.util
import json
from pathlib import Path
import os
import sys
import time
from agent_desktop import input_connection, libei_binding
from agent_desktop.private_bus import PrivateBus

PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('m1_scenarios', PROJECT / 'tools/libei_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
assert not Path(input_connection.__file__).resolve().is_relative_to(PROJECT / 'src')
for path in Path(input_connection.__file__).parent.glob('*'):
    if path.is_file():
        assert path.read_bytes() == (PROJECT / 'src/agent_desktop' / path.name).read_bytes(), path


class Input(input_connection.Input):
    """Evidence-only naming/mapping and assertions, all native lifecycle in product."""
    def __init__(self, owner):
        self.owner = owner
        super().__init__(owner.generation, owner.GLib, invalidated=owner.cancel,
                         failed=lambda error: setattr(owner, 'fatal', error), log=owner.log,
                         emit=owner.emit, name_prefix='issue12')
        self.gating_checks = []

    @property
    def held(self):
        return {d.identity: d.held for d in self.devices.values()}

    def press(self, names):
        try:
            codes = [probe.KEYS[name] for name in names]
        except KeyError:
            raise probe.Failure('unsupported_input', 'Unknown test key')
        return super().press(codes)

    def reject_probe(self, cause):
        before = self.owner.emissions
        try:
            self.keyboard()
        except input_connection.Failure as error:
            assert error.code == 'input_unavailable'
            self.gating_checks.append(dict(cause=cause, epoch=self.epoch,
                                           emissions_unchanged=before == self.owner.emissions))
        else:
            raise AssertionError('Input unexpectedly usable: ' + cause)


probe.Input = Input
probe.binding = libei_binding


class Probe(probe.Probe):
    def __init__(self, binary, scenario):
        super().__init__(binary, scenario)
        self.private = PrivateBus(self.env['DBUS_SESSION_BUS_ADDRESS'], time.monotonic()+3)
        self.sources.append(self.GLib.timeout_add(5, self.private_tick))
        self.result.update(scope='installed production connection / fixture-observed lifecycle',
            production_module=input_connection.__file__,
            production_sha256=libei_binding.digest(input_connection.__file__),
            binding_sha256=libei_binding.digest(libei_binding.__file__),
            installed_hashes={p.name: libei_binding.digest(p) for p in Path(input_connection.__file__).parent.glob('*') if p.is_file()},
            runner_sha256=libei_binding.digest(__file__), async_negotiation=True)
        # The cookie must be disconnected by the same retained originating bus.
        self.eis = type('EIS', (), {'disconnect': lambda _self, cookie, **kw: self.disconnect(cookie, **kw)})()

    def private_tick(self):
        try:
            self.private.tick()
            self.input.tick()
        except Exception as error:
            self.fatal = error
        return True

    def disconnect(self, cookie, *, reply_handler, error_handler, timeout):
        self.private.call('evidence-disconnect', 'org.kde.KWin', '/org/kde/KWin/EIS/RemoteDesktop',
            'org.kde.KWin.EIS.RemoteDesktop', 'disconnect', self.GLib.Variant('(i)', (int(cookie),)),
            '()', min(self.deadline, time.monotonic()+timeout),
            lambda value, fds, error: error_handler(error) if error else reply_handler())

    def connect(self, deadline=None):
        end = min(self.deadline, deadline or time.monotonic()+3)
        self.wait(lambda: self.private.connection is not None, max(0, end-time.monotonic()), 'private bus')
        self.input.connect(self.private, end)
        self.wait(self.input.usable_device, max(0, end-time.monotonic()), 'resumed keyboard')
        self.log('ready', epoch=self.input.epoch, cookie=self.input.cookie)

    def reset(self):
        started = time.monotonic(); end = min(self.deadline, started+3)
        self.cancel('reset'); self.input.resetting = True
        old_epoch = self.input.epoch
        old = self.input.snapshot()
        if self.input.cookie is not None:
            self.call(self.eis.disconnect, self.dbus.Int32(self.input.cookie), seconds=max(.001,end-time.monotonic()))
        self.input.dispose()
        # Evidence reset has fixture neutral receipts; this is not public reset.
        self.wait(lambda: not self.received_held and self.modifiers in (None,0), min(.5,end-time.monotonic()), 'neutral')
        self.input.retired_held.clear()
        self.input.uncertain = False
        self.input.error = None
        self.connect(end)
        self.input.reject_probe('reset_not_yet_neutral')
        self.input.resetting = False
        self.checks.append(dict(kind='reset', old_epoch=old_epoch, new_epoch=self.input.epoch,
            prior_uncertain=old['uncertain'], fixture_neutral=True, seconds=time.monotonic()-started))

    def failed_reset_scenario(self):
        self.call(self.eis.disconnect, self.dbus.Int32(self.input.cookie))
        self.input.dispose()
        self.input.uncertain = True
        class MissingEndpoint:
            def call(_self, token, destination, path, *args, **kwargs):
                return self.private.call(token, destination, '/org/kde/KWin/EIS/Missing', *args, **kwargs)
            def cancel(_self, token):
                self.private.cancel(token)
        self.input.connect(MissingEndpoint(), time.monotonic()+.5)
        try:
            self.wait(lambda: self.input.error is not None, .6, 'expected failed negotiation')
        except input_connection.Failure as error:
            assert error.code == 'input_unavailable'
        assert self.input.error is not None and not self.input.ready()
        self.input.reject_probe('failed_reset')
        self.checks.append(dict(kind='reset_failure', production_async_reply=True,
                               code=self.input.error.code, input_unavailable=True))
        self.fatal = None
        self.input.error = None
        self.reset()
        self.begin(['W']); self.release()

    def run(self, scenario):
        super().run(scenario)
        if scenario == 'input':
            before = len(os.listdir('/proc/self/fd'))
            for _ in range(8):
                self.reset(); self.begin(['W']); self.release()
            after = len(os.listdir('/proc/self/fd'))
            assert after == before, (before,after)
            self.checks.append(dict(kind='repeated_replacement', count=8, before_fds=before, after_fds=after))

    def close(self):
        try:
            super().close()
        finally:
            self.private.close()


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='_cancel':
        raise SystemExit(probe.main())
    value = Probe(Path(sys.argv[1]), sys.argv[2])
    try:
        value.run(sys.argv[2])
    except Exception as error:
        value.result.update(outcome='failed', error={'message': str(error), 'type': type(error).__name__})
        raise
    finally:
        value.close()
