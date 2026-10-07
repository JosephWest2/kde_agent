"""Durable writes off the owner thread (#96): one writer thread, one bounded FIFO.

The owner submits a Store call and gets a Ticket back at once. The writer thread
runs the calls one at a time in submission order, so per-record revisions and
events.jsonl keep the owner's order. The Store's on-disk format and its write
protocol (temporary, fsync, replace, directory fsync, nonblocking lock) are
unchanged; only the thread that runs them moves.

The owner never waits on the writer: submit puts on a SimpleQueue (never blocks)
and drain() pops finished tickets from a deque, once per owner turn. No lock is
shared with the writer while it is inside a write. Work that must not start
before a record is durable checks its Ticket on later turns instead of blocking.

Arguments are deep-copied at submission, so the writer sees the snapshot the
owner meant even if the owner changes its own objects afterwards. A Ticket
passed as an argument (a request token, an allocated path) stands for that
earlier call's return value; FIFO order means it is known by then.

Without a thread (threaded=False, the default for tests and helpers) each call
runs inline inside submit and its Ticket is finished before submit returns, so
callers see exactly the synchronous behavior they always had.
"""
from __future__ import annotations

from collections import deque
import copy
import queue
import sys
import threading
import time

from .contracts import ContractError

MAX_OPS = 256              # Queued or running writes.
MAX_BYTES = 16 << 20       # Their estimated encoded size (see SIZES).
PAUSE_OPS = 8              # Ordinary admission waits while this many are outstanding.
STALL_SECONDS = 5.0        # One write this long fails the session, as the 5s watchdog did.
RECORD = 65536
SIZES = {'event': 4096, 'launch': 1 << 20, 'window_observation': 1 << 20}
_STOP = object()


def _monotonic():
    return time.monotonic()


class Ticket:
    """One submitted write. Owner-visible fields change only in drain() on the owner."""

    __slots__ = ('seq', 'name', 'size', 'done', 'error', 'result', 'callbacks',
                 '_value', '_failed')

    def __init__(self, seq, name, size):
        self.seq, self.name, self.size = seq, name, size
        self.done = False
        self.error = None
        self.result = None
        self.callbacks = []
        self._value = None   # Writer-side copies, read only by the writer.
        self._failed = False

    def __deepcopy__(self, memo):
        return self  # A placeholder for an earlier result, never copied.

    def __repr__(self):
        return f'<Ticket {self.seq} {self.name} {"done" if self.done else "pending"}>'

    def on_error(self, callback):
        """Call callback(error) on the owner if this write fails (now, if it already has)."""
        if self.done:
            if self.error is not None:
                callback(self.error)
        else:
            self.callbacks.append(callback)
        return self


def finished(ticket):
    """True once TICKET is durable; re-raises its write's exception. None counts as durable."""
    if ticket is None:
        return True
    if not ticket.done:
        return False
    if ticket.error is not None:
        raise ticket.error
    return True


def all_finished(tickets):
    """finished() over several tickets: every one must be done; the first failure raises."""
    done = True
    for ticket in tickets:
        if ticket is None:
            continue
        if ticket.done and ticket.error is not None:
            raise ticket.error
        done = done and ticket.done
    return done


def track(context, tickets):
    """Make TICKETS part of a scheduler request's records (its response waits for them)."""
    tickets = [ticket for ticket in tickets if ticket is not None]
    method = getattr(context, 'track', None)
    if method is not None:
        method(tickets)
    return tickets


def recorded(context):
    """Context.recorded() where the context has it (task doubles may not); else True."""
    method = getattr(context, 'recorded', None)
    return True if method is None else method()


class Journal:
    def __init__(self, store, *, threaded=False, max_ops=MAX_OPS, max_bytes=MAX_BYTES, clock=_monotonic):
        self.store = store
        self.threaded = threaded
        self.max_ops, self.max_bytes = max_ops, max_bytes
        self.clock = clock
        self.seq = 0
        self.outstanding = 0      # Owner-side count of submitted, not yet drained, writes.
        self.reserved = 0         # Their estimated bytes.
        self.durable_through = 0  # Highest drained sequence number.
        self.closed = False
        self.requests = queue.SimpleQueue()
        self.completions = deque()
        self.current = None       # (seq, started) of the write in progress; set by the writer.
        self.thread = None
        if threaded:
            self.thread = threading.Thread(target=self._run, name='agent-desktop-writer', daemon=True)
            self.thread.start()

    # Owner side ------------------------------------------------------------

    def submit(self, method, *args, on_error=None, size=None, **kwargs):
        """Queue store.METHOD(*args, **kwargs), or METHOD(...) when it is a function (an
        unsynced diagnostic file such as health.json); returns its Ticket. Never blocks
        or raises for a storage reason: a full queue or a closed journal fails the
        Ticket instead."""
        function = method if callable(method) else getattr(self.store, method)
        method = getattr(method, '__name__', 'call') if callable(method) else method
        self.seq += 1
        ticket = Ticket(self.seq, method, SIZES.get(method, RECORD) if size is None else size)
        if on_error is not None:
            ticket.callbacks.append(on_error)
        if not self.threaded:
            self._finish(ticket, *self._call(ticket, function, args, kwargs))
            return ticket
        if (self.closed or self.outstanding >= self.max_ops
                or self.reserved + ticket.size > self.max_bytes):
            phase = 'closed' if self.closed else 'queue_full'
            self._finish(ticket, None, ContractError('artifact_failed', 'Durable record could not be preserved.',
                                                     context={'phase': phase}))
            return ticket
        try:
            args, kwargs = copy.deepcopy((args, kwargs))
        except Exception as error:
            self._finish(ticket, None, error)
            return ticket
        self.outstanding += 1
        self.reserved += ticket.size
        self.requests.put((ticket, function, args, kwargs))
        return ticket

    def drain(self):
        """Hand finished writes back to the owner. Called at the start of each owner turn."""
        count = 0
        while self.completions:
            ticket, value, error = self.completions.popleft()
            self.outstanding -= 1
            self.reserved -= ticket.size
            self._finish(ticket, value, error)
            count += 1
        return count

    @property
    def pending(self):
        return self.outstanding

    def idle(self):
        self.drain()
        return self.outstanding == 0

    def paused(self):
        """Whether ordinary admission should wait for the writer to catch up."""
        return self.threaded and self.outstanding >= PAUSE_OPS

    def stalled(self, now=None):
        """Seconds the write in progress has been running (0 when none)."""
        current = self.current
        if current is None:
            return 0.0
        return max(0.0, (self.clock() if now is None else now) - current[1])

    def close(self, deadline):
        """Stop accepting writes; let the writer finish what is queued until DEADLINE.
        Returns True when the writer thread has exited (its Store fd may be closed)."""
        self.closed = True
        if self.thread is None:
            return True
        self.requests.put(_STOP)
        self.thread.join(max(0.0, deadline - self.clock()))
        self.drain()
        return not self.thread.is_alive()

    def _finish(self, ticket, value, error):
        if error is None:
            ticket.result = value
        else:
            ticket.error = error
        ticket.done = True
        self.durable_through = max(self.durable_through, ticket.seq)
        callbacks, ticket.callbacks = ticket.callbacks, []
        if error is not None:
            for callback in callbacks:
                try:
                    callback(error)
                except Exception:
                    pass

    # Either side -----------------------------------------------------------

    @staticmethod
    def _resolve(value):
        if isinstance(value, Ticket):
            if value._failed:
                raise ContractError('artifact_failed', 'Durable record could not be preserved.',
                                    context={'phase': 'dependency'})
            return value._value
        return value

    def _call(self, ticket, function, args, kwargs):
        try:
            args = tuple(self._resolve(value) for value in args)
            kwargs = {key: self._resolve(value) for key, value in kwargs.items()}
            value = function(*args, **kwargs)
        except BaseException as error:  # Handed back; never kills the writer.
            ticket._failed = True
            return None, error
        ticket._value = value
        return value, None

    # Writer side -----------------------------------------------------------

    def _run(self):
        while True:
            item = self.requests.get()
            if item is _STOP:
                return
            ticket, function, args, kwargs = item
            self.current = (ticket.seq, self.clock())
            value, error = self._call(ticket, function, args, kwargs)
            self.current = None
            self.completions.append((ticket, value, error))


def journal_of(store):
    """The store's Journal: the worker's threaded one when installed, else an inline one."""
    journal = getattr(store, '__dict__', {}).get('journal')
    if isinstance(journal, Journal):
        return journal
    journal = Journal(store)
    try:
        store.journal = journal
    except Exception:
        pass
    return journal


def diagnostic(message):
    try:
        print('agent-desktop: ' + message, file=sys.stderr)
    except OSError:
        pass
