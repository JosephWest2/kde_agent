"""Opt-in owner-loop profile: nothing wrapped by default; spans, stalls and late probes when on."""
import builtins
from contextlib import redirect_stdout
import gc
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_gc_during_summary_is_accounted_after_the_snapshot(self):
        clock = Clock()
        profiler = self.profiler(clock=clock)
        profiler._span('Server.service', lambda: None)()
        original, collected = profiling._ms, []

        def collecting(seconds):
            if not collected:  # A collection while the totals are being iterated.
                collected.append(True)
                profiler._gc('start', {'generation': 2})
                clock.now += .004
                profiler._gc('stop', {'generation': 2})
            return original(seconds)
        with patch.object(profiling, '_ms', collecting):
            profiler.summary()
        self.assertTrue(profiler.active)
        self.assertNotIn('gc[2]', self.records('summary')[-1]['paths'])
        self.assertEqual(profiler.paths['gc[2]'][0], 1)
        self.assertEqual(self.records('failed'), [])

    def test_profiler_fault_turns_it_off_without_reaching_the_worker(self):
        original = Store.__dict__['_write']
        glib = FakeGLib()
        profiler = profiling.install(self.store, glib, environ={profiling.ENV: '1'})
        self.addCleanup(profiler.close)
        profiler.stack.append(None)  # Corrupt bookkeeping: the next enter() fails.

        def raises():
            raise KeyError('from the worker')
        with self.assertRaises(KeyError):
            profiler._span('x', raises)()
        self.assertFalse(profiler.active)
        self.assertEqual(profiler._span('x', lambda: 7)(), 7)  # Calls straight through now.
        self.assertFalse(profiler.probe())
        self.assertIsNone(profiler.source)
        profiler.close()
        self.assertIs(Store.__dict__['_write'], original)
        self.assertIs(builtins.__import__, profiler.original_import)
        self.assertNotIn(profiler._gc, gc.callbacks)
        failed, = self.records('failed')
        self.assertTrue(failed['error'].startswith('TypeError'))
        self.assertEqual(self.records('summary'), [])  # No summary from a broken profile.

    def test_close_restores_even_when_the_final_summary_fails(self):
        original = Store.__dict__['_write']
        profiler = profiling.install(self.store, FakeGLib(), environ={profiling.ENV: '1'})
        profiler.paths['broken'] = None  # The final summary cannot be built.
        profiler.close()
        self.assertIs(Store.__dict__['_write'], original)
        self.assertEqual(len(self.records('failed')), 1)

    def test_write_time_is_charged_to_profiler_write(self):
        clock = Clock()
        profiler = self.profiler(clock=clock)
        write = os.write

        def slow(fd, data):
            clock.now += .020
            return write(fd, data)
        profiler.probe()
        with patch.object(profiling.os, 'write', slow):
            clock.now += .005
            profiler._span('Server.service', lambda: setattr(clock, 'now', clock.now + .015))()  # A stall record.
            profiler.probe()
        # start, the first summary, then the slow stall and late-probe records.
        self.assertEqual(profiler.roots[profiling.WRITE][0], 4)
        self.assertEqual(round(profiler.roots[profiling.WRITE][1], 3), .040)
        self.assertEqual([r['root'] for r in self.records('stall')], ['Server.service'])
        late, = self.records('late')
        self.assertIn(profiling.WRITE, [root[0] for root in late['roots']])

    def test_byte_budget_ends_with_a_final_summary_and_a_marker(self):
        profiler = self.profiler()
        with patch.object(profiling, 'BUDGET_BYTES', 4096):
            while profiler.active:
                profiler.event('press', note='x' * 200)
        *_, summary, marker = self.records()
        self.assertEqual((summary['kind'], summary['final'], marker['kind']), ('summary', True, 'truncated'))
        self.assertLessEqual(marker['bytes'], 4096 + len(json.dumps(summary)) + 1)
        self.assertEqual(profiler._span('x', lambda: 7)(), 7)
        self.assertFalse(profiler.probe())
        before = self.output.stat().st_size
        profiler.event('press')
        profiler.close()
        self.assertEqual(self.output.stat().st_size, before)

    def test_wire_operation_labels_are_a_fixed_set(self):
        label = profiling._wire
        self.assertEqual(label((None, {'operation': 'windows'}), {}), 'windows')
        self.assertEqual(label((None, {'operation': 'request.cancel'}), {}), 'request.cancel')
        for value in ({'operation': 'x' * 4096}, {'operation': ['windows']}, {'operation': None}, b'raw', None):
            self.assertEqual(label((None, value), {}), 'invalid')

    def test_release_record_carries_the_state_after_the_call(self):
        profiler = self.profiler()

        class Device:
            def __init__(self):
                self.held = [30]

        class Input:
            uncertain, retired_held = False, []

            def __init__(self):
                self.devices = {'keyboard': Device()}

            def release(self):
                self.uncertain = True  # The release could not be sent: keys stay held.
        release = profiler._input('Input.release', Input.release)
        release(Input())
        record, = self.records('input')
        self.assertEqual((record['ev'], record['held_before'], record['held'], record['uncertain']),
                         ('release', 1, 1, True))

    def test_analyzer_matches_timeouts_by_overlap_in_their_own_profile(self):
        root = Path(self.store.root).parent / 'agent-desktop-smoke-abcd1234' / 'logs'
        other = Path(self.store.root).parent / 'agent-desktop-smoke-efgh5678' / 'logs'
        for folder in (root, other):
            folder.mkdir(parents=True)
        lines = [{'kind': 'start', 't': 9.0},
                 {'kind': 'done', 't': 10.4, 'op': 'windows', 'rid': 'abcd', 'ok': False, 'code': 'timeout',
                  'admitted': 9.9, 'deadline': 10.4},
                 # The stall began during the request; its late probe ran after the response.
                 {'kind': 'late', 't': 10.6, 'ms': 400.0, 'roots': []},
                 {'kind': 'summary', 't': 11.0, 'final': True, 'probe': {'samples': 2, 'max_ms': 400.0,
                  'hist': {'400.0': 1, '0.0': 1}}, 'roots': {}, 'paths': {}}]
        (root / 'owner-profile.jsonl').write_text(''.join(json.dumps(line) + '\n' for line in lines))
        # A same-named scenario elsewhere must not lend its stalls.
        (other / 'owner-profile.jsonl').write_text(json.dumps(
            {'kind': 'late', 't': 10.2, 'ms': 300.0, 'roots': []}) + '\n')
        output = io.StringIO()
        with redirect_stdout(output):
            owner_profile.main([str(root.parent.parent)])
        self.assertRegex(output.getvalue(), r'windows +abcd admitted +500 ms; owner late +205 ms of it, '
                                            r'worst late probe 400\.0 ms')

    def test_bucket_resolution(self):
        self.assertEqual([profiling.bucket(v) for v in (0.04, 2.37, 12.9, 140.2)], [0.0, 2.3, 12.0, 140.0])


if __name__ == '__main__':
    unittest.main()
