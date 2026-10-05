"""Title matching for `wait --for title`; regex searches never run on the owner.

Substring matching is a plain `in` on a title of at most 4096 characters.
`--regex` uses Python's `re`, which can backtrack exponentially inside one C
call that holds the GIL, so the search runs in a short-lived child
(`python -I -c`, empty environment, no inherited descriptors beyond /dev/null),
like the kdotool query children: started through the worker's `Children`, which
owns reaping, polled by return code without blocking, aborted on cancel and
killed with the worker. The child arms a 100ms CPU-time timer (ITIMER_PROF)
around the search; its default action terminates the child, so a runaway
pattern ends as SIGPROF however long the interpreter took to start. A child
starts only when the window's title changes (about 15ms each).

Compiling is bounded by the 256-character pattern and happens at validation.
"""
import json
import re
import signal
import sys
import time
import warnings

from .contracts import ContractError

MAX_PATTERN = 256
# CPU seconds one search may use. A real search of a <=4096-character title
# with a <=256-character pattern takes well under 1ms; 100ms leaves >100x
# headroom yet fails a runaway pattern within about one poll interval.
MATCH_CPU_SECONDS = .1
# Wall-clock bound for start-up plus search, for a child that is never scheduled.
CHILD_SECONDS = 2.0
CHILD_CODE = (
    'import json,re,signal,sys\n'
    'pattern,title=json.loads(sys.argv[1])\n'
    'compiled=re.compile(pattern)\n'
    f'signal.setitimer(signal.ITIMER_PROF,{MATCH_CPU_SECONDS!r})\n'
    'found=compiled.search(title) is not None\n'
    'signal.setitimer(signal.ITIMER_PROF,0)\n'
    'sys.exit(10 if found else 11)\n'
)


def rejected(reason, **context):
    raise ContractError('invalid_arguments', 'Invalid --match; see docs/CLI.md.',
                        context={'field': 'match', 'reason': reason} | context)


def validate(text, regex):
    """Request-time check; the pattern text is never echoed."""
    if not isinstance(text, str) or not text or '\0' in text:
        rejected('empty' if text == '' else 'invalid')
    if len(text) > MAX_PATTERN:
        rejected('too_long', maximum=MAX_PATTERN)
    if regex:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')  # e.g. FutureWarning for '[[': never echo the pattern
                re.compile(text)
        except re.error as error:
            rejected('invalid_regex', position=error.pos)
        except (OverflowError, RecursionError, ValueError):
            rejected('invalid_regex', position=None)
    return text


class Search:
    """One title check. step() returns None while pending, then True or False."""

    def __init__(self, children, pattern, regex, title):
        self.child = None
        self.result = None
        if not isinstance(title, str) or not title:
            self.result = False  # A null or empty title never matches.
        elif not regex:
            self.result = pattern in title
        else:
            self.deadline = time.monotonic() + CHILD_SECONDS
            payload = json.dumps([pattern, title], ensure_ascii=True)
            self.child = children.start([sys.executable, '-I', '-c', CHILD_CODE, payload], env={}, cwd='/')

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
        # Distinct codes: an uncaught exception in the child exits 1.
        if code in (10, 11):
            self.result = code == 10
            return self.result
        if code == -signal.SIGPROF:
            rejected('pattern_too_slow', cpu_seconds=MATCH_CPU_SECONDS)
        raise ContractError('internal_error', 'Title regex helper failed.',
                            context={'reason': 'regex_helper_failed'})

    def abort(self):
        if self.child is not None and self.child.returncode is None:
            self.child.abort()

    def reaped(self):
        return self.child is None or self.child.returncode is not None
