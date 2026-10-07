"""Typed decoding, owned async processes, retention and bounded cleanup regressions."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_desktop import windows
from agent_desktop.window_types import Bounds, Decoder, Window, encoded
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.children import Children
from agent_desktop.owner_time import Budget, OwnerClock
from agent_desktop.artifacts import Store, safe_projection
from agent_desktop.app_processes import Registry, Application, birth

GEN = 'a' * 32


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now
UID = '12345678-1234-1234-1234-123456789abc'


def row(**changes):
    return {'uuid': UID, 'pid': None, 'popup': False, 'title': None, 'class': None, 'client': None,
            'frame': None, 'active': False} | changes


def payload(rows=None, **changes):
    return {'schema_version': 1, 'request_id': 'query', 'active_uuid': None,
            'windows': [row()] if rows is None else rows,
            'outputs': [{'name': 'Virtual-1', 'width': 1280, 'height': 720, 'scale': 1}]} | changes


def decoded(value):
    result = Decoder(json.dumps(value).encode(), 'query')
    while not result.step(float('inf'), lambda: 0):
        pass
    return result


class TypesTests(unittest.TestCase):
    def test_nullable_distinct_titles_canonical_identity_fractional_geometry(self):
        value = payload([row(uuid='{' + UID.upper() + '}', title='', client={'x': -.5, 'y': 2.25, 'width': 1.2, 'height': 2}),
                         row(uuid=str(uuid.uuid4()), title='')])
        result = decoded(value)
        self.assertEqual(len(result.windows), 2)
        self.assertIn(UID, result.seen)
        self.assertEqual(result.windows[0].wire(GEN)['title'], '')

    def test_invalid_fields_and_active_consistency(self):
        # pid 0 is rejected only by the decoder: window_query.js maps KWin's pid 0
        # (X11, unknown process) to null before it gets here, see the test below.
        cases = [payload([None]), payload([row(), row(uuid='{' + UID.upper() + '}')]),
                 payload(schema_version=True), payload(active_uuid=UID), payload([row(pid=True)]),
                 payload([row(pid=-2)]), payload([row(pid=0)]), payload([row(title=3)]), payload([row(title='x' * 4097)]),
                 payload([row(uuid=None)]), payload([row(active=True)]), payload([row(client={})]),
                 payload([row(client={'x':0, 'y':0, 'width':True, 'height':3})]),
                 payload([row(client={'x':10**300, 'y':0, 'width':3, 'height':3})]),
                 payload([row(client={'x':0, 'y':0, 'width':0, 'height':3})]),
                 payload([row(uuid=str(uuid.uuid4())) for _ in range(257)])]
        for value in cases:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ContractError) as caught:
                decoded(value)
            self.assertEqual(caught.exception.code, 'window_query_failed')

    def test_kwin_row_classification(self):
        # Captured from KWin 6.7.5: the header-bar window menu (InternalWindow, pid -1)
        # and a GTK tooltip (xdg_popup of the editor).
        menu = row(uuid='ec393468-0268-481e-bd6c-56092c6badcf', pid=-1, popup=True, title='', **{'class': ''},
                   client={'x': 640, 'y': 123, 'width': 260, 'height': 127},
                   frame={'x': 640, 'y': 123, 'width': 260, 'height': 127})
        tooltip = row(uuid='fb3ae9b6-1993-41d4-adcc-7d94310ff76e', pid=695345, popup=True, title='',
                      **{'class': 'gnome-text-editor'}, client={'x': 355, 'y': 144, 'width': 84, 'height': 32})
        editor = row(pid=695345, title='New Document', active=True)
        result = decoded(payload([menu, tooltip, editor], active_uuid=UID))
        kinds = {w.uuid: w.wire(GEN) for w in result.windows}
        self.assertEqual(kinds[menu['uuid']]['kind'], 'compositor')
        self.assertIsNone(kinds[menu['uuid']]['pid'])
        self.assertEqual(kinds[tooltip['uuid']]['kind'], 'popup')
        self.assertEqual(kinds[tooltip['uuid']]['pid'], 695345)
        self.assertEqual(kinds[UID]['kind'], 'window')
        # A compositor surface may also be the active window; it stays consistent.
        self.assertEqual(decoded(payload([row(pid=-1, popup=False, active=True)], active_uuid=UID)).windows[0].kind,
                         'compositor')

    def test_query_maps_unknown_pid_zero_to_null_but_passes_compositor_minus_one(self):
        source = Path(windows.__file__).with_name('window_query.js').read_text()
        self.assertIn('pid:w.pid == null || w.pid === 0 ? null : w.pid,', source)
        # Such a row decodes as an ordinary window with no pid (no association).
        self.assertEqual(decoded(payload([row(pid=None, popup=False)])).windows[0].wire(GEN)['kind'], 'window')

    def test_unidentified_rows_still_fail_closed(self):
        # Only pid -1 identifies a compositor surface, and only KWin's own boolean
        # popupWindow flag a popup; every other field keeps its strict checks.
        cases = [row(popup=None), row(popup=1), row(popup='true'), {k: v for k, v in row().items() if k != 'popup'},
                 row(pid=-1, popup=None), row(pid=-1, popup=True, title=3), row(pid=-1, popup=True, uuid=None),
                 row(pid=-1, popup=True, client={'x': 0, 'y': 0, 'width': 0, 'height': 1}),
                 row(pid=-1, popup=True, active=True), row(pid=-1.0, popup=True), row(pid=True, popup=True),
                 row(popup=True, active=True), row(kind='window'), row(pid=-3, popup=True)]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ContractError) as caught:
                decoded(payload([value]))
            self.assertEqual(caught.exception.code, 'window_query_failed')
        # One malformed row still rejects the whole observation, beside a valid menu.
        with self.assertRaises(ContractError):
            decoded(payload([row(uuid=str(uuid.uuid4()), pid=-1, popup=True), row(popup=None)]))

    def test_strict_json_and_size(self):
        for raw in (b'{}{}', b'{"a":1,"a":2}', b'\xff', b'{"x":NaN}', b'[' * 1000,
                    b' ' * (262144 + 1), b'null'):
            with self.subTest(raw=raw[:20]), self.assertRaises(ContractError):
                Decoder(raw, 'query')

    def test_fixed_source_has_no_caller_code_or_test_operations(self):
        source = Path(windows.__file__).with_name('window_query.js').read_text()
        self.assertNotIn('check_data', source)
        self.assertNotIn('eval(', source)
        for attack in ('";throw 1;//', '${x}', '`evil`', '\u2028', '\n', '\\'):
            with self.assertRaises(ContractError):
                encoded(attack)
        self.assertEqual(encoded('query'), 'const request = {"request_id": "query"};\n')

    def test_output_scale_unavailable_is_explicit_null(self):
        value = payload()
        value['outputs'][0]['scale'] = None
        self.assertIsNone(decoded(value).output.scale)

    def test_max_rows_and_bounded_decode_turns(self):
        value = payload([row(uuid=str(uuid.uuid4())) for _ in range(256)])
        result = Decoder(json.dumps(value).encode(), 'query')
        turns = 0
        while not result.step(float('inf'), lambda: 0):
            turns += 1
        self.assertEqual(turns, 16)


class Bus:
    loaded = False
    collision = False
    def __init__(self, address, deadline):
        self.connection = object()
        self.error = None
        self.closed = False
    def tick(self):
        if self.error:
            raise self.error
    def call(self, token, dest, path, iface, method, args, signature, deadline, callback):
        callback(SimpleNamespace(unpack=lambda: (self.loaded or self.collision,)), None, None)
    def close(self):
        self.closed = True


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desktop_root = self.root / 'desktop'
        self.desktop_root.mkdir(mode=0o700)
        (self.desktop_root / 'tmp').mkdir(mode=0o700)
        self.store = Store(self.root / 'artifacts', 'default', GEN, create=True)
        self.children = Children()
        self.desktop = SimpleNamespace(root=self.desktop_root, children=self.children, store=self.store,
                                       env={'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/private', 'PATH': '/usr/bin'})
        self.adapter = windows.Adapter(self.desktop, GEN, '/test/kdotool')
        self.mode = 'success'
        original = self.children.start
        def launch(argv, **kwargs):
            if '--remove' in argv:
                Bus.loaded = False
                source = 'pass'
            elif self.mode == 'hang':
                source = 'import time; time.sleep(10)'
            elif self.mode in ('flood', 'stderr_flood'):
                source = 'import os;\nwhile True: os.write(' + ('2' if self.mode == 'stderr_flood' else '1') + ',b"x"*4096)'
            elif self.mode == 'bad':
                source = 'print("not JSON")'
            else:
                value = payload(request_id=argv[2])
                source = 'print(' + repr(json.dumps(value)) + ')'
            return original([sys.executable, '-c', source], **kwargs)
        self.children.start = launch
        self.bus_patch = patch.object(windows, 'PrivateBus', Bus)
        self.bus_patch.start()
        Bus.loaded = Bus.collision = False
    def tearDown(self):
        self.bus_patch.stop()
        self.children.close()
        for _ in range(50):
            self.children.poll()
            if not self.children.owned:
                break
            time.sleep(.002)
        self.store.close()
        self.temp.cleanup()
    def drive(self, query):
        result = None
        for _ in range(1000):
            self.children.poll()
            result = query.step()
            if result is not None:
                return result
            time.sleep(.001)
        self.fail('query did not finish')
    def clean(self, query):
        for _ in range(1000):
            self.children.poll()
            if query.cleanup():
                return
            time.sleep(.001)
        self.fail('cleanup did not finish')
    def test_success_removes_temporary_pipes_script_and_publishes_immutable_observation(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        result = self.drive(query)
        self.assertFalse(query.folder.exists())
        self.assertFalse(query.streams)
        self.assertIsNone(query.dir_fd)
        self.assertIsNone(self.adapter.active)
        self.assertIsNotNone(query.child.returncode)
        self.assertTrue((self.store.path / result['query_artifact']).exists())
        self.assertEqual(result['windows'][0]['app'], None)
        self.assertLess(result['accepted_at'], query.deadline)

    def test_acceptance_waits_for_a_held_back_observation_write_without_using_the_budget(self):
        from agent_desktop.writer import Journal
        import threading
        gate = threading.Event()
        original = self.store.window_observation
        def held(*args):
            gate.wait(5)
            return original(*args)
        self.store.window_observation = held
        journal = self.store.journal = Journal(self.store, threaded=True)
        self.addCleanup(lambda: (gate.set(), journal.close(time.monotonic() + 2)))
        query = self.adapter.start('request', time.monotonic() + 5)
        for _ in range(2000):
            self.children.poll()
            journal.drain()
            self.assertIsNone(query.step())
            if query.phase == 'observed':
                break
            time.sleep(.001)
        # Held for longer than the .5s work budget: the query neither accepts nor times out.
        until = time.monotonic() + .7
        while time.monotonic() < until:
            journal.drain()
            self.assertIsNone(query.step())
            self.assertEqual(query.phase, 'observed')
            self.assertIsNone(query.accepted_at)
            time.sleep(.005)
        gate.set()
        for _ in range(1000):
            journal.drain()
            result = query.step()
            if result is not None:
                break
            time.sleep(.001)
        self.assertTrue((self.store.path / result['query_artifact']).exists())
        self.assertEqual(result['observation_state'], 'accepted')

    def test_activation_native_exact_braces_and_cleanup_without_observation(self):
        guard = Mock()
        action = self.adapter.activate('request', time.monotonic() + .5,
            {'generation': GEN, 'window_id': '{' + UID.upper() + '}'}, guard)
        self.assertEqual(action._argv(), ['/test/kdotool', '--name', action.name,
                                         'windowactivate', '{' + UID + '}'])
        with patch.object(self.store, 'window_observation', wraps=self.store.window_observation) as publish:
            result = self.drive(action)
        guard.assert_called_once()
        publish.assert_not_called()
        self.assertIn('activation_completed_at', result)
        self.assertNotIn('windows', result)
        self.assertTrue(action.absent)
        self.assertFalse(action.folder.exists())
        self.assertIsNone(self.adapter.active)

    def test_normal_close_native_process_cleanup_and_no_fabricated_observation(self):
        action = self.adapter.request_close('request', time.monotonic() + .5,
            {'generation': GEN, 'window_id': UID}, Mock())
        self.assertEqual(action._argv()[-2:], ['windowclose', '{' + UID + '}'])
        with patch.object(self.store, 'window_observation') as publish:
            result = self.drive(action)
        self.assertIn('close_transport_completed_at', result)
        self.assertNotIn('windows', result)
        publish.assert_not_called()
        self.assertFalse(action.folder.exists())
        self.assertIsNotNone(action.child.returncode)
        self.assertIsNone(self.adapter.active)

    def test_close_hook_with_real_adapter_child_requires_host_progress(self):
        from agent_desktop.closing import CloseHook
        from test_targeting import APP, HANDLES, snapshot
        selected = Mock(step=Mock(return_value=snapshot()), recheck_selected=Mock(return_value=True))
        exited = [False]
        registry = SimpleNamespace(generation=GEN,
            observe_application_exit=lambda *a, **kw: ({'state': 'running', 'exit_code': None}, exited[0]),
            application_process_state=lambda *a: {'subtree_populated': not exited[0]})
        hook = CloseHook('hook', GEN, self.adapter, registry, lambda: None, Mock(), window=HANDLES[0])
        deadline = time.monotonic() + 1
        with patch.object(self.adapter, 'start', return_value=selected):
            self.assertIsNone(hook(time.monotonic(), deadline))
        native = hook.owner.operation
        for _ in range(1000):
            self.children.poll()  # Required owner responsibility, not hook magic.
            if hook.owner.dispatch == 'transport_completed':
                exited[0] = True
            result = hook(time.monotonic(), deadline)
            if result is not None:
                break
            time.sleep(.001)
        self.assertEqual(result['state'], 'complete')
        self.assertIsNotNone(native.child.returncode)
        self.assertFalse(native.folder.exists())
        self.assertIsNone(self.adapter.active)

    def test_activation_guard_runs_after_collision_and_failure_spawns_nothing(self):
        def guard():
            self.assertEqual(action.phase, 'dispatch')  # Collision checked; not yet spawned.
            self.assertFalse(action.call_pending)
            raise ContractError('target_lost', 'Lost before spawn.')
        action = self.adapter.activate('request', time.monotonic() + .5,
            {'generation': GEN, 'window_id': UID}, guard)
        with self.assertRaises(ContractError) as caught:
            self.drive(action)
        action.cancel(caught.exception)
        self.clean(action)
        self.assertFalse(action.spawned)
        self.assertIsNone(action.child)
        self.assertFalse(action.streams)

    def test_activation_guard_persistence_expiry_never_starts_child(self):
        def guard():
            action.budget = Budget(0)  # Spent while the guard persisted.
        action = self.adapter.activate('request', time.monotonic() + .5,
            {'generation': GEN, 'window_id': UID}, guard)
        with self.assertRaises(ContractError) as caught:
            self.drive(action)
        self.assertEqual(caught.exception.code, 'timeout')
        action.cancel(caught.exception)
        self.clean(action)
        self.assertFalse(action.spawned)
    def test_crash_malformed_hung_and_flood_cleanup(self):
        for mode in ('bad', 'hang', 'flood', 'stderr_flood'):
            with self.subTest(mode=mode):
                self.mode = mode
                query = self.adapter.start('request', time.monotonic() + .1)
                with self.assertRaises(ContractError) as caught:
                    self.drive(query)
                query.cancel(caught.exception)
                original_deadline = query.cleanup_deadline
                query.cancel('cancelled')
                self.assertEqual(original_deadline, query.cleanup_deadline)
                self.clean(query)
                self.assertFalse(query.folder.exists())
                self.assertFalse(query.streams)
                self.assertIsNotNone(query.child.returncode)
    def test_cancel_stopped_owned_child_and_remove_exact_script(self):
        self.mode = 'hang'
        query = self.adapter.start('request', time.monotonic() + .5)
        while query.child is None:
            query.step()
        os.kill(query.child.process.pid, signal.SIGSTOP)
        Bus.loaded = True
        query.cancel()
        with self.assertRaises(ContractError):
            self.adapter.start('other', time.monotonic() + .5)
        self.clean(query)
        self.assertEqual(query.child.returncode, -signal.SIGKILL)
        self.assertIsNotNone(query.remover)
        self.assertTrue(query.absent)
    def test_collision_never_removes_foreign_script(self):
        Bus.collision = True
        query = self.adapter.start('request', time.monotonic() + .5)
        with self.assertRaises(ContractError) as caught:
            self.drive(query)
        query.cancel(caught.exception)
        self.clean(query)
        self.assertFalse(query.spawned)
        self.assertIsNone(query.remover)
        self.assertTrue(Bus.collision)
    def test_success_cleanup_rejects_subdirectory_and_never_follows_symlink(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        query.step()
        outside = self.root / 'outside'
        outside.write_text('keep')
        (query.folder / 'link').symlink_to(outside)
        self.drive(query)
        self.assertEqual(outside.read_text(), 'keep')
        query = self.adapter.start('request', time.monotonic() + .5)
        query.step()
        (query.folder / 'unexpected').mkdir()
        with self.assertRaises(ContractError):
            self.drive(query)
        self.assertTrue((query.folder / 'unexpected').exists())
    def test_expired_before_preparation_has_no_effect(self):
        query = self.adapter.start('request', time.monotonic() - 1)
        with self.assertRaises(ContractError):
            query.step()
        self.assertIsNone(query.parent_fd)
        query.cancel('timeout')
        self.clean(query)
    def test_second_pipe_allocation_failure_closes_first_read_and_write(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        created = []
        original = os.pipe2
        def pipes(flags):
            if created:
                raise OSError('second allocation failed')
            pair = original(flags)
            created.extend(pair)
            return pair
        with patch.object(windows.os, 'pipe2', side_effect=pipes):
            with self.assertRaises(OSError):
                while query.child is None:
                    query.step()
        for fd in created:
            with self.assertRaises(OSError):
                os.fstat(fd)
        query.cancel('window_query_failed')
        self.clean(query)

    def test_cleanup_last_filesystem_call_cannot_cross_shared_reserve(self):
        for finished in (2.5, 2.501):
            with self.subTest(finished=finished):
                adapter = windows.Adapter(self.desktop, GEN, '/test/kdotool')
                query = adapter.start('request', 2.0)
                clock = [1.0]
                def late():
                    clock[0] = finished
                    return True
                with patch.object(windows.time, 'monotonic', side_effect=lambda: clock[0]):
                    query.cancel('timeout')
                    query.cleanup_phase = 'local'
                    with patch.object(query, '_local_cleanup', side_effect=late):
                        self.assertFalse(query.cleanup())
                    self.assertEqual(query.cleanup_deadline, 2.5)
                    self.assertEqual(query.error.code, 'timeout')
                    self.assertFalse(query.done)
                    self.assertIs(adapter.active, query)

    def test_256_refs_survive_repeated_pending_and_confirmed_checkpoints(self):
        request = make_request('launch',session='default',expected_generation=GEN,
                              caller_cwd='/',arguments={'argv':['/bin/true']})
        token = self.store.request(request,time.monotonic(),time.monotonic()+10)
        logs = {key:str(self.store.allocate(token,key)) for key in ('stdout','stderr')}
        appid = 'b'*32
        self.store.application_prepare(token,appid,executable='/bin/true',logs=logs,cgroup='/owned/applications/'+appid)
        registry=object.__new__(Registry);registry.generation=GEN;registry.store=self.store;registry.active=None
        previous=None
        for sequence in range(4):
            queryid=('%032x' % (sequence+1))
            current={'query_id':queryid,'query_artifact':'window-observations/'+queryid+'.json',
                     'observed_at':1.0+sequence,'accepted_at':1.1+sequence,
                     'windows':[{'generation':GEN,'window_id':str(uuid.UUID(int=sequence*256+i+1))} for i in range(256)]}
            registry.publish_windows({'generation':GEN,'application_id':appid},current,lambda:None)
            pending=self.store.application_read(appid)
            self.assertEqual(pending['window_observation'],previous)
            self.assertEqual(pending['pending_observation'],current)
            registry.tick()
            retained=self.store.application_read(appid)
            self.assertEqual(retained['windows'],current['windows'])
            self.assertEqual(retained['window_observation'],current)
            self.assertEqual(retained['previous_observation'],previous)
            self.assertIsNone(retained['pending_observation'])
            self.assertLess((self.store.path/'applications'/appid/'record.json').stat().st_size,65536)
            previous=current

    def test_retained_record_pending_previous_survives_post_replace_error(self):
        request = make_request('launch', session='default', expected_generation=GEN,
            caller_cwd='/', arguments={'argv':['/bin/true']})
        token = self.store.request(request, time.monotonic(), time.monotonic()+10)
        logs = {key:str(self.store.allocate(token,key)) for key in ('stdout','stderr')}
        appid='b'*32
        self.store.application_prepare(token,appid,executable='/bin/true',logs=logs,cgroup='/owned/applications/'+appid)
        first={'query_id':'1'*32,'query_artifact':'window-observations/'+('1'*32)+'.json',
               'observed_at':1.0,'accepted_at':1.1,'windows':[{'generation':GEN,'window_id':UID}]}
        second=first|{'query_id':'2'*32,'query_artifact':'window-observations/'+('2'*32)+'.json','windows':[]}
        self.store.application_windows(appid,None,first,'confirmed')
        original=self.store._write
        def replaced(*args,**kwargs):
            original(*args,**kwargs)
            raise ContractError('artifact_failed','post replace fsync failure')
        with patch.object(self.store,'_write',side_effect=replaced), self.assertRaises(ContractError):
            self.store.application_windows(appid,first,second,'pending')
        retained=self.store.application_read(appid)
        self.assertEqual(retained['windows'],first['windows'])
        self.assertEqual(retained['window_observation'],first)
        self.assertEqual(retained['pending_observation'],second)
        record=self.store.path/'applications'/appid/'record.json'
        value=json.loads(record.read_text());value['logs']=None
        record.write_text(json.dumps(value))
        with self.assertRaises(ContractError) as caught:
            self.store.application_read(appid)
        self.assertEqual(caught.exception.code,'artifact_failed')

    def test_open_failure_after_exclusive_mkdir_retains_cleanup_ownership(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        original = os.open
        def broken(path, *args, **kwargs):
            if path == query.name:
                raise OSError('injected open failure')
            return original(path, *args, **kwargs)
        with patch.object(windows.os, 'open', side_effect=broken):
            with self.assertRaises(OSError):
                query.step()
        path = self.desktop_root / 'tmp' / query.name
        self.assertTrue(path.is_dir())
        query.cancel('window_query_failed')
        self.clean(query)
        self.assertFalse(path.exists())

    def hung_query(self, now, deadline=110.0, start=None):
        """A query whose KWin never answers, on an injected owner clock; its child is real."""
        self.mode = 'hang'
        owner = OwnerClock(now)
        owner.turn()
        self.desktop.owner_clock = owner  # self.desktop is new for each test.
        query = (start or self.adapter.start)('request', deadline)
        return owner, query

    def advance(self, query, owner, now, until, *, stall=False):
        """Owner turns to `until`: one turn if stalled, else every 5ms; the query steps after each."""
        while now.now < until:
            now.now = until if stall else min(until, now.now + .005)
            owner.turn()
            self.children.poll()
            query.step()

    def expire(self, query):
        with self.assertRaises(ContractError) as caught:
            query.step()
        self.assertEqual(caught.exception.code, 'timeout')
        query.cancel(caught.exception)
        self.clean(query)

    def test_query_work_budget_runs_from_initiation(self):
        now = FakeClock(100.0)
        owner, query = self.hung_query(now)
        while now.now < 100.2:  # Regular owner turns, no stall, but the first step only comes now.
            now.now = min(100.2, now.now + .005)
            owner.turn()
        query.step()
        self.assertEqual(query.deadline, 100.5)  # Not 100.7: the budget began at initiation.
        self.advance(query, owner, now, 100.495)
        now.now = 100.5
        owner.turn()
        self.expire(query)

    def test_owner_stall_before_and_during_the_query_counts_at_most_once_more(self):
        for activation in (False, True):
            with self.subTest(activation=activation):
                now = FakeClock(100.0)
                start = None
                if activation:
                    start = lambda request_id, deadline: self.adapter.activate(
                        request_id, deadline, {'generation': GEN, 'window_id': UID}, lambda: None)
                owner, query = self.hung_query(now, start=start)
                self.advance(query, owner, now, 100.3, stall=True)  # Stalled before the first step.
                self.assertAlmostEqual(query.deadline, 100.79)
                self.advance(query, owner, now, 100.6, stall=True)  # And again during the query.
                self.assertEqual(query.deadline, 101.0)  # Capped at twice the budget from initiation.
                self.advance(query, owner, now, 100.995)
                now.now = 101.0
                owner.turn()
                self.expire(query)

    def test_query_work_budget_never_passes_the_caller_deadline(self):
        now = FakeClock(100.0)
        owner, query = self.hung_query(now, deadline=100.5)  # A `windows` request: 0.5s in all.
        self.advance(query, owner, now, 100.4, stall=True)
        self.assertEqual(query.deadline, 100.5)
        now.now = 100.5
        owner.turn()
        self.expire(query)

    def test_deadline_crossed_during_artifact_publication_keeps_observed_data_unaccepted(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        original = self.store.window_observation
        def late(*args):
            value = original(*args)
            query.budget = Budget(-1)  # Spent during publication.
            return value
        with patch.object(self.store, 'window_observation', side_effect=late):
            with self.assertRaises(ContractError) as caught:
                self.drive(query)
        self.assertEqual(caught.exception.code, 'timeout')
        value = json.loads((self.store.path / 'window-observations' / (query.id + '.json')).read_text())
        self.assertEqual(value['observation_state'], 'observed')
        self.assertIsNone(query.accepted_at)
        query.cancel(caught.exception)
        self.clean(query)

    def test_artifact_failure_after_native_cleanup_does_not_accept_pins(self):
        query = self.adapter.start('request', time.monotonic() + .5)
        with patch.object(self.store, 'window_observation', side_effect=ContractError('artifact_failed', 'disk')):
            with self.assertRaises(ContractError):
                self.drive(query)
        self.assertIsNone(query.accepted_at)
        self.assertEqual(self.adapter.pins, {})
        self.assertFalse(query.folder.exists())
        query.cancel('artifact_failed')
        self.clean(query)


class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.registry = object.__new__(Registry)
        self.registry.generation, self.registry.boot_id = GEN, 'boot'
        self.registry.identity_index, self.registry.identity_revision = {}, 0
        self.registry.store = Mock()
        self.app = Application(self.registry, 'b' * 32, None, None, '/owned')
        self.registry.active = self.app
        self.pid = os.getpid()
        self.key = (self.pid, birth(self.pid))
        self.app.handles[self.key] = os.pidfd_open(self.pid)
        self.registry.index_add(self.app, self.key)
    def tearDown(self):
        self.app.close()
    def test_cutoff_reuse_membership_exit_and_app_change(self):
        bracket = self.registry.begin_window_observation()
        with patch('agent_desktop.app_processes.membership', return_value='/owned/child'):
            self.assertIsNotNone(self.registry.window_identity(bracket, self.pid))
            self.registry.index_add(self.app, self.key)
            self.assertIsNone(self.registry.window_identity(bracket, self.pid))
        bracket = self.registry.begin_window_observation()
        with patch('agent_desktop.app_processes.membership', return_value='/outside'):
            self.assertIsNone(self.registry.window_identity(bracket, self.pid))
        with patch('agent_desktop.app_processes.live', return_value=False):
            self.assertIsNone(self.registry.window_identity(bracket, self.pid))
        self.registry.active = None
        self.assertIsNone(self.registry.window_identity(bracket, self.pid))
    def test_lookup_generation_and_non_generated_id_before_filesystem(self):
        for handle, code in (({'generation':'c'*32,'application_id':'b'*32}, 'generation_mismatch'),
                             ({'generation':GEN,'application_id':'descriptive'}, 'target_not_found')):
            with self.assertRaises(ContractError) as caught:
                self.registry.lookup(handle)
            self.assertEqual(caught.exception.code, code)
        self.registry.store.application_read.assert_not_called()
    def test_capacity_and_reappearance_never_evict_or_rebind(self):
        adapter = windows.Adapter(SimpleNamespace(), GEN, '/binary')
        adapter.registry = self.registry
        query = adapter.start('request', float('inf'))
        query.bracket = self.registry.begin_window_observation()
        with patch('agent_desktop.app_processes.membership', return_value='/owned'):
            for n in range(4096):
                w = Window.parse(row(uuid=str(uuid.UUID(int=n+1)), pid=self.pid), None)
                identity, reason = query._association(w)
                self.assertEqual(reason, 'verified_process')
                adapter.pins.update(query.delta)
                query.delta.clear()
            self.assertEqual(query._association(Window.parse(row(pid=self.pid), None))[1], 'association_capacity')
            old = Window.parse(row(uuid=str(uuid.UUID(int=1)), pid=self.pid), None)
            self.assertEqual(query._association(old)[1], 'verified_process')
            original = dict(adapter.pins)
            adapter.pins[old.uuid] = ('other', 1, 2, 'boot')
            self.assertEqual(query._association(old)[1], 'identity_changed')
            self.assertEqual(len(adapter.pins), 4096)
    def test_app_change_before_final_commit_discards_all_staged_pins(self):
        adapter=windows.Adapter(SimpleNamespace(),GEN,'/binary');adapter.registry=self.registry
        query=adapter.start('request',time.monotonic()+.5)
        query.bracket=self.registry.begin_window_observation()
        query.phase='recheck';query.result={};query.rows=[];query.delta={UID:('b'*32,self.pid,self.key[1],'boot')}
        self.registry.active=None
        with self.assertRaises(ContractError):
            query.step()
        self.assertEqual(adapter.pins,{})
        self.assertIsNone(query.accepted_at)

    def test_late_publication_retains_previous_and_pending(self):
        old = {'windows': [{'generation': GEN, 'window_id': UID}]}
        self.app.window_observation = old
        candidate = {'windows': [], 'query_id': 'c'*32}
        def expired():
            raise ContractError('timeout', 'late')
        with self.assertRaises(ContractError):
            self.registry.publish_windows(self.app.handle, candidate, expired)
        self.assertEqual(self.app.window_observation, old)
        self.assertEqual(self.app.pending_observation['publication'], 'uncertain')
        self.assertEqual(self.app.snapshot()['windows'], old['windows'])


if __name__ == '__main__':
    unittest.main()
