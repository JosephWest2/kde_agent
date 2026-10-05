"""Title wait arguments and the linear-time title matcher (checked against Python's re)."""
import random
import re
import time
import unittest
import warnings

from agent_desktop.contracts import ContractError, make_request
from agent_desktop.title_match import MAX_PATTERN, compile_match

GEN = 'a' * 32
REF = GEN + ':12345678-1234-1234-1234-123456789ab0'


def run(matcher, title):
    search = matcher.search(title)
    steps = 0
    while (found := search.step()) is None:
        steps += 1
    return found, steps


def matches(pattern, title, regex=True):
    return run(compile_match(pattern, regex), title)[0]


def reason(pattern, regex=True):
    try:
        compile_match(pattern, regex)
    except ContractError as error:
        return error
    raise AssertionError(f'{pattern!r} was accepted')


class ArgumentTests(unittest.TestCase):
    def request(self, **arguments):
        return make_request('wait', caller_cwd='/tmp', arguments=arguments)

    def test_title_and_gone_take_one_window_and_normalize_match_fields(self):
        request = self.request(condition='title', window=REF, match='Saved')
        self.assertEqual(request.arguments['match'], 'Saved')
        self.assertIs(request.arguments['regex'], False)
        self.assertEqual(request.expected_generation, GEN)
        self.assertIs(self.request(condition='title', window=REF, match='^a', regex=True).arguments['regex'], True)
        gone = self.request(condition='gone', window=REF, match=None, regex=False)
        self.assertNotIn('match', gone.arguments)
        self.assertNotIn('regex', gone.arguments)
        # Existing waits keep their exact argument shape.
        focus = self.request(condition='focus', window=REF)
        self.assertEqual(set(focus.arguments), {'condition', 'window'})

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

    def test_pattern_length_is_bounded_in_both_modes(self):
        for regex in (False, True):
            compile_match('a' * MAX_PATTERN, regex)
            with self.assertRaises(ContractError) as caught:
                compile_match('a' * (MAX_PATTERN + 1), regex)
            self.assertEqual(caught.exception.context['reason'], 'too_long')

    def test_generation_mismatch_is_refused_before_dispatch(self):
        with self.assertRaises(ContractError) as caught:
            make_request('wait', caller_cwd='/tmp', expected_generation='b' * 32,
                         arguments={'condition': 'gone', 'window': REF})
        self.assertEqual(caught.exception.code, 'generation_mismatch')


class MatcherTests(unittest.TestCase):
    def test_substring_is_case_sensitive_and_titles_must_be_nonempty(self):
        self.assertTrue(matches('Saved', 'Report — Saved', regex=False))
        self.assertFalse(matches('saved', 'Report — Saved', regex=False))
        self.assertTrue(matches('.*', 'a.*b', regex=False))  # literal, never a pattern
        for title in (None, ''):
            self.assertFalse(matches('a', title, regex=False))
            self.assertFalse(matches('a|b', title))

    def test_regex_subset(self):
        cases = [
            ('^New Document', 'New Document (Draft) - Text Editor', True),
            ('^New Document', 'Draft: New Document', False),
            ('Editor$', 'New Document - Text Editor', True),
            ('Editor$', 'Editor (2)', False),
            ('^(?:a|b)c$', 'bc', True), ('^(a|b)c$', 'abc', False),
            (r'\d+ items?', 'Inbox (12 items)', True), (r'\d+ items?', 'Inbox (no items)', False),
            ('[^ ]+\\.txt', 'notes.txt - gedit', True), ('[a-c]x', 'bx', True), ('[a-c]x', 'dx', False),
            (r'\w+\s\W', 'ab -', True), ('[]x]', ']', True), ('[x-]', '-', True), (r'a\$', 'a$', True),
            (r'[\d.]+%', 'Loading 42.5%', True), ('.', '\n', False), ('x\\n', 'x\n', True),
            ('a$', 'a\n', True),  # like Python: $ also matches before one final newline
        ]
        for pattern, title, expected in cases:
            with self.subTest(pattern=pattern, title=title):
                self.assertEqual(matches(pattern, title), expected)
                self.assertEqual(bool(re.search(pattern, title)), expected)

    def test_unsupported_or_invalid_patterns_are_rejected_with_a_reason(self):
        cases = {'(a': 'unbalanced_parenthesis', 'a)': 'unbalanced_parenthesis', '[a': 'unterminated_class',
                 '*a': 'nothing_to_repeat', 'a**': 'unsupported_quantifier', 'a*?': 'unsupported_quantifier',
                 'a{2}': 'unsupported_quantifier', '(?=a)': 'unsupported_group', '(?i)a': 'unsupported_group',
                 r'(a)\1': 'unsupported_escape', r'\bword': 'unsupported_escape', 'a\\': 'trailing_backslash',
                 'a^b': 'unsupported_syntax', 'a$b': 'unsupported_syntax', '[z-a]': 'invalid_range',
                 'a*': 'matches_empty', '^$': 'matches_empty', '(a|)': 'matches_empty',
                 '^a|b': 'anchor_with_alternation', 'a|b$': 'anchor_with_alternation', '(' * 40 + 'a' + ')' * 40: 'nesting_too_deep'}
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                error = reason(pattern)
                self.assertEqual(error.code, 'invalid_arguments')
                self.assertEqual(error.context['field'], 'match')
                self.assertEqual(error.context['reason'], expected)
                self.assertNotIn(pattern, error.message)

    def test_agrees_with_python_re_on_random_patterns(self):
        rng = random.Random(78)
        alphabet = 'ab.()|*+?[]^$\\-dwsDS:'
        checked = 0
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            for _ in range(6000):
                pattern = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 9)))
                try:
                    matcher = compile_match(pattern, True)
                except ContractError:
                    continue
                expected = re.compile(pattern)  # Every accepted pattern is valid Python.
                for _ in range(10):
                    title = ''.join(rng.choice('ab-$^ 1_.\n') for _ in range(rng.randint(1, 8)))
                    self.assertEqual(run(matcher, title)[0], bool(expected.search(title)), (pattern, title))
                checked += 1
        self.assertGreater(checked, 500)

    def test_adversarial_patterns_stay_linear_and_split_across_steps(self):
        title = 'a' * 4096
        for pattern in ('.*' * 127 + 'x', '(a|a)*' * 40 + 'x', '(?:a?)' * 42 + 'a' * 2 + 'x', r'\w*' * 84 + 'x'):
            matcher = compile_match(pattern[:MAX_PATTERN], True)
            search = matcher.search(title)
            started, steps, longest = time.perf_counter(), 0, 0
            while True:
                begun = time.perf_counter()
                found = search.step()
                longest = max(longest, time.perf_counter() - begun)
                if found is not None:
                    break
                steps += 1
            with self.subTest(pattern=pattern[:20]):
                self.assertFalse(found)
                self.assertGreater(steps, 0)
                self.assertLess(steps, 64)
                # Generous for slow CI; a backtracking engine would take far longer.
                self.assertLess(longest, .05)
                self.assertLess(time.perf_counter() - started, 1)


if __name__ == '__main__':
    unittest.main()
