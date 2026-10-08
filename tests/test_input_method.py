"""Input-method `type`: Wayland wire frames, the client's state, confirmation, routing and limits."""
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop import cli, input_method as im, mcp_tools
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.input_actions import ACTIVATION_WAIT, TypeTask
from agent_desktop.keymap import MAX_COMMIT_BYTES, commit_text, route, text_strokes, typeable
from agent_desktop.protocol import request_from_wire

GEN = 'a' * 32
WINDOW = {'generation': GEN, 'window_id': '2382f322-3657-4566-8870-33e1237ab765'}
CONTEXT = 0xff000000

# Frames recorded from KWin 6.7.5 (private session) to this client, in order.
GLOBAL_IM = bytes.fromhex('020000000000280025000000140000007a77705f696e7075745f6d6574686f645f76310001000000')
GLOBAL_SEAT = bytes.fromhex('0200000000001c000b00000008000000776c5f73656174000a000000')
DONE_3 = bytes.fromhex('0300000000000c0021000000')
DELETE_3 = bytes.fromhex('0100000001000c0003000000')
ACTIVATE = bytes.fromhex('0400000000000c00000000ff')
DONE_5 = bytes.fromhex('0500000000000c0021000000')
DELETE_5 = bytes.fromhex('0100000001000c0005000000')
SURROUNDING_A = bytes.fromhex('000000ff0000180002000000610000000100000001000000')        # 'a', cursor 1, anchor 1
SURROUNDING_AB = bytes.fromhex('000000ff0000180003000000616200000200000002000000')       # 'ab', 2, 2
SURROUNDING_AB_E_CHECK = bytes.fromhex('000000ff00001c00080000006162c3a9e29c93000700000007000000')  # 'abé✓', 7, 7
COMMIT_STATE_2 = bytes.fromhex('000000ff04000c0002000000')
COMMIT_STATE_0x13 = bytes.fromhex('000000ff04000c0013000000')
CONTENT_TYPE = bytes.fromhex('000000ff020010000100000000000000')
RESET = bytes.fromhex('000000ff01000800')
DEACTIVATE = struct.pack('<III', 4, 12 << 16 | 1, CONTEXT)


class WireTests(unittest.TestCase):
    def test_requests_match_the_wire_format(self):
        self.assertEqual(im.encode(1, 1, ('n', 2)).hex(), '0100000001000c0002000000')  # get_registry
        self.assertEqual(im.encode(1, 0, ('n', 3)).hex(), '0100000000000c0003000000')  # sync
        bind = im.encode(2, 0, ('u', 0x25), ('s', b'zwp_input_method_v1'), ('u', 1), ('n', 4))
        self.assertEqual(bind.hex(), '0200000000002c0025000000140000007a77705f696e7075745f6d6574686f645f7631000100000004000000')
        # commit_string(serial 0x13, 'é✓') to the server-created context.
        self.assertEqual(im.commit_message(CONTEXT, 0x13, 'é✓'.encode()).hex(),
                         '000000ff01001800130000000600000' '0c3a9e29c9300' '0000')
        self.assertEqual(im.encode(CONTEXT, 0).hex(), '000000ff00000800')  # context destroy

    def test_recorded_events_decode(self):
        messages, consumed = im.frames(GLOBAL_IM + ACTIVATE + SURROUNDING_AB_E_CHECK + COMMIT_STATE_0x13
                                       + CONTENT_TYPE + RESET)
        self.assertEqual(consumed, len(GLOBAL_IM + ACTIVATE + SURROUNDING_AB_E_CHECK + COMMIT_STATE_0x13
                                       + CONTENT_TYPE + RESET))
        (o1, op1, p1), (o2, op2, p2), (o3, op3, p3), (o4, op4, p4), (o5, op5, p5), (o6, op6, p6) = messages
        self.assertEqual((o1, op1, im.decode('usu', p1)), (2, 0, [0x25, b'zwp_input_method_v1', 1]))
        self.assertEqual((o2, op2, im.decode('n', p2)), (4, 0, [CONTEXT]))
        self.assertEqual((o3, op3, im.decode('suu', p3)), (CONTEXT, 0, ['abé✓'.encode(), 7, 7]))
        self.assertEqual((o4, op4, im.decode('u', p4)), (CONTEXT, 4, [0x13]))
        self.assertEqual((op5, im.decode('uu', p5)), (2, [1, 0]))
        self.assertEqual((op6, im.decode('', p6)), (1, []))

    def test_partial_frames_wait_and_bad_frames_are_refused(self):
        data = SURROUNDING_AB + COMMIT_STATE_2
        for cut in range(len(data)):
            messages, consumed = im.frames(data[:cut])
            self.assertEqual(consumed, sum(8 + len(p) for _, _, p in messages))
            self.assertLessEqual(len(messages), 1 if cut < len(data) else 2)
        for bad in (struct.pack('<II', 1, 4 << 16), struct.pack('<II', 1, 10 << 16) + b'\0\0'):
            with self.assertRaises(im.ProtocolError):
                im.frames(bad)
        for signature, payload in (('u', b''), ('s', struct.pack('<I', 8) + b'abc\0'),
                                   ('s', struct.pack('<I', 4) + b'abcd'), ('u', b'\0' * 8)):
            with self.subTest(payload=payload), self.assertRaises(im.ProtocolError):
                im.decode(signature, payload)

    def test_commit_limit_fits_one_wayland_message(self):
        self.assertEqual(len(im.commit_message(CONTEXT, 0, b'x' * MAX_COMMIT_BYTES)), 4020)
        with self.assertRaises(ValueError):
            im.encode(CONTEXT, 1, ('u', 0), ('s', b'x' * 4100))


def snap(text, cursor=None, anchor=None, epoch=1):
    data = text.encode()
    cursor = len(data) if cursor is None else cursor
    return im.Snapshot(data, cursor, cursor if anchor is None else anchor, epoch)


class VerdictTests(unittest.TestCase):
    def check(self, before, afters, committed, expected, **kwargs):
        self.assertEqual(im.verdict(before, afters, committed.encode(), changed=kwargs.get('changed', False)),
                         expected)

    def test_exact_replacement_confirms(self):
        self.check(snap('ab'), [snap('abé✓')], 'é✓', (True, None))
        self.check(snap(''), [snap('x'), snap('xé')], 'xé', (True, None))  # Any later snapshot may match.

    def test_selection_and_cursor_use_byte_offsets(self):
        before = snap('中文 word', cursor=7, anchor=11)  # 'word' selected, after two 3-byte characters and a space.
        self.check(before, [snap('中文 🎉', cursor=11)], '🎉', (True, None))
        before = snap('中文 word', cursor=11, anchor=7)  # Same selection, made backwards.
        self.check(before, [snap('中文 🎉', cursor=11)], '🎉', (True, None))
        self.check(snap('héllo', cursor=3), [snap('hé✓llo', cursor=6)], '✓', (True, None))
        # Right text, cursor elsewhere, or a selection left behind: not confirmed.
        self.check(snap('héllo', cursor=3), [snap('hé✓llo', cursor=9)], '✓', (False, 'mismatch'))
        self.check(snap('ab'), [snap('abé', cursor=4, anchor=2)], 'é', (False, 'mismatch'))
        # Offsets inside a character cannot be trusted.
        self.check(snap('é', cursor=1), [snap('éx')], 'x', (False, 'mismatch'))

    def test_field_already_ending_with_the_text_without_a_fresh_snapshot_is_not_confirmed(self):
        self.check(None, [snap('note: é✓')], 'é✓', (False, 'no_fresh_snapshot'))
        # Text that only looks right after the fact proves nothing either.
        self.check(snap('note: é✓'), [snap('note: é✓')], 'é✓', (False, 'mismatch'))

    def test_context_change_and_unrelated_edits(self):
        self.check(snap('ab'), [snap('abé')], 'é', (True, None), changed=True)  # A match before the change stands.
        self.check(snap('ab'), [], 'é', (False, 'context_changed'), changed=True)
        self.check(snap('ab'), [snap('abXé')], 'é', (False, 'mismatch'))
        self.check(snap('ab'), [], 'é', (False, 'timeout'))
        self.assertEqual(im.verdict(snap('ab'), [], b'x', changed=False, lost=True), (False, 'context_changed'))

    def test_empty_surrounding_text_is_reported_as_such(self):
        self.check(snap(''), [snap(''), snap('')], 'é', (False, 'no_surrounding_text'))

    def test_snapshots_near_the_cap_are_truncated(self):
        long = 'x' * 3950
        self.check(snap(long), [snap(long[100:] + 'é')], 'é', (False, 'truncated'))
        self.check(snap(long), [snap(long + 'é')], 'é', (False, 'truncated'))  # Even a match.
        self.check(snap('ab'), [snap('ab' + 'é' * 1999)], 'é' * 1999, (False, 'truncated'))
        self.check(snap('a' * 3000), [snap('b')], 'é' * 500, (False, 'truncated'))


class FakeGLib:
    PRIORITY_DEFAULT = 0
    IO_IN, IO_OUT, IO_ERR, IO_HUP = 1, 4, 8, 16

    def __init__(self):
        self.watches = {}
        self.next = 1

    def io_add_watch(self, fd, priority, condition, callback):
        self.next += 1
        self.watches[self.next] = (fd, condition, callback)
        return self.next

    def source_remove(self, source):
        del self.watches[source]


class ClientTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, 'wayland-private')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(self.path)
        self.server.listen(1)
        self.glib = FakeGLib()
        self.client = im.InputMethod(self.path, self.glib)
        self.addCleanup(self.client.close)
        self.client.start(time.monotonic() + 5)
        self.conn, _ = self.server.accept()
        self.addCleanup(self.conn.close)
        self.conn.settimeout(2)

    def read(self, size):
        data = b''
        while len(data) < size:
            chunk = self.conn.recv(size - len(data))
            if not chunk:
                self.fail('client closed the connection')
            data += chunk
        return data

    def feed(self, *frames):
        self.conn.sendall(b''.join(frames))
        time.sleep(.01)
        self.client.pump()

    def ready(self):
        self.assertEqual(self.read(24).hex(), '0100000001000c0002000000' '0100000000000c0003000000')
        self.feed(GLOBAL_SEAT, GLOBAL_IM, DONE_3, DELETE_3)
        self.assertEqual(self.read(44 + 12)[44:].hex(), '0100000000000c0005000000')
        self.assertEqual(self.client.state, 'binding')
        self.feed(ACTIVATE, DONE_5, DELETE_5)
        self.assertEqual(self.client.state, 'ready')
        return self.client

    def test_binds_the_advertised_global_and_tracks_the_context(self):
        client = self.ready()
        self.assertEqual(client.health(), {'state': 'passed', 'advertised': True})
        self.assertTrue(client.active())
        self.assertIsNone(client.snapshot)
        self.feed(SURROUNDING_A, COMMIT_STATE_2, SURROUNDING_A, CONTENT_TYPE, COMMIT_STATE_0x13)
        self.assertEqual((client.snapshot.text, client.snapshot.cursor, client.serial), (b'a', 1, 0x13))
        epoch = client.epoch
        self.feed(DEACTIVATE)
        self.assertEqual(self.read(8).hex(), '000000ff00000800')  # The client destroys the deactivated context.
        self.assertFalse(client.active())
        self.assertIsNone(client.snapshot)
        self.assertGreater(client.epoch, epoch)
        self.feed(SURROUNDING_AB)  # Late events for the destroyed context are skipped.
        self.assertEqual(client.state, 'ready')

    def test_commit_sends_one_message_and_observes_the_result(self):
        client = self.ready()
        self.feed(SURROUNDING_AB, COMMIT_STATE_0x13)
        commit = client.commit('é✓'.encode())
        self.assertTrue(commit.sent and commit.flushed)
        self.assertEqual(self.read(24), im.commit_message(CONTEXT, 0x13, 'é✓'.encode()))
        self.assertFalse(commit.settled())
        self.feed(SURROUNDING_AB_E_CHECK)
        self.assertTrue(commit.settled())
        self.assertEqual(commit.verdict(), (True, None))

    def test_a_report_after_the_confirmation_bound_does_not_confirm(self):
        client = self.ready()
        self.feed(SURROUNDING_AB)
        commit = client.commit('é✓'.encode())
        self.read(24)
        late = commit.until + .01
        client.clock = lambda: late
        self.feed(SURROUNDING_AB_E_CHECK)
        self.assertFalse(commit.settled())
        self.assertEqual(commit.verdict(), (False, 'timeout'))

    def test_an_overlapping_activation_retires_the_previous_context(self):
        client = self.ready()
        for index in range(1, 101):
            self.feed(struct.pack('<III', 4, 12 << 16, CONTEXT + index))
            # The replaced context is destroyed, as after a deactivate.
            self.assertEqual(self.read(8), im.encode(CONTEXT + index - 1, 0))
        self.assertEqual(client.context, CONTEXT + 100)
        self.assertEqual(sum(kind == 'context' for kind in client.objects.values()), 1)
        self.assertLessEqual(len(client.objects), 8)
        # A late deactivate for a retired context is ignored.
        self.feed(DEACTIVATE)
        self.assertEqual((client.state, client.context), ('ready', CONTEXT + 100))

    def test_a_context_change_after_the_commit_settles_it_unconfirmed(self):
        client = self.ready()
        self.feed(SURROUNDING_AB)
        commit = client.commit(b'x')
        self.feed(DEACTIVATE)
        self.assertTrue(commit.settled())
        self.assertEqual(commit.verdict(), (False, 'context_changed'))
        with self.assertRaises(RuntimeError):
            client.commit(b'x')

    def test_missing_global_is_unavailable_and_never_ready(self):
        self.read(24)
        self.feed(GLOBAL_SEAT, DONE_3)
        self.assertEqual(self.client.state, 'unavailable')
        self.assertEqual(self.client.health(), {'state': 'unavailable', 'advertised': False,
                                                'reason': 'not_advertised'})
        self.assertEqual(self.glib.watches, {})

    def test_protocol_violations_and_disconnects_fail_the_connection(self):
        for frames, reason in (([struct.pack('<II', 99, 8 << 16)], 'protocol'),
                               ([struct.pack('<II', 2, 12 << 16 | 7) + b'\0' * 4], 'protocol'),
                               ([struct.pack('<IIIII', 1, 24 << 16, 1, 0, 2) + b'x\0\0\0'], 'protocol_error'),
                               ([], 'disconnected')):
            with self.subTest(reason=reason):
                self.setUp()
                self.read(24)
                if frames:
                    self.feed(*frames)
                else:
                    self.conn.close()
                    time.sleep(.01)
                    self.client.pump()
                self.assertEqual((self.client.state, self.client.reason), ('failed', reason))
                self.assertIsNone(self.client.sock)

    def test_setup_expires(self):
        self.client.tick(time.monotonic() + 10)
        self.assertEqual((self.client.state, self.client.reason), ('failed', 'timeout'))

    def test_unwritten_commit_is_recalled_on_cancel_and_partly_written_one_closes(self):
        client = self.ready()
        self.feed(SURROUNDING_AB)
        with patch.object(client, '_flush'):
            commit = client.commit(b'x')
        self.assertFalse(commit.sent)
        client.abandon(commit)
        self.assertTrue(commit.recalled)
        self.assertEqual(client.outgoing, b'')
        self.assertEqual(client.state, 'ready')
        commit = client.commit(b'y')
        self.assertEqual(self.read(20), im.commit_message(CONTEXT, 0, b'y'))
        # A message only partly written cannot be recalled: the connection closes.
        with patch.object(client, '_flush'):
            commit = client.commit(b'z' * 40)
        client.written += 4
        client.abandon(commit)
        self.assertEqual((client.state, client.reason), ('failed', 'abandoned'))


    def jam(self, client):
        """Make the client's socket report EAGAIN (or, with error set, fail) on send."""
        jammed = Jammed(client.sock)
        client.sock = jammed
        return jammed

    def test_a_queued_commit_is_recalled_when_its_field_is_deactivated(self):
        client = self.ready()
        self.feed(SURROUNDING_AB)
        jammed = self.jam(client)
        commit = client.commit('é'.encode())
        self.assertFalse(commit.sent)
        self.feed(DEACTIVATE)
        self.assertTrue(commit.recalled and commit.changed)
        self.assertFalse(commit.sent)
        jammed.blocked = False
        client._flush()
        # Only the context's destroy goes out, never the commit.
        self.assertEqual(self.read(8), im.encode(CONTEXT, 0))
        self.conn.settimeout(.1)
        self.assertRaises(TimeoutError, self.conn.recv, 1)
        self.assertEqual(client.state, 'ready')

    def test_a_queued_commit_is_recalled_when_another_field_activates(self):
        client = self.ready()
        jammed = self.jam(client)
        commit = client.commit('é'.encode())
        self.feed(struct.pack('<III', 4, 12 << 16, CONTEXT + 1))
        self.assertTrue(commit.recalled)
        jammed.blocked = False
        client._flush()
        self.assertEqual(self.read(8), im.encode(CONTEXT, 0))
        self.conn.settimeout(.1)
        self.assertRaises(TimeoutError, self.conn.recv, 1)

    def test_a_partly_written_commit_closes_the_connection_when_its_field_changes(self):
        client = self.ready()
        self.jam(client)
        commit = client.commit(b'z' * 40)
        client.written += 4
        self.feed(DEACTIVATE)
        self.assertEqual((client.state, client.reason), ('failed', 'context_changed'))
        self.assertTrue(commit.lost and commit.sent)

    def test_a_failed_write_during_an_overlapping_activation_tears_down_cleanly(self):
        client = self.ready()
        self.jam(client).error = True
        self.feed(struct.pack('<III', 4, 12 << 16, CONTEXT + 1), DONE_5)
        self.assertEqual((client.state, client.reason, client.context), ('failed', 'disconnected', None))
        self.assertEqual(client.outgoing, b'')


class Jammed:
    def __init__(self, sock):
        self.sock, self.blocked, self.error = sock, True, False

    def send(self, data, flags):
        if self.error:
            raise BrokenPipeError()
        if self.blocked:
            raise BlockingIOError()
        return self.sock.send(data, flags)

    def __getattr__(self, name):
        return getattr(self.sock, name)


class RoutingTests(unittest.TestCase):
    def test_typeable_matches_the_key_map_exactly(self):
        for code in range(0, 0x3000):
            char = chr(code)
            try:
                text_strokes(char)
                strokes = True
            except ContractError:
                strokes = False
            self.assertEqual(typeable(char), strokes, hex(code))

    def test_us_text_keeps_the_key_path_byte_for_byte(self):
        printable = ''.join(chr(c) for c in range(32, 127)) + '\n\t'
        self.assertEqual(route(printable), 'keys')
        self.assertEqual(route(''), 'keys')
        self.assertEqual(route('ok café'), 'input_method')
        self.assertEqual(route('ok', 'input-method'), 'input_method')
        self.assertEqual(route('café', 'keys'), 'keys')

    def test_limit_is_4000_utf8_bytes_and_applies_only_to_commits(self):
        def request(text, method=None):
            arguments = {'window': WINDOW, 'text': text}
            if method:
                arguments['method'] = method
            return make_request('type', arguments=arguments, caller_cwd='/')
        self.assertEqual(request('é' * 2000).arguments['method'], 'auto')
        with self.assertRaises(ContractError) as caught:
            request('é' * 2000 + 'x')
        self.assertEqual((caught.exception.code, caught.exception.context),
                         ('invalid_arguments', {'field': 'text', 'reason': 'text_too_long', 'bytes': 4001,
                                                'limit_bytes': 4000}))
        request('x' * 5000)  # Keys have no such limit.
        with self.assertRaises(ContractError):
            request('x' * 4001, 'input-method')
        request('é' * 5000, 'keys')  # Refused later, as unsupported_input, exactly as before.
        for method in ('bogus', 'input_method', 3):
            with self.subTest(method=method), self.assertRaises(ContractError) as caught:
                request('x', method)
            self.assertEqual(caught.exception.context, {'field': 'method'})
        with self.assertRaises(ContractError) as caught:
            request('', 'input-method')
        self.assertEqual(caught.exception.context['reason'], 'empty_text')

    def test_lone_surrogates_are_not_committed(self):
        with self.assertRaises(ContractError) as caught:
            commit_text('a\udcff')
        self.assertEqual(caught.exception.context, {'index': 1, 'codepoint': 'U+DCFF'})

    def test_cli_mcp_and_wire_carry_the_method(self):
        parsed = cli.parser().parse_args(['type', '--window', 'w', '--method', 'input-method', 'é'])
        self.assertEqual(parsed.method, 'input-method')
        schema = mcp_tools.input_schema('type')['properties']['method']
        self.assertEqual(schema['enum'], ['auto', 'keys', 'input-method'])
        request = make_request('type', arguments={'window': WINDOW, 'text': 'é', 'method': 'keys'}, caller_cwd='/',
                               session='default', expected_generation=GEN)
        wire = {'schema_version': 1, 'request_id': request.request_id, 'operation': 'type', 'session': 'default',
                'expected_generation': GEN, 'caller_cwd': '/', 'timeout_seconds': 3,
                'arguments': dict(request.arguments)}
        self.assertEqual(request_from_wire(wire).arguments['method'], 'keys')


class Client:
    """Fake input-method client with InputMethod's public surface."""

    def __init__(self, state='ready', active=True):
        self.state, self.is_active = state, active
        self.settled = state not in ('connecting', 'registry', 'binding')
        self.commits, self.finished, self.abandoned = [], [], []
        self.verdict = (True, None)
        self.written = 0
        self.epoch = 1 if active else 0
        self.activated_at = time.monotonic() - 1 if active else None

    def deactivate(self):
        self.is_active = False
        self.epoch += 1

    def activate(self, at=None):
        """A new context (a field, or a dialog's field) becomes active."""
        self.is_active = True
        self.epoch += 1
        self.activated_at = time.monotonic() if at is None else at

    def active(self):
        return self.state == 'ready' and self.is_active

    def commit(self, text, wait, limit):
        commit = SimpleNamespace(text=text, sent=True, flushed=True, flushed_at=time.monotonic(), lost=False,
                                 recalled=False,
                                 settled=lambda: True, verdict=lambda: self.verdict)
        commit.until = min(commit.flushed_at + wait, limit)
        self.commits.append(text)
        return commit

    def finish(self, commit):
        self.finished.append(commit)

    def abandon(self, commit):
        self.abandoned.append(commit)


class Target:
    instances = []
    error = None  # Raised by targets created from now on.
    on_recheck = None  # Called when a recheck (a target with a selected window) steps.

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.result = {'window': WINDOW, 'focused': True, 'query_artifact': 'window-observations/q.json'}
        self.error = Target.error
        self.selected = None
        Target.instances.append(self)

    def step(self, now):
        if self.error is not None:
            raise self.error
        if self.selected is not None and Target.on_recheck is not None:
            Target.on_recheck()
        return self.result

    def request_cancel(self, reason):
        pass

    def cleanup(self, now):
        return True


class Owner:
    def __init__(self):
        self.device = SimpleNamespace(held=[], emulating=False)
        self.devices = {1: self.device}
        self.uncertain = False
        self.retired_held = []
        self.presses = []

    def press(self, codes, kind='keyboard'):
        self.presses.append(list(codes))
        self.device.held.extend(codes)

    def release(self):
        self.device.held.clear()


class TypeTaskTests(unittest.TestCase):
    def make(self, text, method=None, client=None):
        arguments = {'window': WINDOW, 'text': text}
        if method:
            arguments['method'] = method
        request = make_request('type', arguments=arguments, caller_cwd='/')
        self.effects = []
        work = SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + request.timeout_seconds),
                               error=None, partial=None)

        def effects(partial, uncertain=False):  # Like Context.effects: the intent becomes the partial result.
            self.effects.append(partial)
            work.partial = partial
            if self.on_effects:
                self.on_effects()
        context = SimpleNamespace(work=work, effects=effects)
        self.on_effects = None
        self.owner = Owner()
        self.client = client if client is not None else Client()
        self.getter_calls = 0

        def getter():
            self.getter_calls += 1
            return self.client
        Target.instances, Target.error, Target.on_recheck = [], None, None
        patcher = patch('agent_desktop.input_actions.TargetTask', Target)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TypeTask(request, context, None, lambda: self.owner, None, lambda: None, getter)

    def run_task(self, task, limit=3):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            result = task.step(time.monotonic())
            if result is not None:
                return result
            time.sleep(.001)
        self.fail('task did not finish')

    def test_us_text_types_keys_exactly_as_before(self):
        task = self.make('Hi!\n')
        self.assertEqual(task.strokes, text_strokes('Hi!\n'))
        result = self.run_task(task)
        self.assertEqual(self.owner.presses, text_strokes('Hi!\n'))
        self.assertEqual((result['method'], result['strokes'], result['characters']), ('keys', 4, 4))
        self.assertEqual(self.client.commits, [])
        self.assertNotIn('text', str(self.effects))

    def test_other_text_is_one_commit_with_its_verdict(self):
        text = 'héllo ✓ 中文 🎉 é\n\t'
        task = self.make(text)
        result = self.run_task(task)
        self.assertEqual(self.client.commits, [text.encode()])
        self.assertEqual(self.owner.presses, [])
        self.assertEqual({k: result[k] for k in ('method', 'characters', 'bytes', 'confirmed', 'confirmation_reason')},
                         {'method': 'input_method', 'characters': len(text), 'bytes': len(text.encode()),
                          'confirmed': True, 'confirmation_reason': None})
        self.assertEqual(self.effects, [{'window': WINDOW, 'phase': 'emitting', 'method': 'input_method',
                                         'bytes': len(text.encode())}])
        self.assertNotIn(text, repr(result))
        self.client.verdict = (False, 'no_surrounding_text')
        result = self.run_task(self.make('é', client=self.client))
        self.assertEqual((result['confirmed'], result['confirmation_reason']), (False, 'no_surrounding_text'))

    def test_no_active_text_field_fails_before_anything_is_recorded_or_sent(self):
        task = self.make('é', client=Client(active=False))
        started = time.monotonic()
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertGreaterEqual(time.monotonic() - started, ACTIVATION_WAIT)
        self.assertEqual((caught.exception.code, caught.exception.context['reason'], caught.exception.outcome),
                         ('unsupported_input', 'text_input_unavailable', 'not_started'))
        self.assertEqual((self.effects, self.client.commits, self.owner.presses), ([], [], []))

    def test_a_late_activation_inside_the_bound_still_commits(self):
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        client.is_active = True
        self.assertTrue(self.run_task(task)['confirmed'])

    def test_unavailable_input_method_is_input_unavailable(self):
        for client in (Client(state='unavailable'), Client(state='failed')):
            with self.subTest(state=client.state), self.assertRaises(ContractError) as caught:
                self.run_task(self.make('é', client=client))
            self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                             ('input_unavailable', 'input_method_unavailable'))
        task = self.make('é')
        self.client = None
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.code, 'input_unavailable')

    def test_explicit_keys_still_refuses_other_text_and_ascii_can_be_committed(self):
        with self.assertRaises(ContractError) as caught:
            self.make('é', 'keys')
        self.assertEqual(caught.exception.code, 'unsupported_input')
        result = self.run_task(self.make('ab', 'input-method'))
        self.assertEqual((result['method'], self.client.commits), ('input_method', [b'ab']))

    def test_uncertain_input_still_refuses_a_commit(self):
        task = self.make('é')
        self.owner.uncertain = True
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.code, 'input_uncertain')
        self.assertEqual(self.client.commits, [])

    def test_failure_after_the_commit_reports_it_as_partial(self):
        task = self.make('é')
        commit = SimpleNamespace(sent=True, flushed=False, lost=True, flushed_at=None)
        self.client.commit = lambda text, **_: commit
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.assertEqual(caught.exception.partial_result, {'window': WINDOW, 'method': 'input_method', 'bytes': 2,
                                                          'commit_sent': True})

    def test_cancel_after_the_commit_abandons_it_and_keeps_progress(self):
        task = self.make('é')
        commit = SimpleNamespace(sent=True, flushed=False, lost=False, recalled=False, flushed_at=None)
        self.client.commit = lambda text, **_: commit
        for _ in range(5):
            task.step(time.monotonic())
        task.request_cancel('cancelled')
        self.assertEqual(self.client.abandoned, [commit])
        self.assertEqual(task.context.work.partial['commit_sent'], True)
        self.assertTrue(task.cleanup(time.monotonic()))

    def test_a_context_change_while_the_intent_is_recorded_sends_nothing(self):
        task = self.make('é')
        self.on_effects = self.client.activate  # A dialog's field takes over before the commit.
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('target_lost', 'context_changed'))
        self.assertEqual(self.client.commits, [])

    def test_a_context_change_during_the_focus_recheck_records_nothing(self):
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        client.activate()
        Target.on_recheck = client.activate  # A dialog's field takes over during the recheck.
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.context['reason'], 'context_changed')
        self.assertEqual((self.effects, client.commits), ([], []))

    def test_a_delayed_activation_rechecks_focus_before_committing(self):
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        client.activate()
        Target.error = ContractError('target_lost', 'Target is not focused.', context={'reason': 'focus_lost'})
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.context['reason'], 'focus_lost')
        self.assertEqual(len(Target.instances), 2)
        self.assertEqual(Target.instances[1].selected, WINDOW)
        self.assertEqual(client.commits, [])
        # With focus still on the window, the commit goes ahead.
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        client.activate()
        self.assertTrue(self.run_task(task)['confirmed'])
        self.assertEqual((len(Target.instances), client.commits), (2, ['é'.encode()]))

    def test_an_already_active_context_needs_no_second_focus_check(self):
        self.run_task(self.make('é'))
        self.assertEqual(len(Target.instances), 1)

    def test_an_activation_after_the_bound_is_refused_by_its_own_time(self):
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        time.sleep(ACTIVATION_WAIT + .05)
        client.activate()  # Observed now: too late, even though no step ran in between.
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.context['reason'], 'text_input_unavailable')
        self.assertEqual(client.commits, [])
        # Observed in time, though the owner only gets to it late: accepted.
        client = Client(active=False)
        task = self.make('é', client=client)
        self.assertIsNone(task.step(time.monotonic()))
        client.activate(at=time.monotonic() + .05)
        time.sleep(ACTIVATION_WAIT + .05)
        self.assertTrue(self.run_task(task)['confirmed'])

    def test_the_client_is_acquired_once_per_request(self):
        client = Client(state='binding', active=False)
        task = self.make('é', client=client)
        for _ in range(5):
            self.assertIsNone(task.step(time.monotonic()))
        client.state, client.settled = 'failed', True
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual(caught.exception.context['reason'], 'input_method_unavailable')
        self.assertEqual(self.getter_calls, 1)

    def test_a_commit_recalled_with_its_field_fails_with_nothing_sent(self):
        task = self.make('é')
        commit = SimpleNamespace(sent=False, flushed=False, lost=False, recalled=True, changed=True)
        self.client.commit = lambda text, **_: commit
        with self.assertRaises(ContractError) as caught:
            for _ in range(5):
                task.step(time.monotonic())
        error = caught.exception
        self.assertEqual((error.code, error.context['reason']), ('target_lost', 'context_changed'))
        self.assertEqual(error.partial_result['commit_sent'], False)

    def test_losing_the_connection_before_the_commit_is_input_method_unavailable(self):
        task = self.make('é')
        self.on_effects = lambda: setattr(self.client, 'state', 'failed')
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('input_unavailable', 'input_method_unavailable'))
        self.assertEqual(self.client.commits, [])

    def test_the_field_deactivated_before_the_commit_is_context_changed(self):
        task = self.make('é')
        self.on_effects = self.client.deactivate
        with self.assertRaises(ContractError) as caught:
            self.run_task(task)
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('target_lost', 'context_changed'))
        self.assertEqual(self.client.commits, [])

    def test_timeout_after_the_commit_reports_it_sent_over_the_intent(self):
        task = self.make('é')
        commit = SimpleNamespace(sent=True, flushed=False, lost=False, recalled=False, flushed_at=None)
        self.client.commit = lambda text, **_: commit
        for _ in range(5):
            task.step(time.monotonic())
        self.assertEqual(task.context.work.partial['phase'], 'emitting')  # The intent, as Context.effects left it.
        task.request_cancel('timeout')
        self.assertEqual(task.context.work.partial['commit_sent'], True)
        self.assertEqual(task.context.work.partial['bytes'], 2)


if __name__ == '__main__':
    unittest.main()
