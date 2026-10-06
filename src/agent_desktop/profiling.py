"""Opt-in owner-loop latency profile (AGENT_DESKTOP_PROFILE_OWNER=1), for #80.

Set the variable in the environment of `session start`; the service start
passes exactly this one setting through to the worker. The worker then wraps a
fixed list of owner-thread methods, runs a 5ms probe timer and appends JSON lines
to logs/owner-profile.jsonl in its generation. tools/owner_profile.py summarizes
them; see docs/TESTING.md.

Without the variable install() returns None and wraps nothing. With it, each
wrapped call costs about a microsecond. The profile is a diagnostic, not a
durable record: lines are appended without fsync, and a failed write turns the
profile off instead of failing the worker.

Records (times are time.monotonic() seconds, durations milliseconds):
  start    the profiler's settings
  late     a probe ran STALL or more late; the root callbacks that overlapped it
  stall    a root callback that took STALL or more, with every span in it of at
           least DETAIL: [depth, name, offset, duration, self]
  input    a press, release, scroll or move, or a focus recheck's start/end
  done     a request's accepted response (operation, error code, deadline)
  summary  cumulative probe histogram and per-call-path totals (every
           SUMMARY_SECONDS, and final at loop exit)
A span name is Class.method, [label] for the operation, phase or event, and
@file:line for the caller where that is the interesting part.
"""
from __future__ import annotations

import builtins
from collections import deque
import functools
import gc
import json
import math
import os
import sys
import threading
import time

ENV = 'AGENT_DESKTOP_PROFILE_OWNER'
FILENAME = 'owner-profile.jsonl'
PROBE_MS = 5            # Probe timer interval. Lateness is the gap between probes minus this.
STALL = .010            # Late probes and root callbacks this long are written one by one.
DETAIL = .0005          # Shorter spans are left out of a stall record (still in the totals).
FAST_IMPORT = .0002     # A faster import found the module loaded; it is not recorded.
SUMMARY_SECONDS = 5.0
RECENT = 64             # Root callbacks kept to explain a late probe.
PACKAGE = __name__.rpartition('.')[0]


def _operation(args, kwargs):
    return args[1].request.operation


def _second(args, kwargs):
    return args[2]


def _phase(args, kwargs):
    return args[0].phase


def _wire(args, kwargs):
    return args[1].get('operation') if isinstance(args[1], dict) else None


# (target, kind, label, caller site). Kinds: span, input, watch, complete, bus.
TARGETS = (
    ('transport.Server.service', 'span', None, False),
    ('transport.Server.accept', 'span', None, False),
    ('transport.Server.expire', 'span', None, False),
    ('transport.Connection.ready', 'span', None, False),
    ('transport.Connection.dispatch', 'span', _wire, False),
    ('transport.Connection.reply', 'span', None, False),
    ('transport.Admission.complete', 'complete', None, False),
    ('scheduler.Scheduler.tick', 'span', None, False),
    ('scheduler.Scheduler.submit', 'span', None, False),
    ('scheduler.Scheduler.cancel', 'span', None, False),
    ('scheduler.Scheduler._advance', 'span', _operation, False),
    ('scheduler.Scheduler._observe', 'span', _second, False),
    ('scheduler.Scheduler._finish', 'span', None, False),
    ('records.Records.observe', 'span', None, False),
    ('records.Records._ensure', 'span', None, False),
    ('artifacts.mkdir_durable', 'span', None, True),
    ('artifacts.Store._write', 'span', None, True),
    ('artifacts.Store._read', 'span', None, False),
    ('artifacts.Store.request', 'span', None, True),
    ('artifacts.Store.transition', 'span', _second, True),
    ('artifacts.Store.event', 'span', None, True),
    ('artifacts.Store.references', 'span', None, False),
    ('artifacts.Store.generation_update', 'span', None, True),
    ('artifacts.Store.provenance', 'span', None, True),
    ('artifacts.Store.allocate', 'span', None, True),
    ('artifacts.Store.launch', 'span', None, True),
    ('artifacts.Store.artifact_state', 'span', None, True),
    ('artifacts.Store.application_prepare', 'span', None, True),
    ('artifacts.Store.application_update', 'span', None, True),
    ('artifacts.Store.application_read', 'span', None, True),
    ('artifacts.Store.application_windows', 'span', None, True),
    ('artifacts.Store.application_processes', 'span', None, True),
    ('artifacts.Store.window_observation', 'span', None, True),
    ('artifacts.Store.summarize_applications', 'span', None, True),
    ('artifacts.Store.worker_identity', 'span', None, True),
    ('artifacts.Store.open_log', 'span', None, True),
    ('artifacts.Store.log_path', 'span', None, True),
    ('lifecycle.atomic', 'span', None, True),
    ('lifecycle.read_metadata', 'span', None, True),
    ('children.Children.start', 'span', None, True),
    ('children.Children.poll', 'span', None, False),
    ('desktop.Desktop.tick', 'span', None, False),
    ('desktop.Desktop.launch', 'span', None, True),
    ('readiness.Readiness.tick', 'span', None, False),
    ('readiness.Readiness._record', 'span', None, False),
    ('private_bus.PrivateBus.__init__', 'span', None, True),
    ('private_bus.PrivateBus._connected', 'span', None, False),
    ('private_bus.PrivateBus.close', 'span', None, False),
    ('private_bus.PrivateBus.call', 'bus', None, False),
    ('input_connection.Input.on_fd', 'span', None, False),
    ('input_connection.Input.drain_once', 'span', None, False),
    ('input_connection.Input.tick', 'span', None, False),
    ('input_connection.Input.press', 'input', None, False),
    ('input_connection.Input.release', 'input', None, False),
    ('input_connection.Input.scroll', 'input', None, False),
    ('input_connection.Input.move', 'input', None, False),
    ('input_actions.InputTask.watch_focus', 'watch', None, False),
    ('targeting.TargetTask.step', 'span', None, False),
    ('windows.Query.step', 'span', _phase, False),
    ('windows.Query.cleanup', 'span', None, False),
    ('capture.Capture.__init__', 'span', None, True),
    ('capture.Capture.step', 'span', None, False),
    ('app_processes.Registry.tick', 'span', None, False),
    ('app_processes.Application.observe', 'span', None, False),
    ('app_processes.Application.scan_turn', 'span', None, False),
    ('app_processes.Application.persist', 'span', None, False),
    ('title_regex.spawn', 'span', None, True),
    ('watchdog.Watchdog.tick', 'span', None, False),
    ('shutdown.Shutdown.tick', 'span', None, False),
)


def install(store, GLib, environ=os.environ):
    """A running Profiler when ENV is '1', else None with nothing wrapped."""
    if environ.get(ENV) != '1':
        return None
    try:
        return Profiler(store, GLib)
    except Exception:
        return None


def bucket(ms):
    """Histogram bucket: 0.1ms below 10ms, 1ms below 100ms, then 10ms."""
    if ms < 10:
        return math.floor(ms * 10) / 10
    if ms < 100:
        return float(math.floor(ms))
    return float(math.floor(ms / 10) * 10)


def _ms(seconds):
    return round(seconds * 1000, 3)


class Profiler:
    def __init__(self, store, GLib, *, clock=time.monotonic):
        self.clock, self.GLib = clock, GLib
        self.thread = threading.get_ident()
        self.active = True
        self.stack = []        # [path, start, child seconds]
        self.spans = []        # Completed spans of the current root: (depth, name, start, seconds, self)
        self.paths = {}        # path -> [count, seconds, self seconds, max seconds, max self]
        self.roots = {}        # root name -> [count, seconds, max seconds]
        self.recent = deque(maxlen=RECENT)
        self.hist = {}
        self.samples = 0
        self.max_late = 0.0
        self.last_probe = None
        self.next_summary = 0.0
        self.root_context = {}
        self.gc_started = None
        self.importing = 0
        self.import_gc = 0.0  # GC inside the import being timed, already counted on its own.
        self.scheduler = self.input = None
        self.restore = []
        self.fd = None
        self.source = None
        self.pending = {}
        for target in TARGETS:
            module = PACKAGE + '.' + target[0].split('.', 1)[0]
            self.pending.setdefault(module, []).append(target)
        with store.directory('logs') as logs:
            self.fd = os.open(FILENAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW
                              | os.O_CLOEXEC, 0o600, dir_fd=logs)
        try:
            self._patch(os, 'fsync', self._span('os.fsync', os.fsync, site=True))
            self.original_import = builtins.__import__
            self._patch(builtins, '__import__', self._import)
            gc.callbacks.append(self._gc)
            self._patch_loaded()
            self.source = GLib.timeout_add(PROBE_MS, self.probe)
        except BaseException:
            self.active = False
            self.close()
            raise
        self._write({'kind': 'start', 't': self.clock(), 'pid': os.getpid(), 'generation': store.generation,
                     'probe_ms': PROBE_MS, 'stall_ms': _ms(STALL), 'detail_ms': _ms(DETAIL)})

    # Wrapping ---------------------------------------------------------------

    def _patch(self, owner, attribute, replacement):
        self.restore.append((owner, attribute, owner.__dict__[attribute]))
        setattr(owner, attribute, replacement)

    def _patch_loaded(self):
        """Wrap targets in modules that have finished importing; the rest wait for their import."""
        for name in tuple(self.pending):
            module = sys.modules.get(name)
            if module is None or getattr(getattr(module, '__spec__', None), '_initializing', False):
                continue
            for target, kind, label, site in self.pending.pop(name):
                parts = target.split('.')[1:]
                owner = module
                for part in parts[:-1]:
                    owner = getattr(owner, part)
                attribute = parts[-1]
                function = owner.__dict__[attribute]
                display = '.'.join(parts)
                if kind == 'span':
                    keep = 'scheduler' if display == 'Scheduler.tick' else None
                    wrapped = self._span(display, function, label=label, site=site, keep=keep)
                else:
                    wrapped = getattr(self, '_' + kind)(display, function)
                self._patch(owner, attribute, wrapped)

    def _span(self, name, function, *, label=None, site=False, keep=None):
        profiler = self

        @functools.wraps(function)
        def timed(*args, **kwargs):
            if threading.get_ident() != profiler.thread or not profiler.active:
                return function(*args, **kwargs)
            element = name
            if label is not None:
                try:
                    element += '[' + str(label(args, kwargs)) + ']'
                except Exception:
                    pass
            if site:
                frame = sys._getframe(1)
                element += '@' + os.path.basename(frame.f_code.co_filename) + ':' + str(frame.f_lineno)
            if keep is not None:
                setattr(profiler, keep, args[0])
            profiler.enter(element)
            try:
                return function(*args, **kwargs)
            finally:
                profiler.exit()
        return timed

    def _input(self, name, function):
        """Input emission: a span, then an input record once it returns."""
        timed = self._span(name, function)
        profiler, event = self, name.rsplit('.', 1)[1]

        @functools.wraps(function)
        def emitted(owner, *args, **kwargs):
            if threading.get_ident() != profiler.thread or not profiler.active:
                return function(owner, *args, **kwargs)
            profiler.input = owner
            held = sum(len(device.held) for device in owner.devices.values())
            result = timed(owner, *args, **kwargs)
            if event != 'release' or held:
                profiler.event(event, held=held)
            return result
        return emitted

    def _watch(self, name, function):
        """Focus recheck start and end, from InputTask.watch_focus state."""
        timed = self._span(name, function)
        profiler = self

        @functools.wraps(function)
        def watched(task, *args, **kwargs):
            if threading.get_ident() != profiler.thread or not profiler.active:
                return function(task, *args, **kwargs)
            idle, due = task.recheck is None, task.next_recheck
            ended = 'recheck_failed'
            try:
                result = timed(task, *args, **kwargs)
                ended = 'recheck_done'
                return result
            finally:
                if idle and task.recheck is not None:
                    profiler.event('recheck_start', due=due)
                elif not idle and (task.recheck is None or ended == 'recheck_failed'):
                    profiler.event(ended)
        return watched

    def _complete(self, name, function):
        """Admission.complete: a span, then the accepted response's outcome."""
        timed = self._span(name, function)
        profiler = self

        @functools.wraps(function)
        def complete(admission, *args, **kwargs):
            terminal = admission.terminal
            result = timed(admission, *args, **kwargs)
            if not terminal and threading.get_ident() == profiler.thread and profiler.active:
                payload = admission.final_payload or {}
                error = payload.get('error') or {}
                request = admission.request
                profiler._write({'kind': 'done', 't': profiler.clock(), 'op': request.operation,
                                 'rid': request.request_id[:8], 'ok': payload.get('ok'), 'code': error.get('code'),
                                 'admitted': admission.admitted_at, 'deadline': admission.deadline})
            return result
        return complete

    def _bus(self, name, function):
        """PrivateBus.call: a span for the call, and its reply callback becomes a root span."""
        profiler = self

        @functools.wraps(function)
        def call(*args, **kwargs):
            if threading.get_ident() != profiler.thread or not profiler.active or len(args) != 10:
                return function(*args, **kwargs)
            method = str(args[5])
            args = list(args)
            reply = profiler._span('dbus.reply[' + method + ']', args[9])
            args[9] = reply
            profiler.enter(name + '[' + method + ']')
            try:
                return function(*args, **kwargs)
            finally:
                profiler.exit()
        return call

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if threading.get_ident() != self.thread or not self.active or self.importing:
            return self.original_import(name, globals, locals, fromlist, level)
        self.importing += 1
        self.import_gc = 0.0
        start = self.clock()
        try:
            return self.original_import(name, globals, locals, fromlist, level)
        finally:
            self.importing -= 1
            seconds = self.clock() - start
            if seconds >= FAST_IMPORT:
                importer = globals.get('__name__', '?') if isinstance(globals, dict) else '?'
                self._leaf('import[' + importer + ':' + '.' * level + name + ']', start, seconds,
                           seconds - self.import_gc)
            if self.pending:
                try:
                    self._patch_loaded()
                except Exception:
                    self.pending.clear()

    def _gc(self, phase, info):
        if threading.get_ident() != self.thread or not self.active:
            return
        if phase == 'start':
            self.gc_started = self.clock()
        elif self.gc_started is not None:
            start, self.gc_started = self.gc_started, None
            seconds = self.clock() - start
            if self.importing:
                self.import_gc += seconds
            self._leaf('gc[' + str(info.get('generation')) + ']', start, seconds)

    # Accounting -------------------------------------------------------------

    def enter(self, element):
        if self.stack:
            self.stack.append([self.stack[-1][0] + '>' + element, self.clock(), 0.0])
        else:
            self.root_context = self.context()
            self.stack.append([element, self.clock(), 0.0])

    def exit(self):
        path, start, children = self.stack.pop()
        end = self.clock()
        seconds = end - start
        if self.stack:
            self.stack[-1][2] += seconds
        self._account(path, seconds, seconds - children)
        element = path.rsplit('>', 1)[-1]
        self.spans.append((len(self.stack), element, start, seconds, seconds - children))
        if not self.stack:
            self._root(element, start, end)

    def _leaf(self, element, start, seconds, own=None):
        """A measured interval that was never on the stack: GC or an import (own excludes GC inside it)."""
        own = seconds if own is None else own
        if not self.stack:
            self._account(element, seconds, own)
            self.spans.append((0, element, start, seconds, own))
            self._root(element, start, start + seconds)
            return
        parent = self.stack[-1]
        parent[2] += own  # Any GC inside was added when it ended.
        self._account(parent[0] + '>' + element, seconds, own)
        self.spans.append((len(self.stack), element, start, seconds, own))

    def _account(self, path, seconds, own):
        entry = self.paths.get(path)
        if entry is None:
            self.paths[path] = [1, seconds, own, seconds, own]
            return
        entry[0] += 1
        entry[1] += seconds
        entry[2] += own
        if seconds > entry[3]:
            entry[3] = seconds
        if own > entry[4]:
            entry[4] = own

    def _root(self, element, start, end):
        seconds = end - start
        self.recent.append((start, end, element))
        entry = self.roots.get(element)
        if entry is None:
            self.roots[element] = [1, seconds, seconds]
        else:
            entry[0] += 1
            entry[1] += seconds
            entry[2] = max(entry[2], seconds)
        if seconds >= STALL:
            spans = sorted(([depth, name, _ms(at - start), _ms(length), _ms(own)]
                            for depth, name, at, length, own in self.spans if length >= DETAIL),
                           key=lambda span: (span[2], span[0]))
            self._write({'kind': 'stall', 't': start, 'ms': _ms(seconds), 'root': element, 'spans': spans}
                        | self.root_context)
        self.spans.clear()

    def context(self):
        """What time-sensitive work the owner has now: held input, the active request and its phase."""
        context = {}
        try:
            if self.input is not None:
                context['held'] = sum(len(device.held) for device in self.input.devices.values())
            work = getattr(self.scheduler, 'active', None)
            if work is not None:
                context['active'] = work.request.operation
                context['rid'] = work.request.request_id[:8]
                phase = getattr(work.task, 'phase', None)
                if isinstance(phase, str):
                    context['phase'] = phase
        except Exception:
            pass
        return context

    def event(self, name, **fields):
        record = {'kind': 'input', 'ev': name, 't': self.clock()} | self.context() | fields
        work = getattr(self.scheduler, 'active', None)
        task = getattr(work, 'task', None)
        for key in ('hold', 'gap'):
            value = getattr(task, key, None)
            if type(value) in (int, float):
                record[key] = value
        record['path'] = self.stack[-1][0] if self.stack else None
        self._write(record)

    # Probe and output -------------------------------------------------------

    def probe(self):
        now = self.clock()
        last, self.last_probe = self.last_probe, now
        if last is not None and self.active:
            late = now - last - PROBE_MS / 1000
            key = bucket(late * 1000)
            self.hist[key] = self.hist.get(key, 0) + 1
            self.samples += 1
            self.max_late = max(self.max_late, late)
            if late >= STALL:
                roots = [[element, _ms(start - now), _ms(end - start)]
                         for start, end, element in self.recent if end > last]
                self._write({'kind': 'late', 't': now, 'ms': _ms(late), 'roots': roots} | self.context())
        if now >= self.next_summary:
            self.next_summary = now + SUMMARY_SECONDS
            self.summary()
        return True

    def summary(self, final=False):
        self._write({'kind': 'summary', 't': self.clock(), 'final': final,
                     'probe': {'interval_ms': PROBE_MS, 'samples': self.samples, 'max_ms': _ms(self.max_late),
                               'hist': {str(key): count for key, count in sorted(self.hist.items())}},
                     'roots': {name: [count, _ms(seconds), _ms(worst)]
                               for name, (count, seconds, worst) in self.roots.items()},
                     'paths': {path: [count, _ms(seconds), _ms(own), _ms(worst), _ms(worst_own)]
                               for path, (count, seconds, own, worst, worst_own) in self.paths.items()}})

    def _write(self, record):
        if self.fd is None:
            return
        try:
            os.write(self.fd, (json.dumps(record, separators=(',', ':'), default=str) + '\n').encode())
        except Exception:
            self.active = False
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self):
        """Final summary, then unwrap everything (tests install and close more than once)."""
        if self.active:
            self.summary(final=True)
        self.active = False
        if self.source is not None:
            try:
                self.GLib.source_remove(self.source)
            except Exception:
                pass
            self.source = None
        if self._gc in gc.callbacks:
            gc.callbacks.remove(self._gc)
        for owner, attribute, original in reversed(self.restore):
            setattr(owner, attribute, original)
        self.restore.clear()
        self.pending.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
