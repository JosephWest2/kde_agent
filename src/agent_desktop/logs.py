"""Public `logs`: where each session and application log is, plus a bounded tail.

Never returns a whole log. Each log contributes at most TAIL_BYTES from its end,
split into lines, of which the last `tail` are returned. ANSI color codes and
other control characters are removed from the returned lines, not the files.
"""
from __future__ import annotations

import os
import re
import stat
import time

from .contracts import ContractError

TAIL_BYTES = 16384
SESSION_SOURCES = ('worker', 'compositor', 'bus')
CONTROL = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f]')


def tail(path, lines, store=None):
    """(bytes, last LINES lines, truncated) of the regular file at PATH.

    With STORE, PATH must be inside its generation and every directory on the
    way is opened without following symlinks.
    """
    fd = (store.open_owned(path) if store is not None
          else os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ContractError('artifact_failed', 'Log is not an owned regular file.')
        size = info.st_size
        start = max(0, size - TAIL_BYTES)
        raw = os.pread(fd, size - start, start) if lines else b''
    finally:
        os.close(fd)
    text = raw.decode('utf-8', 'replace').split('\n')
    if text and text[-1] == '':
        text.pop()
    if start and text:
        text.pop(0)  # Partial first line.
    if not lines:
        return size, [], size > 0
    shown = text[-lines:]
    return size, [CONTROL.sub('', line) for line in shown], start > 0 or len(text) > len(shown)


class LogsTask:
    cleanup_seconds = .1

    def __init__(self, request, context, store, applications, healthy):
        self.request, self.context, self.store = request, context, store
        self.applications, self.healthy = applications, healthy

    def step(self, now):
        if time.monotonic() >= self.context.work.admission.deadline:
            raise ContractError('timeout', 'Logs deadline expired.')
        self.healthy()
        source, lines = self.request.arguments['source'], self.request.arguments['tail']
        app = self.request.arguments.get('app')
        if app is not None and source not in ('all', 'application'):
            raise ContractError('invalid_arguments', '--app selects application logs; use --source all or application.',
                                context={'field': 'source'})
        entries = []
        if source in ('all', 'application'):
            entries += self.application_logs(app)
        if app is None:
            for name in SESSION_SOURCES:
                if source in ('all', name):
                    entries.append({'source': name, 'path': str(self.store.path / 'logs' / (name + '.log')),
                                    'complete': False})
        for entry in entries:
            entry['bytes'], entry['tail'], entry['truncated'] = tail(entry['path'], lines, self.store)
        return {'logs': entries, 'tail': lines}

    def application_logs(self, app):
        explicit = app is not None
        app = app or getattr(self.applications, 'latest', None)
        if app is None:
            return []
        try:
            record = self.applications.lookup(app)
        except ContractError as error:
            if explicit or error.code != 'target_not_found':
                raise
            return []  # The latest launch failed before recording anything.
        active = getattr(self.applications, 'active', None)
        if active is not None and active.handle == app:
            complete = active.completed  # Verified empty cgroup, not just a launch outcome.
        else:
            # Retired applications were verified empty before the registry let go of them.
            complete = record['state'] in ('all-exited', 'launch-failed')
        return [{'source': 'application', 'stream': stream, 'application': app,
                 'path': record['logs'][stream], 'complete': complete}
                for stream in ('stdout', 'stderr') if stream in record['logs']]

    def request_cancel(self, reason):
        pass

    def cleanup(self, now):
        return True
