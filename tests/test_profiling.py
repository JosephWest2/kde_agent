"""Opt-in owner-loop profile: nothing wrapped by default; spans, stalls and late probes when on."""
import builtins
from contextlib import redirect_stdout
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from agent_desktop import profiling
from agent_desktop.artifacts import Store

SPEC = importlib.util.spec_from_file_location('owner_profile', Path(__file__).resolve().parents[1] / 'tools/owner_profile.py')
owner_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_profile)


class FakeGLib:
    def __init__(self):
        self.sources = {}

    def timeout_add(self, interval, callback):
        self.sources[len(self.sources) + 1] = (interval, callback)
        return len(self.sources)

    def source_remove(self, source):
        del self.sources[source]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ProfilingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent-profile-test-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'artifacts'
        self.store = Store(str(root), 'profile', uuid.uuid4().hex, create=True)
        self.addCleanup(self.store.close)
        self.output = self.store.path / 'logs' / profiling.FILENAME

    def profiler(self, **kwargs):
        profiler = profiling.Profiler(self.store, FakeGLib(), **kwargs)
        self.addCleanup(profiler.close)
        return profiler

    def records(self, kind=None):
        lines = [json.loads(line) for line in self.output.read_text().splitlines()]
        return [line for line in lines if kind is None or line['kind'] == kind]

    def test_disabled_wraps_nothing_and_writes_nothing(self):
        # worker.py and lifecycle.py check this name without importing the profiler.
        self.assertEqual(profiling.ENV, 'AGENT_DESKTOP_PROFILE_OWNER')
        before = (os.fsync, builtins.__import__, Store._write)
        self.assertIsNone(profiling.install(self.store, FakeGLib(), environ={}))
        self.assertIsNone(profiling.install(self.store, FakeGLib(), environ={profiling.ENV: 'yes'}))
        self.assertEqual((os.fsync, builtins.__import__, Store._write), before)
        self.assertFalse(self.output.exists())

    def test_every_target_resolves_and_close_restores_it(self):
        for target in profiling.TARGETS:
            importlib.import_module(profiling.PACKAGE + '.' + target[0].split('.', 1)[0])
        original = Store.__dict__['_write']
        glib = FakeGLib()
        profiler = profiling.install(self.store, glib, environ={profiling.ENV: '1'})
        self.assertIsNotNone(profiler)
        self.addCleanup(profiler.close)
        self.assertEqual(profiler.pending, {})
        self.assertEqual(len(profiler.restore), len(profiling.TARGETS) + 2)  # Plus os.fsync and __import__.
        self.assertIsNot(Store.__dict__['_write'], original)
        self.assertEqual(list(glib.sources.values())[0][0], profiling.PROBE_MS)
        profiler.close()
        self.assertIs(Store.__dict__['_write'], original)
        self.assertIs(builtins.__import__, profiler.original_import)
        self.assertEqual(glib.sources, {})
        self.assertTrue(self.records('summary')[-1]['final'])

    def test_module_imported_later_is_wrapped_when_its_import_finishes(self):
        name = profiling.PACKAGE + '.watchdog'
        importlib.import_module(name)
        loaded = sys.modules.pop(name)
        self.addCleanup(sys.modules.__setitem__, name, loaded)
        profiler = self.profiler()
        self.assertIn(name, profiler.pending)
        fresh = __import__(name, fromlist=['Watchdog'])  # The import statement path, not importlib.
        self.assertNotIn(name, profiler.pending)
        self.assertIsNot(fresh.Watchdog.__dict__['tick'], loaded.Watchdog.__dict__['tick'])
        self.assertTrue(hasattr(fresh.Watchdog.__dict__['tick'], '__wrapped__'))

    def test_stall_and_late_probe_attribute_time_to_the_nested_call(self):
        clock = Clock()
        profiler = self.profiler(clock=clock)

        def slow_write():
            clock.now += .030

        def root():
            clock.now += .001
            profiler._span('Store._write', slow_write, site=True)()

        profiler.probe()
        clock.now += .005
        profiler._span('Server.service', root)()
        profiler.probe()
        stall, = self.records('stall')
        self.assertEqual(stall['root'], 'Server.service')
        self.assertEqual(stall['ms'], 31.0)
        (depth0, root_name, _, root_ms, root_self), (depth1, name, offset, ms, own) = stall['spans']
        self.assertEqual((depth0, root_name, root_ms, root_self), (0, 'Server.service', 31.0, 1.0))
        self.assertEqual((depth1, offset, ms, own), (1, 1.0, 30.0, 30.0))
        self.assertTrue(name.startswith('Store._write@test_profiling.py:'))
        late, = self.records('late')
        self.assertEqual(late['ms'], 31.0)
        self.assertEqual(late['roots'][0][0], 'Server.service')
        profiler.summary()
        summary = self.records('summary')[-1]
        self.assertEqual(summary['probe']['samples'], 1)
        self.assertEqual(summary['probe']['hist'], {'31.0': 1})
        self.assertEqual(summary['paths']['Server.service>' + name], [1, 30.0, 30.0, 30.0, 30.0])

    def test_gc_inside_an_import_is_counted_once(self):
        clock = Clock()
        profiler = self.profiler(clock=clock)

        def original_import(*args):
            clock.now += .002
            profiler._gc('start', {'generation': 1})
            clock.now += .003
            profiler._gc('stop', {'generation': 1})
            clock.now += .001
            return sys
        profiler.original_import = original_import  # The patched builtin is restored from its own record.
        profiler._span('Server.service', lambda: profiler._import('somewhere', {'__name__': 'here'}))()
        ms = lambda values: [round(value * 1000, 3) for value in values]  # noqa: E731
        self.assertEqual(ms(profiler.paths['Server.service'][1:3]), [6.0, 0.0])
        self.assertEqual(ms(profiler.paths['Server.service>import[here:somewhere]'][1:3]), [6.0, 3.0])
        self.assertEqual(ms(profiler.paths['Server.service>gc[1]'][1:3]), [3.0, 3.0])

    def test_failed_write_turns_the_profile_off(self):
        profiler = self.profiler()
        os.close(profiler.fd)
        profiler.fd = os.open(os.devnull, os.O_RDONLY)
        profiler.summary()
        self.assertFalse(profiler.active)
        self.assertIsNone(profiler.fd)
        self.assertEqual(profiler._span('x', lambda: 7)(), 7)

    def test_analyzer_reads_what_the_profiler_writes(self):
        root = Path(self.store.root).parent / 'agent-desktop-smoke-abcd1234'
        store = Store(str(root), 'profile', uuid.uuid4().hex, create=True)
        self.addCleanup(store.close)
        clock = Clock()
        profiler = profiling.Profiler(store, FakeGLib(), clock=clock)
        self.addCleanup(profiler.close)

        class Device:
            held = []

        class Input:
            devices = {'keyboard': Device()}

            def press(self):
                self.devices['keyboard'].held.append(30)

            def release(self):
                self.devices['keyboard'].held.clear()

        class Work:
            class request:
                operation, request_id = 'key', 'r' * 32

            class task:
                hold, phase = .05, 'emit'
        profiler.scheduler = type('Scheduler', (), {'active': Work})()
        keyboard = Input()
        press, release = (profiler._input('Input.' + name, getattr(Input, name)) for name in ('press', 'release'))

        def fsync():
            clock.now += .060

        profiler.probe()
        clock.now += .005
        press(keyboard)
        clock.now += .001
        profiler._span('Server.service', profiler._span('os.fsync', fsync, site=True))()
        profiler.probe()
        clock.now += .049
        release(keyboard)
        profiler.close()

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(owner_profile.main([str(root)]), 0)
        report = output.getvalue()
        self.assertRegex(report, r'smoke +1 +61\.0')
        self.assertIn('>50ms: 1, >100ms: 0, >250ms: 0', report)
        self.assertRegex(report, r'fsync +60\.0')
        self.assertIn('Stalls of 50ms or more by trigger', report)
        self.assertIn('during a held key or button: 1 of 1; held time inside them 60 ms', report)
        self.assertRegex(report, r'key hold 50ms +n=1 +median +60\.0')

    def test_bucket_resolution(self):
        self.assertEqual([profiling.bucket(v) for v in (0.04, 2.37, 12.9, 140.2)], [0.0, 2.3, 12.0, 140.0])


if __name__ == '__main__':
    unittest.main()
