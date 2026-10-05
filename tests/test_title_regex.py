"""Title wait arguments, request-time --match validation and the regex helper child."""
import json
import os
import signal
import subprocess
import time
import unittest
from unittest.mock import patch

from agent_desktop import children as owned
from agent_desktop import title_regex
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.protocol import request_from_wire
from agent_desktop.title_regex import MAX_PATTERN, Search, compile_check, spawn, validate

GEN = 'a' * 32
REF = GEN + ':12345678-1234-1234-1234-123456789ab0'
# About 150ms to compile on a desktop CPU though only 254 characters long.
SLOW_COMPILE = '(?i)' + '[a-\uffff]' * 50
NO_COMPILE = patch('re.compile', side_effect=AssertionError('compiled outside the helper child'))


class Children(owned.Children):
    """The worker's Children, recording what was started."""
    def __init__(self):
        super().__init__()
        self.started, self.argv = [], []
    def start(self, argv, **options):
        child = super().start(argv, **options)
        self.started.append(child)
        self.argv.append(argv)
        return child


def settle(children, search, limit=5):
    """Poll like the worker's tick until the search answers (or raises)."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        children.poll()
        found = search.step()
        if found is not None:
            return found
        time.sleep(.005)
    raise AssertionError('no answer')


def answer(pattern, title, regex=True):
    children = Children()
    try:
        return settle(children, Search(children, pattern, regex, title))
    finally:
        children.close()


def fds():
    return set(os.listdir('/proc/self/fd'))


class ArgumentTests(unittest.TestCase):
    def request(self, **arguments):
        return make_request('wait', caller_cwd='/tmp', arguments=arguments)

    def test_title_and_gone_take_one_window_and_normalize_match_fields(self):
        request = self.request(condition='title', window=REF, match='Saved')
        self.assertEqual((request.arguments['match'], request.arguments['regex']), ('Saved', False))
        self.assertEqual(request.expected_generation, GEN)
        self.assertIs(self.request(condition='title', window=REF, match='^a', regex=True).arguments['regex'], True)
        gone = self.request(condition='gone', window=REF, match=None, regex=False)
        self.assertEqual(set(gone.arguments), {'condition', 'window'})
        self.assertEqual(set(self.request(condition='focus', window=REF).arguments), {'condition', 'window'})

    def test_invalid_combinations_name_the_field(self):
        cases = [
            ({'condition': 'title', 'window': REF}, 'match'),
            ({'condition': 'title', 'window': REF, 'match': ''}, 'match'),
            ({'condition': 'title', 'window': REF, 'match': 'a', 'regex': 'yes'}, 'regex'),
            ({'condition': 'title', 'app': GEN + ':app', 'match': 'a'}, 'target'),
            ({'condition': 'gone', 'app': GEN + ':app'}, 'target'),
            ({'condition': 'gone', 'window': REF, 'match': 'a'}, 'match'),
            ({'condition': 'gone', 'window': REF, 'regex': True}, 'match'),
            ({'condition': 'exit', 'app': GEN + ':app', 'match': 'a'}, 'match'),
            ({'condition': 'vanish', 'window': REF}, 'for'),
        ]
        for arguments, field in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ContractError) as caught:
                self.request(**arguments)
            self.assertEqual(caught.exception.code, 'invalid_arguments')
            self.assertEqual(caught.exception.context['field'], field)

    def test_generation_mismatch_is_refused_before_dispatch(self):
        with self.assertRaises(ContractError) as caught:
            make_request('wait', caller_cwd='/tmp', expected_generation='b' * 32,
                         arguments={'condition': 'gone', 'window': REF})
        self.assertEqual(caught.exception.code, 'generation_mismatch')


class ValidationTests(unittest.TestCase):
    def reason(self, check, text):
        with self.assertRaises(ContractError) as caught:
            check(text)
        self.assertEqual((caught.exception.code, caught.exception.context['field']), ('invalid_arguments', 'match'))
        if text:
            self.assertNotIn(text, caught.exception.message + repr(caught.exception.context))
        return caught.exception.context

    def test_length_and_emptiness_are_bounded(self):
        validate('a' * MAX_PATTERN)
        self.assertEqual(self.reason(validate, 'a' * (MAX_PATTERN + 1))['reason'], 'too_long')
        self.assertEqual(self.reason(validate, '')['reason'], 'empty')
        self.assertEqual(self.reason(validate, 'a\0b')['reason'], 'invalid')

    def test_worker_side_validation_never_compiles(self):
        # The worker decodes requests with request_from_wire on its owner thread.
        for pattern in ('(unclosed', SLOW_COMPILE, 'a' * MAX_PATTERN):
            with self.subTest(pattern=pattern[:12]), NO_COMPILE:
                request = make_request('wait', caller_cwd='/tmp', arguments={
                    'condition': 'title', 'window': REF, 'match': pattern, 'regex': True})
                decoded = request_from_wire(json.loads(json.dumps(request.payload())))
                self.assertEqual((decoded.arguments['match'], decoded.arguments['regex']), (pattern, True))

    def test_cli_compile_errors_are_invalid_arguments_without_echo(self):
        for pattern, position in (('(unclosed', 0), ('a)', 1), ('[z-a]', 1), ('a**', 2), (r'\1x', 1)):
            with self.subTest(pattern=pattern):
                context = self.reason(compile_check, pattern)
                self.assertEqual((context['reason'], context['position']), ('invalid_regex', position))

    def test_full_python_syntax_is_accepted(self):
        for pattern in (r'(?i)^saved\b', r'(?<=v)\d+(?:\.\d+){1,3}', r'^(a|b)\1$', '[[:x]', 'a*'):
            with self.subTest(pattern=pattern):
                compile_check(pattern)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.children = Children()
        self.addCleanup(self.children.close)

    def failure(self, search):
        with self.assertRaises(ContractError) as caught:
            settle(self.children, search)
        self.children.poll()
        self.assertEqual(self.children.owned, set())
        return caught.exception

    def test_substring_is_case_sensitive_in_process_and_titles_must_be_nonempty(self):
        children = self.children
        self.assertTrue(Search(children, 'Saved', False, 'Report - Saved').step())
        self.assertFalse(Search(children, 'saved', False, 'Report - Saved').step())
        self.assertTrue(Search(children, '.*', False, 'a.*b').step())  # literal, never a pattern
        for title in (None, ''):
            self.assertFalse(Search(children, 'a*', True, title).step())
        self.assertEqual(children.started, [])

    def test_regex_is_python_re_search(self):
        cases = [('^New Document', 'New Document (Draft) - Text Editor', True),
                 ('^New Document', 'Draft: New Document', False),
                 (r'(?i)SAVED$', 'report saved', True), (r'^(\w+) \1$', 'ab ab', True),
                 ('a*', 'anything', True),  # an empty-matching pattern matches every non-empty title
                 ('x\0y', 'x\0y', True), ('\U0001f600$', 'smile \U0001f600', True)]
        for pattern, title, expected in cases:
            with self.subTest(pattern=pattern, title=title):
                self.assertEqual(answer(pattern, title), expected)

    def test_catastrophic_pattern_is_stopped_by_the_cpu_timer(self):
        search = Search(self.children, '(a+)+$', True, 'a' * 64 + 'b')
        started = time.monotonic()
        error = self.failure(search)
        self.assertEqual((error.code, error.context['reason']), ('invalid_arguments', 'pattern_too_slow'))
        self.assertEqual(self.children.started[0].returncode, -signal.SIGPROF)
        self.assertLess(time.monotonic() - started, 2)

    def test_compile_runs_only_in_the_child_under_the_cpu_timer(self):
        # The timer is armed before compiling, so a slow compile is bounded too.
        # A lower bound keeps this independent of the host's speed.
        with NO_COMPILE, patch.object(title_regex, 'MATCH_CPU_SECONDS', .02):
            error = self.failure(Search(self.children, SLOW_COMPILE, True, 'x'))
        self.assertEqual((error.code, error.context['reason'], error.context['cpu_seconds']),
                         ('invalid_arguments', 'pattern_too_slow', .02))
        self.assertEqual(self.children.started[0].returncode, -signal.SIGPROF)
        with NO_COMPILE, patch.object(title_regex, 'MATCH_CPU_SECONDS', 10):
            self.assertTrue(settle(self.children, Search(self.children, SLOW_COMPILE, True, 'x' * 50)))

    def test_pattern_the_cli_never_checked_is_invalid_regex_from_the_child(self):
        # A client other than the CLI can send any pattern within the length bound.
        for pattern in ('(unclosed', 'a**', '[z-a]', r'\1x'):
            with self.subTest(pattern=pattern), NO_COMPILE:
                error = self.failure(Search(self.children, pattern, True, 'secret title'))
                self.assertEqual((error.code, error.context['field'], error.context['reason']),
                                 ('invalid_arguments', 'match', 'invalid_regex'))
                self.assertNotIn(pattern, error.message + repr(error.context))
                self.assertEqual(self.children.started[-1].returncode, title_regex.INVALID)

    def test_child_failure_is_internal_error_not_no_match(self):
        search = Search(self.children, 'a', True, 'a')
        search.child.abort()
        error = self.failure(search)
        self.assertEqual((error.code, error.context['reason']), ('internal_error', 'regex_helper_failed'))


class ChannelTests(unittest.TestCase):
    """The pattern and title reach the child on a private memfd stdin, never argv."""
    PATTERN, TITLE = 'secret-pattern-4f1c', 'secret-title-9b2e'

    def setUp(self):
        self.children = Children()
        self.addCleanup(self.children.close)

    def run_probe(self, probe):
        child = spawn(self.children, probe, 5, [self.PATTERN, self.TITLE])
        end = time.monotonic() + 5
        while child.returncode is None and time.monotonic() < end:
            self.children.poll()
            time.sleep(.005)
        return child.returncode

    def test_child_sees_the_payload_on_stdin_only_and_no_other_descriptors(self):
        expected = json.dumps([self.PATTERN, self.TITLE]).encode().hex()  # The probe itself is in argv.
        probe = (
            'import json,os,sys\n'
            'fds=sorted(os.listdir("/proc/self/fd"))\n'
            'assert fds in (["0","1","2"],["0","1","2","3"]),fds\n'  # 3: listdir's own
            'assert os.readlink("/proc/self/fd/0").startswith("/memfd:agent-desktop-title")\n'
            'pattern,title=json.loads(sys.stdin.buffer.read())\n'
            f'assert [pattern,title]==json.loads(bytes.fromhex("{expected}"))\n'
            'cmdline=open("/proc/self/cmdline","rb").read()\n'
            'assert pattern.encode() not in cmdline and title.encode() not in cmdline\n'
            'assert sys.flags.isolated and os.getcwd()=="/"\n'
            'assert not {"PATH","HOME","PYTHONPATH","XDG_RUNTIME_DIR","AGENT_DESKTOP_TEST_SENTINEL"}&set(os.environ)\n'
            'sys.exit(10)\n')
        with patch.dict(os.environ, AGENT_DESKTOP_TEST_SENTINEL='1'):
            self.assertEqual(self.run_probe(probe), 10)

    def test_search_argv_never_carries_the_pattern_or_title(self):
        before = fds()
        search = Search(self.children, self.PATTERN, True, self.TITLE)
        self.assertEqual(fds(), before)  # The parent's memfd copy is closed once the child starts.
        self.assertFalse(settle(self.children, search))
        for word in self.children.argv[0]:
            self.assertNotIn('secret-', word)

    def test_children_accept_a_descriptor_but_never_a_pipe_for_stdin(self):
        with self.assertRaises(ValueError):
            self.children.start(['true'], stdin=subprocess.PIPE)
        self.assertEqual(self.children.owned, set())

    def test_descriptor_is_closed_when_the_child_cannot_start(self):
        before = fds()
        with patch.object(owned.subprocess, 'Popen', side_effect=OSError('no exec')), self.assertRaises(OSError):
            Search(self.children, 'a', True, 'a')
        self.assertEqual(fds(), before)


if __name__ == '__main__':
    unittest.main()
