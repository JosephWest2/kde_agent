"""Worker-owned zwp_input_method_v1 client: a minimal Wayland wire client on the GLib owner.

`type` commits text the US layout cannot type through KWin's input-method
protocol (see docs/INPUT.md and docs/UNICODE.md). This module speaks just
enough of the Wayland wire protocol for that: wl_display, wl_registry,
wl_callback, zwp_input_method_v1 and its contexts. None of them carry file
descriptors, so the client needs no libwayland and no native code.

The owner thread never blocks on the socket: it is non-blocking, GLib watches
it, each wakeup reads at most READ_BYTES and parses only complete messages, and
writes that would block wait in a bounded buffer for the next writable wakeup.

One connection serves the session. KWin sends `activate` for a focused text
field, but it does not replay that field's surrounding text to a client that
binds later; the text arrives only when the application next updates its
text-input state. A connection opened per request would therefore almost never
have the before-snapshot that confirmation needs. If the connection is lost,
the next request opens a new one.

The focused field's surrounding text is application data. It is kept in memory
only (the latest snapshot of the current context) and never logged, recorded or
returned: results carry lengths and whether it matched.
"""
from __future__ import annotations

import math
import os
import socket
import struct
import time

from .keymap import MAX_COMMIT_BYTES  # text-input-v3's commit limit; the message then fits 4096 bytes.

MAX_MESSAGE = 4096        # libwayland's message size limit.
TRUNCATION_BYTES = 3900   # A snapshot this long may be a toolkit's window into a longer text (4000-byte cap).
READ_BYTES = 65536        # At most this much is read per wakeup.
MAX_OUTGOING = 64 * 1024  # Bound on queued writes; a commit is at most MAX_MESSAGE.
SERVER_ID = 0xff000000    # Objects the compositor creates (a context) have ids from here.
MAX_OBJECTS = 16          # Live objects: display, registry, the input method, a sync callback, one context.
CONFIRM_WAIT = .25        # After a commit is written, only reports received within this count.
INTERFACE = 'zwp_input_method_v1'

DISPLAY, REGISTRY = 1, 2
# Event signatures by interface and opcode. u: uint, i: int, s: string, o: object, n: new_id.
EVENTS = {
    'wl_display': {0: ('error', 'ous'), 1: ('delete_id', 'u')},
    'wl_registry': {0: ('global', 'usu'), 1: ('global_remove', 'u')},
    'wl_callback': {0: ('done', 'u')},
    INTERFACE: {0: ('activate', 'n'), 1: ('deactivate', 'o')},
    'context': {0: ('surrounding_text', 'suu'), 1: ('reset', ''), 2: ('content_type', 'uu'),
                3: ('invoke_action', 'uu'), 4: ('commit_state', 'u'), 5: ('preferred_language', 's')},
}
# Requests: (object interface, opcode).
SYNC, GET_REGISTRY = 0, 1   # wl_display
BIND = 0                    # wl_registry
CONTEXT_DESTROY, COMMIT_STRING = 0, 1  # zwp_input_method_context_v1


class ProtocolError(Exception):
    """A malformed or unexpected message. The message is fixed text, never received data."""


def padded(length):
    return (length + 3) & ~3


def encode(obj, opcode, *args):
    """One request: ARGS are ('u'|'i'|'o'|'n', int) or ('s', bytes)."""
    body = bytearray()
    for kind, value in args:
        if kind == 'i':
            body += struct.pack('<i', value)
        elif kind in 'uon':
            body += struct.pack('<I', value)
        elif kind == 's':
            if b'\0' in value:
                raise ValueError('Wayland strings cannot contain NUL.')
            data = value + b'\0'
            body += struct.pack('<I', len(data)) + data + bytes(padded(len(data)) - len(data))
        else:
            raise ValueError('Unknown argument kind.')
    size = 8 + len(body)
    if size > MAX_MESSAGE:
        raise ValueError('Wayland message too long.')
    return struct.pack('<II', obj, size << 16 | opcode) + bytes(body)


def commit_message(context, serial, text):
    """zwp_input_method_context_v1.commit_string(serial, text) for TEXT (UTF-8 bytes)."""
    return encode(context, COMMIT_STRING, ('u', serial), ('s', text))


def decode(signature, payload):
    """Arguments of one event payload; every byte must be accounted for."""
    values, offset, end = [], 0, len(payload)
    for kind in signature:
        if offset + 4 > end:
            raise ProtocolError('Truncated Wayland event.')
        word = payload[offset:offset + 4]
        offset += 4
        if kind == 'i':
            values.append(struct.unpack('<i', word)[0])
        elif kind in 'uon':
            values.append(struct.unpack('<I', word)[0])
        else:  # 's'
            length = struct.unpack('<I', word)[0]
            if length == 0:
                values.append(None)
                continue
            if offset + padded(length) > end or payload[offset + length - 1] != 0:
                raise ProtocolError('Malformed Wayland string.')
            values.append(bytes(payload[offset:offset + length - 1]))
            offset += padded(length)
    if offset != end:
        raise ProtocolError('Unexpected Wayland event length.')
    return values


def frames(buffer):
    """Complete messages at the start of BUFFER as (object, opcode, payload); returns (messages, consumed)."""
    messages, offset = [], 0
    while len(buffer) - offset >= 8:
        obj, word = struct.unpack_from('<II', buffer, offset)
        size, opcode = word >> 16, word & 0xffff
        if size < 8 or size % 4:
            raise ProtocolError('Invalid Wayland message size.')
        if len(buffer) - offset < size:
            break
        messages.append((obj, opcode, bytes(buffer[offset + 8:offset + size])))
        offset += size
    return messages, offset


class Snapshot:
    """One surrounding_text event: UTF-8 TEXT with byte offsets CURSOR and ANCHOR."""
    __slots__ = ('text', 'cursor', 'anchor', 'epoch', 'seq', 'received_at')

    def __init__(self, text, cursor, anchor, epoch=0, seq=0, received_at=None):
        self.text, self.cursor, self.anchor, self.epoch, self.seq = text, cursor, anchor, epoch, seq
        self.received_at = received_at

    def empty(self):
        return not self.text and self.cursor == 0 and self.anchor == 0


def expected_after(before, committed):
    """(text, cursor) after COMMITTED replaces BEFORE's selection, or None if its offsets are invalid."""
    start, end = sorted((before.cursor, before.anchor))
    text = before.text
    if end > len(text):
        return None
    for offset in (start, end):
        # Offsets must fall on UTF-8 character boundaries.
        if offset < len(text) and text[offset] & 0xc0 == 0x80:
            return None
    return text[:start] + committed + text[end:], start + len(committed)


def matches(before, after, committed):
    """True only if AFTER is BEFORE with its selection replaced by COMMITTED and the cursor right after it."""
    expected = expected_after(before, committed)
    return (expected is not None and after.text == expected[0]
            and after.cursor == after.anchor == expected[1])


def verdict(before, afters, committed, *, changed, lost=False):
    """(confirmed, reason) from the before-snapshot, the later snapshots of the same context, and events.

    BEFORE is the latest snapshot of the current context received before the
    commit (None if there was none). AFTERS are that context's snapshots
    received after the commit was written, in order. CHANGED means the context
    was deactivated or replaced in between. Confirmation is an observation,
    never proof, and False never means nothing was sent.
    """
    near_cap = lambda snapshot: len(snapshot.text) >= TRUNCATION_BYTES
    if before is not None and any(matches(before, after, committed) for after in afters):
        expected = expected_after(before, committed)[0]
        if near_cap(before) or len(expected) >= TRUNCATION_BYTES:
            return False, 'truncated'
        return True, None
    if changed or lost:
        return False, 'context_changed'
    if before is None:
        return False, 'no_fresh_snapshot'
    if not afters:
        return False, 'timeout'
    if near_cap(before) or any(near_cap(after) for after in afters) or (
            len(before.text) + len(committed) >= TRUNCATION_BYTES):
        return False, 'truncated'
    if committed and before.empty() and all(after.empty() for after in afters):
        return False, 'no_surrounding_text'
    return False, 'mismatch'


class Commit:
    """One commit_string in flight: whether it was written, and what the context reported afterwards."""

    def __init__(self, client, text, before, epoch, start, end, wait=CONFIRM_WAIT, limit=math.inf):
        self.client, self.text, self.before, self.epoch = client, text, before, epoch
        self.start, self.end = start, end  # Byte offsets of the message in the connection's output stream.
        self.wait, self.limit = wait, limit
        self.afters = []
        self.matched = False
        self.changed = False
        self.lost = False
        self.recalled = False
        self.flushed_at = None

    @property
    def sent(self):
        """Some of the message reached the socket: the commit may have been delivered."""
        return not self.recalled and self.client.written > self.start

    @property
    def flushed(self):
        return not self.recalled and self.client.written >= self.end

    @property
    def until(self):
        """Reports received after this (WAIT after the commit was written, at most LIMIT) do not count."""
        return self.limit if self.flushed_at is None else min(self.flushed_at + self.wait, self.limit)

    def observe(self, snapshot):
        if snapshot.received_at is not None and snapshot.received_at > self.until:
            return  # Too late: the request's verdict may already be out, and must not depend on it.
        if self.flushed and snapshot.epoch == self.epoch and len(self.afters) < 64:
            self.afters.append(snapshot)
            if self.before is not None and matches(self.before, snapshot, self.text):
                self.matched = True

    def settled(self):
        """Nothing further can change the verdict."""
        return self.matched or self.changed or self.lost

    def verdict(self):
        return verdict(self.before, self.afters, self.text, changed=self.changed, lost=self.lost)


class InputMethod:
    """One Wayland connection that binds zwp_input_method_v1 and tracks its active context.

    state: 'new', 'connecting', 'registry' (globals requested), 'binding'
    (bound, waiting for the round trip), 'ready', 'unavailable' (the global is
    not advertised), 'failed' or 'closed'. Only 'ready' commits.
    """

    def __init__(self, path, GLib, *, clock=time.monotonic):
        self.path, self.GLib, self.clock = str(path), GLib, clock
        self.state = 'new'
        self.reason = None
        self.advertised = None
        self.sock = None
        self.watch = self.out_watch = None
        self.inbox = bytearray()
        self.outgoing = bytearray()
        self.written = 0       # Bytes written to the socket so far.
        self.queued = 0        # Bytes ever queued (written + outgoing).
        self.objects = {DISPLAY: 'wl_display', REGISTRY: 'wl_registry'}
        self.next_id = 3
        self.registry_sync = self.bind_sync = None
        self.im = None
        self.context = None    # The active context's object id.
        self.epoch = 0         # Incremented on every activate and deactivate.
        self.serial = 0        # The latest commit_state serial of the active context.
        self.snapshot = None   # The latest surrounding text of the active context.
        self.seq = 0
        self.activations = 0
        self.activated_at = None  # Clock time of the latest activate.
        self.pending = None    # The Commit awaiting confirmation, if any.
        self.deadline = None
        self.started_at = None

    # Connection -----------------------------------------------------------

    def start(self, deadline):
        """Open the connection and ask for the registry; never waits."""
        if self.state != 'new':
            raise RuntimeError('An input-method connection starts once.')
        self.deadline = deadline
        self.started_at = self.clock()
        self.state = 'connecting'
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC)
        except OSError:
            self._fail('socket')
            return
        self._connect()

    def _connect(self):
        try:
            self.sock.connect(self.path)
        except (BlockingIOError, InterruptedError):
            return  # The listen backlog is full; tick() tries again.
        except OSError:
            self._fail('connect')
            return
        self.state = 'registry'
        self.watch = self.GLib.io_add_watch(self.sock.fileno(), self.GLib.PRIORITY_DEFAULT,
                                            self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, self._on_io)
        self._send(encode(DISPLAY, GET_REGISTRY, ('n', REGISTRY)))
        self.registry_sync = self._callback()
        self._send(encode(DISPLAY, SYNC, ('n', self.registry_sync)))

    def _callback(self):
        return self._allocate('wl_callback')

    def _allocate(self, interface):
        new = self.next_id
        if new >= SERVER_ID or len(self.objects) >= MAX_OBJECTS:
            raise ProtocolError('Client object ids exhausted.')
        self.next_id += 1
        self.objects[new] = interface
        return new

    def tick(self, now=None):
        """Retry a pending connect and expire set-up; never waits."""
        now = self.clock() if now is None else now
        if self.state == 'connecting':
            self._connect()
        if self.state in ('connecting', 'registry', 'binding') and self.deadline is not None and now >= self.deadline:
            self._fail('timeout')

    @property
    def settled(self):
        return self.state not in ('new', 'connecting', 'registry', 'binding')

    def usable(self):
        return self.state == 'ready'

    def active(self):
        return self.state == 'ready' and self.context is not None

    def health(self):
        """Bounded status for session health: never any text."""
        value = {'state': {'ready': 'passed'}.get(self.state, self.state), 'advertised': self.advertised}
        if self.reason is not None:
            value['reason'] = self.reason
        return value

    def close(self):
        if self.state != 'closed':
            self._teardown()
            self.state = 'closed'

    def _teardown(self):
        for name in ('watch', 'out_watch'):
            source = getattr(self, name)
            setattr(self, name, None)
            if source is not None:
                try:
                    self.GLib.source_remove(source)
                except Exception:
                    pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.outgoing.clear()
        self.inbox.clear()
        if self.context is not None:
            self.epoch += 1
        self.context, self.snapshot = None, None
        if self.pending is not None:
            self.pending.lost = True

    def _fail(self, reason):
        if self.state in ('failed', 'closed'):
            return
        self.reason = reason
        self._teardown()
        self.state = 'failed'

    # I/O -------------------------------------------------------------------

    def _send(self, message):
        if self.sock is None:
            return  # Torn down (a write failed mid-dispatch): nothing more goes out.
        if len(self.outgoing) + len(message) > MAX_OUTGOING:
            raise ProtocolError('Input-method output buffer is full.')
        self.outgoing += message
        self.queued += len(message)
        self._flush()

    def _flush(self):
        commit = self.pending
        if commit is not None and commit.epoch != self.epoch and self.sock is not None:
            self._stale(commit)  # The write boundary: never write a commit to a context no longer active.
            if self.sock is None:
                return
        while self.outgoing and self.sock is not None:
            try:
                count = self.sock.send(self.outgoing, socket.MSG_NOSIGNAL | socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._fail('disconnected')
                return
            if count <= 0:
                break
            del self.outgoing[:count]
            self.written += count
        if self.pending is not None and self.pending.flushed and self.pending.flushed_at is None:
            self.pending.flushed_at = self.clock()
        if self.outgoing and self.out_watch is None and self.sock is not None:
            self.out_watch = self.GLib.io_add_watch(self.sock.fileno(), self.GLib.PRIORITY_DEFAULT,
                                                    self.GLib.IO_OUT, self._on_writable)
        elif not self.outgoing and self.out_watch is not None:
            self.GLib.source_remove(self.out_watch)
            self.out_watch = None

    def _on_writable(self, fd, condition):
        self.out_watch = None
        self._flush()
        return False  # _flush adds a new watch if anything is still queued.

    def _on_io(self, fd, condition):
        if self.sock is None:
            self.watch = None
            return False
        self.pump()
        if self.sock is None:
            self.watch = None
            return False
        if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR) and not condition & self.GLib.IO_IN:
            self._fail('disconnected')
            self.watch = None
            return False
        return True

    def pump(self):
        """Read at most READ_BYTES and handle every complete message; never waits."""
        if self.sock is None:
            return
        try:
            data = self.sock.recv(READ_BYTES, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._fail('disconnected')
            return
        if not data:
            self._fail('disconnected')
            return
        self.inbox += data
        try:
            messages, consumed = frames(self.inbox)
            del self.inbox[:consumed]
            for message in messages:
                self._dispatch(*message)
                if self.sock is None:
                    return
        except ProtocolError:
            self._fail('protocol')
        except ValueError:
            self._fail('protocol')

    # Events ----------------------------------------------------------------

    def _dispatch(self, obj, opcode, payload):
        interface = self.objects.get(obj)
        if interface is None:
            # Events can still be in flight to a context destroyed after deactivate; skip them.
            if obj >= SERVER_ID:
                return
            raise ProtocolError('Event for an unknown object.')
        events = EVENTS['context' if interface == 'context' else interface]
        if opcode not in events:
            raise ProtocolError('Unknown Wayland event.')
        name, signature = events[opcode]
        args = decode(signature, payload)
        getattr(self, f'_{interface}_{name}')(obj, *args)

    def _wl_display_error(self, obj, target, code, message):
        self._fail('protocol_error')

    def _wl_display_delete_id(self, obj, identifier):
        if identifier < SERVER_ID and self.objects.get(identifier) == 'wl_callback':
            del self.objects[identifier]

    def _wl_registry_global(self, obj, name, interface, version):
        if interface == INTERFACE.encode() and self.im is None and self.state == 'registry':
            self.advertised = True
            self.im = self._allocate(INTERFACE)
            self._send(encode(REGISTRY, BIND, ('u', name), ('s', INTERFACE.encode()), ('u', 1), ('n', self.im)))

    def _wl_registry_global_remove(self, obj, name):
        pass

    def _wl_callback_done(self, obj, data):
        if obj == self.registry_sync and self.state == 'registry':
            if self.im is None:
                self.advertised = False
                self.reason = 'not_advertised'
                self._teardown()
                self.state = 'unavailable'
                return
            # A second round trip: an error from the bind would arrive before its done.
            self.state = 'binding'
            self.bind_sync = self._callback()
            self._send(encode(DISPLAY, SYNC, ('n', self.bind_sync)))
        elif obj == self.bind_sync and self.state == 'binding':
            self.state = 'ready'
            self.deadline = None

    def _zwp_input_method_v1_activate(self, obj, context):
        if context < SERVER_ID or context in self.objects:
            raise ProtocolError('Invalid input-method context id.')
        # KWin deactivates before activating another field (as recorded), but
        # an activation that replaces a context retires it, as a deactivate would.
        retired = self.context
        if retired is not None:
            del self.objects[retired]
        if len(self.objects) >= MAX_OBJECTS:
            raise ProtocolError('Too many input-method objects.')
        self.objects[context] = 'context'
        self.context = context
        self.activated_at = self.clock()
        self.epoch += 1
        self.activations += 1
        self.serial = 0
        self.snapshot = None
        self._stale(self.pending)
        if retired is not None:
            self._send(encode(retired, CONTEXT_DESTROY))  # A no-op if the connection is already torn down.

    def _zwp_input_method_v1_deactivate(self, obj, context):
        if self.objects.get(context) != 'context':
            if context >= SERVER_ID:
                return  # A context already retired by a later activate.
            raise ProtocolError('Deactivate of an unknown context.')
        del self.objects[context]
        if context == self.context:
            self.context, self.snapshot = None, None
            self.epoch += 1
            self._stale(self.pending)
        # v1 contexts are destroyed by the client once deactivated.
        self._send(encode(context, CONTEXT_DESTROY))

    def _stale(self, commit):
        """COMMIT's context is no longer active: recall it if none of it was written; if only part
        was, close the connection (the rest can neither be recalled nor sent to another field)."""
        if commit is None or commit.recalled or commit.lost:
            return
        commit.changed = True
        if commit.flushed:
            return
        if commit.sent:
            self._fail('context_changed')
        else:
            self._recall(commit)

    def _recall(self, commit):
        """Remove COMMIT, none of which was written, from the output queue."""
        offset = commit.start - self.written
        del self.outgoing[offset:offset + commit.end - commit.start]
        self.queued -= commit.end - commit.start
        commit.end = commit.start  # Never flushed: nothing of it can be written now.
        commit.recalled = True

    def _context_surrounding_text(self, obj, text, cursor, anchor):
        if obj != self.context:
            return
        self.seq += 1
        self.snapshot = Snapshot(text or b'', cursor, anchor, self.epoch, self.seq, self.clock())
        if self.pending is not None:
            self.pending.observe(self.snapshot)

    def _context_reset(self, obj):
        pass

    def _context_content_type(self, obj, hint, purpose):
        pass

    def _context_invoke_action(self, obj, button, index):
        pass

    def _context_commit_state(self, obj, serial):
        if obj == self.context:
            self.serial = serial

    def _context_preferred_language(self, obj, language):
        pass

    # Commit ----------------------------------------------------------------

    def commit(self, text, *, wait=CONFIRM_WAIT, limit=math.inf):
        """Send TEXT (UTF-8 bytes, at most MAX_COMMIT_BYTES) to the active context as one commit_string.

        Reports count towards its verdict until WAIT after it was written, and never after LIMIT.
        """
        if not self.active():
            raise RuntimeError('No active input-method context.')
        if len(text) > MAX_COMMIT_BYTES:
            raise ValueError('Commit text too long.')
        message = commit_message(self.context, self.serial, text)
        start = self.queued
        self.pending = Commit(self, text, self.snapshot, self.epoch, start, start + len(message), wait, limit)
        try:
            self._send(message)
        except ProtocolError:  # The compositor stopped reading: the output buffer is full.
            self._fail('overflow')
        return self.pending

    def finish(self, commit):
        """Stop tracking COMMIT (its request has its verdict)."""
        if self.pending is commit:
            self.pending = None

    def abandon(self, commit):
        """Cancellation: recall COMMIT if none of it was written; if only part was, close the connection
        (the rest can neither be recalled nor completed)."""
        if self.pending is commit and not commit.flushed and not commit.recalled:
            if commit.sent:
                self._fail('abandoned')
            elif self.sock is not None:
                self._recall(commit)
                self._flush()
        self.finish(commit)


def socket_path(desktop):
    """The private compositor's Wayland socket."""
    return os.path.join(str(desktop.root), desktop.env['WAYLAND_DISPLAY'])

