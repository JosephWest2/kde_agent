"""Title wait arguments, request-time --match validation and the regex helper child."""
import signal
import subprocess
import sys
import time
import unittest

from agent_desktop.contracts import ContractError, make_request
from agent_desktop.title_regex import CHILD_CODE, MAX_PATTERN, Search, validate

GEN = 'a' * 32
REF = GEN + ':12345678-1234-1234-1234-123456789ab0'


class Children:
    """Minimal stand-in for agent_desktop.children: Popen with the same isolation."""
    def __init__(self):
        self.started = []
    def start(self, argv, *, env, cwd):
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, cwd=cwd, close_fds=True)
        child = Child(process)
        self.started.append(child)
        return child


class Child:
    def __init__(self, process):
        self.process = process
    @property
    def returncode(self):
        return self.process.poll()
    def abort(self):
        self.process.kill()


def answer(pattern, title, regex=True):
    search = Search(Children(), pattern, regex, title)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        found = search.step()
        if found is not None:
            return found
        time.sleep(.005)
    raise AssertionError('no answer')


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
    def reason(self, text, regex=True):
        with self.assertRaises(ContractError) as caught:
            validate(text, regex)
        self.assertEqual((caught.exception.code, caught.exception.context['field']), ('invalid_arguments', 'match'))
        if text:
            self.assertNotIn(text, caught.exception.message + repr(caught.exception.context))
        return caught.exception.context

    def test_length_and_emptiness_are_bounded_in_both_modes(self):
        for regex in (False, True):
            validate('a' * MAX_PATTERN, regex)
            self.assertEqual(self.reason('a' * (MAX_PATTERN + 1), regex)['reason'], 'too_long')
            self.assertEqual(self.reason('', regex)['reason'], 'empty')

    def test_compile_errors_are_invalid_arguments_without_echo(self):
        for pattern, position in (('(unclosed', 0), ('a)', 1), ('[z-a]', 1), ('a**', 2), (r'\1x', 1)):
            with self.subTest(pattern=pattern):
                context = self.reason(pattern)
                self.assertEqual((context['reason'], context['position']), ('invalid_regex', position))

    def test_full_python_syntax_is_accepted(self):
        for pattern in (r'(?i)^saved\b', r'(?<=v)\d+(?:\.\d+){1,3}', r'^(a|b)\1$', '[[:x]', 'a*'):
            with self.subTest(pattern=pattern):
                validate(pattern, True)


class SearchTests(unittest.TestCase):
    def test_substring_is_case_sensitive_in_process_and_titles_must_be_nonempty(self):
        children = Children()
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
                 ('x\0y', 'x\0y', True)]
        for pattern, title, expected in cases:
            with self.subTest(pattern=pattern, title=title):
                self.assertEqual(answer(pattern, title), expected)

    def test_catastrophic_pattern_is_stopped_by_the_cpu_timer(self):
        children = Children()
        search = Search(children, '(a+)+$', True, 'a' * 64 + 'b')
        started = time.monotonic()
        with self.assertRaises(ContractError) as caught:
            while time.monotonic() - started < 5:
                search.step()
                time.sleep(.005)
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('invalid_arguments', 'pattern_too_slow'))
        self.assertEqual(children.started[0].process.returncode, -signal.SIGPROF)
        self.assertLess(time.monotonic() - started, 2)

    def test_child_failure_is_internal_error_not_no_match(self):
        children = Children()
        search = Search(children, 'a', True, 'a')
        search.child.process.kill()
        search.child.process.wait()
        with self.assertRaises(ContractError) as caught:
            search.step()
        self.assertEqual((caught.exception.code, caught.exception.context['reason']),
                         ('internal_error', 'regex_helper_failed'))

    def test_child_runs_isolated_with_only_standard_descriptors(self):
        probe = CHILD_CODE.replace("import json,re,signal,sys\n",
                                   "import json,os,re,signal,sys\n"
                                   "assert sorted(os.listdir('/proc/self/fd')) in (['0','1','2'],['0','1','2','3'])\n"
                                   "assert sys.flags.isolated and set(os.environ) <= {'LC_CTYPE'}\n")  # locale coercion
        result = subprocess.run([sys.executable, '-I', '-c', probe, '["a","a"]'], env={}, cwd='/',
                                stdin=subprocess.DEVNULL, close_fds=True)
        self.assertEqual(result.returncode, 10)


if __name__ == '__main__':
    unittest.main()
