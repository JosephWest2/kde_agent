"""Title matching for `wait --for title`; regex work never runs on the owner.

Substring matching is a plain `in` on a title of at most 4096 characters.
`--regex` uses Python's `re`. Both compiling (case-insensitive wide classes take
~150ms within 256 characters) and searching (exponential backtracking) can run
long inside one C call that holds the GIL, so the worker never does either:
- `validate` (shared request validation, also run by the worker on the owner)
  only checks type and length;
- `compile_check` is the CLI's early, friendly check, run in the CLI process;
- the authoritative compile and the search happen in a short-lived child
  (`python -I -c`, empty environment, no inherited descriptors) under a CPU-time
  timer (ITIMER_PROF) armed before anything else, whose default action
  terminates the child. Like the kdotool query children it is started through
  the worker's `Children` (which owns reaping), polled by return code without
  blocking, aborted on cancel and killed with the worker. A child starts only
  when the window's title changes (about 15ms each).

The pattern and title reach the child through a private memfd as its stdin,
never argv (`/proc/PID/cmdline` is world-readable). The parent closes its copy
as soon as the child is spawned, so it cannot leak into later children, and the
child's copy ends with the child on every path.
"""
import json
import os
import re
import signal
import sys
import time
import warnings

from .contracts import ContractError

MAX_PATTERN = 256
# CPU seconds one child may use to parse its input, compile and search. Real
# patterns compile and search a <=4096-character title in well under 1ms;
# 100ms leaves >100x headroom yet fails a runaway pattern within about one poll.
MATCH_CPU_SECONDS = .1
# Wall-clock bound for start-up, compile and search, for a child that is never scheduled.
CHILD_SECONDS = 2.0
MATCHED, UNMATCHED, INVALID = 10, 11, 12  # An uncaught exception in the child exits 1.
CHILD_CODE = (
    'import json,re,signal,sys\n'
    'signal.setitimer(signal.ITIMER_PROF,float(sys.argv[1]))\n'
    'pattern,title=json.loads(sys.stdin.buffer.read())\n'
    'try:\n'
    '    compiled=re.compile(pattern)\n'
    'except (re.error,OverflowError,RecursionError,ValueError):\n'
    f'    sys.exit({INVALID})\n'
    'found=compiled.search(title) is not None\n'
    'signal.setitimer(signal.ITIMER_PROF,0)\n'
    f'sys.exit({MATCHED} if found else {UNMATCHED})\n'
)


def rejected(reason, **context):
    raise ContractError('invalid_arguments', 'Invalid --match; see docs/CLI.md.',
                        context={'field': 'match', 'reason': reason} | context)


def validate(text):
    """Bounded request-time check, safe on the owner; the text is never echoed."""
    if not isinstance(text, str) or not text or '\0' in text:
        rejected('empty' if text == '' else 'invalid')
    if len(text) > MAX_PATTERN:
        rejected('too_long', maximum=MAX_PATTERN)
    return text


def compile_check(text):
    """The CLI's early compile, never run by the worker; the child compiles again."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # e.g. FutureWarning for '[[': never echo the pattern
            re.compile(text)
    except re.error as error:
        rejected('invalid_regex', position=error.pos)
    except (OverflowError, RecursionError, ValueError):
        rejected('invalid_regex', position=None)


def spawn(children, code, cpu_seconds, value):
    """Start `python -I -c code cpu_seconds` with JSON `value` on a private stdin."""
    fd = os.memfd_create('agent-desktop-title', os.MFD_CLOEXEC)
    try:
        data = memoryview(json.dumps(value, ensure_ascii=True).encode())
        while data:  # memory-backed: never blocks; bounded by the 256/4096 limits
            data = data[os.write(fd, data):]
        os.lseek(fd, 0, os.SEEK_SET)  # The child's stdin shares this offset.
        return children.start([sys.executable, '-I', '-c', code, repr(float(cpu_seconds))],
                              stdin=fd, env={}, cwd='/')
    finally:
        os.close(fd)


class Search:
    """One title check. step() returns None while pending, then True or False."""

    def __init__(self, children, pattern, regex, title):
        self.child = None
        self.result = None
        self.cpu_seconds = MATCH_CPU_SECONDS
        if not isinstance(title, str) or not title:
            self.result = False  # A null or empty title never matches.
        elif not regex:
            self.result = pattern in title
        else:
            self.deadline = time.monotonic() + CHILD_SECONDS
            self.child = spawn(children, CHILD_CODE, self.cpu_seconds, [pattern, title])

    def step(self):
        if self.result is not None:
            return self.result
        code = self.child.returncode
        if code is None:
            if time.monotonic() >= self.deadline:
                self.abort()
                raise ContractError('internal_error', 'Title regex helper did not answer.',
                                    context={'reason': 'regex_helper_unresponsive'})
            return None
        if code in (MATCHED, UNMATCHED):
            self.result = code == MATCHED
            return self.result
        if code == INVALID:
            # Requests sent straight over the transport are first compiled here.
            rejected('invalid_regex', position=None)
        if code == -signal.SIGPROF:
            rejected('pattern_too_slow', cpu_seconds=self.cpu_seconds)
        raise ContractError('internal_error', 'Title regex helper failed.',
                            context={'reason': 'regex_helper_failed'})

    def abort(self):
        if self.child is not None and self.child.returncode is None:
            self.child.abort()

    def reaped(self):
        return self.child is None or self.child.returncode is not None
