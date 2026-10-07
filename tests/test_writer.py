"""Durable writes on the writer thread (#96): order, failure, backpressure, shutdown,
records before effects, and an owner that keeps running while writes are slow."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from agent_desktop.artifacts import Store
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.records import Records
from agent_desktop.scheduler import SETTLE_GRACE, Scheduler
from agent_desktop.writer import PAUSE_OPS, Journal, Ticket, finished, journal_of

GEN = 'a' * 32
WINDOW = GEN + ':2a63a414-1509-460a-bff9-b7c1103ba8d5'
SRC = Path(__file__).resolve().parents[1] / 'src'


def wait_for(predicate, journal=None, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if journal is not None:
            journal.drain()
        if predicate():
            return True
        time.sleep(.001)
    raise AssertionError('timed out')


class FakeStore:
    """Records calls in the order the writer runs them; GATE holds the writer back."""
    def __init__(self):
        self.calls = []
        self.threads = set()
        self.gate = threading.Event()
        self.gate.set()

    def write(self, name, value=None):
        self.gate.wait(10)
        self.threads.add(threading.get_ident())
        self.calls.append((name, value))
        return name + '-result'

    def fail(self, name):
        self.calls.append((name, None))
        raise ContractError('artifact_failed', 'Injected failure.')


class JournalTests(unittest.TestCase):
    def journal(self, store, **kwargs):
        journal = Journal(store, threaded=True, **kwargs)
        self.addCleanup(lambda: (store.gate.set(), journal.close(time.monotonic() + 2)))
        return journal

    def test_writes_run_in_submission_order_off_the_owner_with_earlier_results(self):
        store = FakeStore()
        journal = self.journal(store)
        tickets = [journal.submit('write', str(index)) for index in range(50)]
        # A Ticket argument stands for that earlier write's result.
        dependent = journal.submit('write', 'dependent', tickets[0])
        wait_for(lambda: dependent.done, journal)
        self.assertEqual([name for name, _ in store.calls[:50]], [str(index) for index in range(50)])
        self.assertEqual(store.calls[-1], ('dependent', '0-result'))
        self.assertNotIn(threading.get_ident(), store.threads)
        self.assertTrue(all(finished(ticket) for ticket in tickets))
        self.assertEqual(tickets[3].result, '3-result')

    def test_arguments_are_copied_when_submitted(self):
        store = FakeStore()
        store.gate.clear()
        journal = self.journal(store)
        value = {'state': 'before'}
        ticket = journal.submit('write', 'record', value)
        value['state'] = 'after'
        store.gate.set()
        wait_for(lambda: ticket.done, journal)
        self.assertEqual(store.calls, [('record', {'state': 'before'})])

    def test_failure_is_handed_back_on_the_owner_and_latches_dependents(self):
        store = FakeStore()
        journal = self.journal(store)
        seen = []
        failed = journal.submit('fail', 'token', on_error=lambda error: seen.append((threading.get_ident(), error.code)))
        dependent = journal.submit('write', 'transition', failed)
        later = journal.submit('write', 'other')
        # Nothing reaches the owner until drain(), which runs on the owner.
        time.sleep(.05)
        self.assertFalse(failed.done)
        self.assertEqual(seen, [])
        wait_for(lambda: later.done, journal)
        self.assertEqual(seen, [(threading.get_ident(), 'artifact_failed')])
        with self.assertRaises(ContractError):
            finished(failed)
        # A write that needs the failed one's result fails too and never runs.
        self.assertEqual(dependent.error.context['phase'], 'dependency')
        self.assertNotIn(('transition', None), store.calls)
        # The writer keeps going for unrelated records.
        self.assertTrue(finished(later))
        late = []
        failed.on_error(lambda error: late.append(error.code))
        self.assertEqual(late, ['artifact_failed'])

    def test_full_queue_fails_at_once_and_pauses_admission_first(self):
        store = FakeStore()
        store.gate.clear()
        journal = self.journal(store, max_ops=PAUSE_OPS + 2)
        tickets = [journal.submit('write', str(index)) for index in range(PAUSE_OPS - 1)]
        self.assertFalse(journal.paused())
        tickets += [journal.submit('write', 'x'), journal.submit('write', 'y'), journal.submit('write', 'z')]
        self.assertTrue(journal.paused())
        before = time.monotonic()
        full = journal.submit('write', 'refused')
        self.assertLess(time.monotonic() - before, .05)
        self.assertTrue(full.done)
        self.assertEqual(full.error.context['phase'], 'queue_full')
        store.gate.set()
        wait_for(lambda: all(ticket.done for ticket in tickets), journal)
        self.assertFalse(journal.paused())
        self.assertNotIn(('refused', None), store.calls)

    def test_byte_bound_counts_large_records(self):
        store = FakeStore()
        store.gate.clear()
        journal = self.journal(store, max_bytes=3 << 20)
        accepted = [journal.submit('write', str(index), size=1 << 20) for index in range(3)]
        refused = journal.submit('write', 'too-much', size=1 << 20)
        self.assertEqual(refused.error.context['phase'], 'queue_full')
        store.gate.set()
        wait_for(lambda: all(ticket.done for ticket in accepted), journal)

    def test_shutdown_drains_within_its_bound_and_reports_a_stuck_writer(self):
        store = FakeStore()
        journal = Journal(store, threaded=True)
        tickets = [journal.submit('write', str(index)) for index in range(20)]
        self.assertTrue(journal.close(time.monotonic() + 2))
        self.assertTrue(all(finished(ticket) for ticket in tickets))
        self.assertEqual(journal.submit('write', 'after').error.context['phase'], 'closed')

        stuck = FakeStore()
        stuck.gate.clear()
        journal = Journal(stuck, threaded=True)
        ticket = journal.submit('write', 'held')
        wait_for(lambda: journal.stalled() > 0)
        before = time.monotonic()
        self.assertFalse(journal.close(time.monotonic() + .2))
        self.assertLess(time.monotonic() - before, .4)
        self.assertFalse(ticket.done)
        stuck.gate.set()
        journal.thread.join(2)
        self.assertFalse(journal.thread.is_alive())

    def test_writes_queued_behind_a_stuck_write_never_run_after_the_close_bound(self):
        stuck = FakeStore()
        stuck.gate.clear()
        journal = Journal(stuck, threaded=True)
        held = journal.submit('write', 'held')
        queued = [journal.submit('write', 'terminal-%d' % index) for index in range(3)]
        wait_for(lambda: journal.stalled() > 0)
        self.assertFalse(journal.close(time.monotonic() + .1))
        time.sleep(.15)   # Past the bound when the stuck write returns.
        stuck.gate.set()
        journal.thread.join(2)
        self.assertFalse(journal.thread.is_alive())
        journal.drain()
        self.assertEqual(stuck.calls, [('held', None)], 'queued writes ran after the shutdown bound')
        self.assertTrue(held.done)
        for ticket in queued:
            self.assertEqual(ticket.error.context['phase'], 'closed')

    def test_final_close_bound_is_the_shutdown_bound(self):
        from agent_desktop.writer import final_deadline
        self.assertEqual(final_deadline(5.0, 10.0), 5.0, 'the final close outlived the shutdown bound')
        self.assertEqual(final_deadline(12.0, 10.0), 12.0)
        self.assertEqual(final_deadline(None, 10.0), 10.5)   # A failed loop has no shutdown bound.

    def test_inline_journal_is_synchronous(self):
        store = FakeStore()
        journal = journal_of(store)
        self.assertIs(journal_of(store), journal)
        ticket = journal.submit('write', 'now')
        self.assertTrue(ticket.done)
        self.assertEqual(store.calls, [('now', None)])
        self.assertEqual(store.threads, {threading.get_ident()})


class AcceptPauseTests(unittest.TestCase):
    def test_ordinary_accepts_wait_for_the_writer_but_never_longer_than_max_pause(self):
        from unittest.mock import Mock, patch
        from agent_desktop import transport
        endpoint = Mock()
        endpoint.listener.accept.side_effect = BlockingIOError
        endpoint.priority_listener.accept.side_effect = BlockingIOError
        busy = [True]
        server = transport.Server(endpoint, Mock(), Mock(), paused=lambda: busy[0])
        now = [100.0]
        with patch.object(transport.time, 'monotonic', lambda: now[0]):
            server.accept(False)
            server.accept(True)  # Priority controls are never paused.
            self.assertEqual(endpoint.listener.accept.call_count, 0)
            self.assertEqual(endpoint.priority_listener.accept.call_count, 1)
            now[0] += transport.MAX_PAUSE - .01
            server.accept(False)
            self.assertEqual(endpoint.listener.accept.call_count, 0)
            now[0] += .02
            server.accept(False)
            self.assertEqual(endpoint.listener.accept.call_count, 1)
            busy[0] = False
            server.accept(False)
            self.assertEqual(endpoint.listener.accept.call_count, 2)


class HeldReplyTests(unittest.TestCase):
    """A reply answered at acceptance (a managed session.status, a stop waiter)
    leaves only once the records written then are durable, as on main, where
    on_terminal wrote them inline before the next send turn."""
    def serve(self, gated):
        from unittest.mock import Mock
        from agent_desktop import transport
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'artifacts', 'default', GEN, create=True)
        self.addCleanup(store.close)
        gate = threading.Event()
        self.addCleanup(gate.set)
        for name in gated:
            original = getattr(store, name)
            def held(*args, original=original, **kwargs):
                gate.wait(10)
                return original(*args, **kwargs)
            setattr(store, name, held)
        journal = store.journal = Journal(store, threaded=True)
        self.addCleanup(lambda: (gate.set(), journal.close(time.monotonic() + 2)))
        records = Records(store)
        endpoint = Mock()
        endpoint.name, endpoint.generation = 'default', GEN
        endpoint.listener.accept.side_effect = BlockingIOError
        endpoint.priority_listener.accept.side_effect = BlockingIOError
        glib = Mock(IO_IN=1, IO_OUT=4, IO_HUP=16, IO_ERR=8)
        server = transport.Server(endpoint, glib, Mock())
        ours, peer = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(peer.close)
        ours.setblocking(False)
        peer.settimeout(0)
        connection = transport.Connection(server, ours)
        server.connections.add(connection)
        server.order[False].append(connection)
        request = make_request('session.status', arguments={}, caller_cwd='/tmp',
                               expected_generation=GEN, timeout_seconds=3)
        admission = connection.admission = transport.Admission(connection, request)
        records.attach(request, admission)
        admission.complete(result={'state': 'ready'})   # As dispatch_request does.

        def received():
            journal.drain()
            server.service()
            try:
                return peer.recv(65536)
            except BlockingIOError:
                return b''
        return gate, server, received

    def assert_held_until_durable(self, gated):
        gate, server, received = self.serve(gated)
        for _ in range(20):
            self.assertEqual(received(), b'', 'the reply left before its records were durable')
            time.sleep(.005)
        self.assertTrue(server.flushing(), 'the worker could quit with the reply unsent')
        gate.set()
        data = []
        wait_for(lambda: data.append(received()) or any(data))
        self.assertIn(b'"ok":true', b''.join(data).replace(b' ', b''))
        self.assertFalse(server.flushing())

    def test_a_hold_is_bounded_and_resets_the_frame_budget_on_release(self):
        from unittest.mock import Mock, patch
        from agent_desktop import transport
        now = [100.0]
        with patch.object(transport.time, 'monotonic', lambda: now[0]), \
                socket.socket(socket.AF_UNIX) as sock:
            server = Mock(connections=set())
            connection = transport.Connection(server, sock)
            connection.admission = Mock(holds=[Ticket(1, 'transition', 0)])
            connection.output = b'reply'
            self.assertFalse(connection.releasable(now[0]))
            now[0] += transport.FRAME_SECONDS + 1
            self.assertTrue(connection.holding(), 'Server.expire would cut a held reply at the frame deadline')
            connection.admission.holds[0].done = True
            self.assertTrue(connection.releasable(now[0]))
            self.assertEqual(connection.deadline, now[0] + transport.FRAME_SECONDS)
            connection.admission.holds.append(Ticket(2, 'transition', 0))
            self.assertFalse(connection.releasable(now[0]))
            now[0] += transport.HOLD_SECONDS
            self.assertFalse(connection.releasable(now[0]))
            self.assertTrue(connection.closed, 'a stuck record held the reply without bound')

    def test_reply_waits_for_its_request_record(self):
        self.assert_held_until_durable(['request'])

    def test_reply_waits_for_its_terminal_record(self):
        self.assert_held_until_durable(['transition'])


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class Admission:
    def __init__(self, clock, operation='type', timeout=3):
        arguments = {} if operation.startswith('session.') else {'window': WINDOW, 'text': 'hi'}
        self.request = make_request(operation, arguments=arguments, caller_cwd='/tmp',
                                    expected_generation=GEN, timeout_seconds=timeout)
        self.admitted_at = clock()
        self.deadline = clock() + timeout
        self.on_disconnect = self.on_terminal = None
        self.disconnected = False
        self.results = []

    def complete(self, **result):
        self.results.append(result)


def ticket(done=False, error=None):
    value = Ticket(0, 'test', 0)
    value.done, value.error = done, error
    return value


class GatedTask:
    """Records an intent, waits for it with Context.recorded(), then 'emits'."""
    cleanup_seconds = .1

    def __init__(self, context, events):
        self.context, self.events = context, events
        self.phase = 'start'

    def step(self, now):
        self.events.append('step')
        if self.phase == 'start':
            self.context.effects({'intent': True}, uncertain=True)
            self.phase = 'intent'
        if not self.context.recorded():
            return None
        self.events.append('emit')
        return {'emitted': True}

    def request_cancel(self, reason):
        self.events.append('release')

    def cleanup(self, now):
        self.events.append('cleanup')
        return True


class SchedulerRecordTests(unittest.TestCase):
    """The scheduler's waits, with tickets the test finishes by hand."""
    def setUp(self):
        self.clock, self.events, self.records = Clock(), [], []

        def observer(record):
            value = ticket()
            self.records.append((record['event'], value))
            return value
        self.owner = Scheduler(clock=self.clock, observer=observer, capabilities={'type', 'session.stop'},
                               factory=lambda request, context: GatedTask(context, self.events))

    def finish(self, event=None, error=None):
        for name, value in self.records:
            if event is None or name == event:
                value.done, value.error = True, error

    def test_first_step_effect_and_response_each_wait_for_their_records(self):
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.owner.tick()
        self.assertEqual(self.events, [], 'stepped before the admission record was durable')
        self.finish()
        self.owner.tick()
        self.assertEqual(self.events, ['step'])
        self.owner.tick()
        self.assertNotIn('emit', self.events, 'emitted before the intent was durable')
        self.finish('effects')
        self.owner.tick()
        self.assertIn('emit', self.events)
        self.assertEqual(admission.results, [], 'answered before finalizing was durable')
        self.finish('finalizing')
        self.owner.tick()
        self.assertEqual(admission.results[0]['result'], {'emitted': True})
        self.assertIsNone(self.owner.active)

    def test_a_self_gating_task_steps_early_but_still_emits_only_after_every_record(self):
        class Early(GatedTask):
            gates_effects = True
        self.owner.factory = lambda request, context: Early(context, self.events)
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.assertEqual(self.events, ['step'], 'a self-gating task may observe while start records are queued')
        self.finish('effects')
        self.owner.tick()
        self.assertNotIn('emit', self.events, 'emitted before the admission record was durable')
        self.finish()
        self.owner.tick()
        self.assertIn('emit', self.events)

    def test_failed_record_latches_artifact_failed_and_never_emits(self):
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        self.finish('effects', ContractError('artifact_failed', 'Injected.'))
        self.owner.tick()
        self.assertEqual(self.events, ['step', 'release', 'cleanup'])
        self.finish()
        self.owner.tick()
        self.assertNotIn('emit', self.events)
        self.assertEqual(admission.results[0]['error'].code, 'artifact_failed')

    def test_cancel_and_cleanup_never_wait_for_records(self):
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        # The intent record is still queued: release and cleanup do not wait for it.
        self.assertTrue(self.owner.cancel(admission.request.request_id))
        self.assertEqual(self.events[-1], 'release')
        self.owner.tick()
        self.assertEqual(self.events[-1], 'cleanup')
        # The response does wait for its records.
        self.assertEqual(admission.results, [])
        self.finish()
        self.owner.tick()
        self.assertEqual(admission.results[0]['error'].code, 'cancelled')

    def test_records_still_pending_after_the_bound_never_answer_success(self):
        admission = Admission(self.clock, timeout=1)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        self.finish('effects')
        self.owner.tick()
        self.assertEqual(admission.results, [])
        self.clock.now = 1 + SETTLE_GRACE - .1
        self.owner.tick()
        self.assertEqual(admission.results, [])
        self.clock.now = 1 + SETTLE_GRACE + .2
        self.owner.tick()
        error = admission.results[0]['error']
        self.assertEqual(error.code, 'artifact_failed', 'a storage stall past the bound was reported as a timeout')
        self.assertTrue(error.partial_result['emitted'])

    def test_an_earlier_error_keeps_precedence_over_unsettled_records_as_on_main(self):
        admission = Admission(self.clock, timeout=1)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        self.owner.cancel(admission.request.request_id)
        self.owner.tick()
        self.clock.now = 1 + SETTLE_GRACE + .2   # Its cancelling and finalizing records never settle.
        self.owner.tick()
        self.assertEqual(admission.results[0]['error'].code, 'cancelled')

    def test_finished_cleanup_releases_shutdown_while_its_records_are_pending(self):
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        self.owner.begin_shutdown(self.clock() + 5)
        self.assertEqual(self.events[-1], 'release')
        self.owner.tick()
        self.assertEqual(self.events[-1], 'cleanup')
        # Cleanup is complete; the cancelling and finalizing records are still queued.
        self.assertEqual(admission.results, [])
        self.assertTrue(self.owner.drain_shutdown(self.clock() + .5),
                        'release waited for the writer after cleanup had finished')
        self.finish()
        self.owner.tick()
        self.assertEqual(admission.results[0]['error'].code, 'cancelled')

    def test_stop_steps_at_once_even_behind_pending_records(self):
        admission = Admission(self.clock)
        self.owner.submit(admission.request, admission)
        self.owner.tick()
        self.finish()
        self.owner.tick()
        steps = self.events.count('step')
        stop = Admission(self.clock, operation='session.stop')
        self.owner.submit(stop.request, stop)   # Cancels the active request.
        self.owner.tick()
        self.owner.tick()
        # The cancelled request's records and the stop's control_admitted are all queued.
        self.assertTrue(any(not value.done for _, value in self.records))
        self.assertGreater(self.events.count('step'), steps, 'stop waited for the writer before its first step')


class LifecycleWaiterTests(unittest.TestCase):
    def test_joined_stop_waiters_answer_with_the_owners_settled_outcome(self):
        clock, records = Clock(), []
        def observer(record):
            value = ticket()
            records.append((record['request_id'], record['event'], value))
            return value
        class Stop:
            cleanup_seconds = 0
            def __init__(self, request, context):
                pass
            def step(self, now):
                return {'state': 'stopping'}
            def request_cancel(self, reason):
                pass
            def cleanup(self, now):
                return True
        owner = Scheduler(clock=clock, observer=observer, capabilities={'session.stop'},
                          factory=lambda request, context: Stop(request, context))
        first, joined = Admission(clock, operation='session.stop'), Admission(clock, operation='session.stop')
        owner.submit(first.request, first)
        owner.submit(joined.request, joined)
        owner.tick()
        self.assertIsNone(owner.lifecycle, 'the mutation slot waited for the owner\'s records')
        self.assertEqual(joined.results, [], 'the waiter answered before the owner\'s records settled')
        # The owner's finalizing write fails; every other record is durable.
        for request_id, event, value in records:
            failed = request_id == first.request.request_id and event == 'finalizing'
            value.done, value.error = True, ContractError('artifact_failed', 'Injected.') if failed else None
        owner.tick()
        for request_id, event, value in records:
            value.done = True   # The waiter's own finalizing record.
        owner.tick()
        self.assertEqual(first.results[0]['error'].code, 'artifact_failed')
        self.assertIsNotNone(joined.results[0]['error'], 'the waiter reported success for a failed stop')
        self.assertEqual(joined.results[0]['error'].code, 'artifact_failed')


class HeldBackWriterTests(unittest.TestCase):
    """The real Store, Records and Scheduler with a writer the test holds back."""
    def test_no_effect_starts_before_its_record_is_durable(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'artifacts', 'default', GEN, create=True)
        self.addCleanup(store.close)
        gate = threading.Event()
        durable = []
        transition = store.transition

        def held(token, phase, **kwargs):
            if phase == 'effects':
                gate.wait(10)
            value = transition(token, phase, **kwargs)
            durable.append(phase)
            return value
        store.transition = held
        journal = store.journal = Journal(store, threaded=True)
        self.addCleanup(lambda: (gate.set(), journal.close(time.monotonic() + 2)))
        records, events = Records(store), []
        owner = Scheduler(observer=records.observe, capabilities={'type', 'session.stop'},
                          factory=records.factory(lambda request, context: GatedTask(context, events)))
        admission = Admission(time.monotonic, timeout=10)
        records.attach(admission.request, admission)
        owner.submit(admission.request, admission)

        def turn():
            journal.drain()
            owner.tick()
        wait_for(lambda: (turn(), 'step' in events)[1])
        self.assertEqual(durable[:1], ['admitted'])
        self.assertIn('started', durable)
        for _ in range(20):
            turn()
            time.sleep(.005)
        self.assertNotIn('emit', events)
        self.assertNotIn('effects', durable)
        gate.set()
        wait_for(lambda: (turn(), bool(admission.results))[1])
        self.assertEqual(events.count('emit'), 1)
        self.assertLess(durable.index('effects'), durable.index('finalizing'))
        self.assertEqual(admission.results[0]['result'], {'emitted': True})


FIXTURE = r'''
import os, sys, threading, time
from pathlib import Path
from agent_desktop import artifacts
GATE, MARKERS = Path(sys.argv[3]), Path(sys.argv[4])
os.environ['NOTIFY_SOCKET'] = sys.argv[5]
os.environ['WATCHDOG_USEC'] = '5000000'
os.environ['WATCHDOG_PID'] = str(os.getpid())
original = artifacts.Store.transition
def transition(self, token, phase, **kwargs):
    while phase == 'effects' and GATE.exists():
        time.sleep(.01)  # A slow disk: the writer thread is inside this write.
    return original(self, token, phase, **kwargs)
artifacts.Store.transition = transition
def mark(event):
    with MARKERS.open('a') as stream:
        stream.write(event + ' ' + repr(time.monotonic()) + '\n')
class Task:
    cleanup_seconds = .1
    def __init__(self, request, context):
        self.context, self.first = context, True
    def step(self, now):
        if self.first:
            self.first = False
            mark('effect')
            self.context.effects({'intent': True}, uncertain=True)
        if not self.context.recorded():
            return None
        mark('emit')
        return {'desktop_ready': False}
    def request_cancel(self, code):
        pass
    def cleanup(self, now):
        return True
from agent_desktop.worker import run
run('default', sys.argv[1], artifacts=sys.argv[2], factory=Task, capabilities={'session.stop'})
'''


class SlowWriteProcessTests(unittest.TestCase):
    """A real worker: the owner keeps its watchdog heartbeat while a write is slow."""
    def test_watchdog_heartbeat_continues_while_a_write_is_slow(self):
        temp = tempfile.TemporaryDirectory(prefix='adw-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ('runtime', 'caller', 'worker'):
            (root / name).mkdir(mode=0o700)
        (root / 'fixture.py').write_text(FIXTURE)
        gate, markers = root / 'gate', root / 'markers'
        gate.touch()
        notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(notify.close)
        notify.bind(str(root / 'notify'))
        notify.settimeout(.05)
        env = os.environ | {'XDG_RUNTIME_DIR': str(root / 'runtime'), 'PYTHONPATH': str(SRC),
                            'PYTHONWARNINGS': 'ignore'}
        err = (root / 'worker.err').open('w')
        self.addCleanup(err.close)
        worker = subprocess.Popen([sys.executable, str(root / 'fixture.py'), GEN, str(root / 'artifacts'),
                                   str(gate), str(markers), str(root / 'notify')],
                                  cwd=root / 'worker', env=env, stdout=subprocess.DEVNULL, stderr=err)

        def stop():
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=5)
        self.addCleanup(stop)

        def client(*args):
            return subprocess.Popen([sys.executable, '-m', 'agent_desktop', '--json', *args],
                                    cwd=root / 'caller', env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)

        def lines():
            return markers.read_text().split('\n') if markers.exists() else []
        wait_for(lambda: (root / 'runtime/agent-desktop/current/default.json').exists(), seconds=5)
        active = client('type', '--window', WINDOW, 'hello')
        self.addCleanup(lambda: active.poll() is None and active.kill())
        wait_for(lambda: any(line.startswith('effect') for line in lines()), seconds=5)
        # The writer is now held inside the effects write for 2.5s.
        beats, end = [], time.monotonic() + 2.5
        while time.monotonic() < end:
            try:
                if notify.recv(64) == b'WATCHDOG=1':
                    beats.append(time.monotonic())
            except TimeoutError:
                pass
            self.assertIsNone(worker.poll(), (root / 'worker.err').read_text())
        self.assertFalse(any(line.startswith('emit') for line in lines()), 'emitted before its intent was durable')
        self.assertGreaterEqual(len(beats), 2)
        self.assertTrue(all(b - a < 1.5 for a, b in zip(beats, beats[1:])), beats)
        gate.unlink()
        out, _ = active.communicate(timeout=5)
        self.assertTrue(json.loads(out)['ok'], out)
        self.assertTrue(any(line.startswith('emit') for line in lines()))


if __name__ == '__main__':
    unittest.main()
