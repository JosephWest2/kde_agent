import io
from contextlib import redirect_stderr
import unittest

import run_ci


def case(module_name, body):
    cls = type('Case', (unittest.TestCase,), {'__module__': module_name, 'test_it': body})
    return cls('test_it')


def run(*tests):
    result = unittest.TestResult()
    with redirect_stderr(io.StringIO()):
        unittest.TestSuite(tests).run(result)
    return result


class GuardTests(unittest.TestCase):
    def test_host_module_skip_is_allowed(self):
        result = run(case('test_libei_probe', lambda self: self.skipTest('no /usr/lib/libei.so.1')))
        self.assertEqual(run_ci.problems(result), [])
        self.assertEqual(run_ci.summary(result)[0], 'Tests run: 1, skipped: 1, executed: 0')

    def test_portable_skip_after_stderr_newline_is_caught(self):
        def body(self):
            import sys
            sys.stderr.write('diagnostic\n')
            self.skipTest('quiet')
        problems = run_ci.problems(run(case('test_portable', body)))
        self.assertEqual(len(problems), 1)
        self.assertIn('test_portable.Case.test_it', problems[0])

    def test_host_module_name_in_reason_is_not_allowed(self):
        result = run(case('test_portable', lambda self: self.skipTest('see (test_libei_probe.InputSafetyTests)')))
        self.assertEqual(len(run_ci.problems(result)), 1)

    def test_subtest_skip_keeps_its_module(self):
        def body(self):
            with self.subTest(n=1):
                self.skipTest('no user service manager')
        self.assertEqual(run_ci.problems(run(case('test_lifecycle_process', body))), [])

    def test_class_setup_skip_is_not_attributed_to_a_host_module(self):
        cls = type('Case', (unittest.TestCase,), {'__module__': 'test_libei_probe', 'test_it': lambda self: None,
            'setUpClass': classmethod(lambda cls: (_ for _ in ()).throw(unittest.SkipTest('class')))})
        self.assertTrue(run_ci.problems(run(cls('test_it')))[0].startswith('unexpected skip'))

    def test_failures_errors_and_empty_runs_fail(self):
        self.assertEqual(len(run_ci.problems(run(case('test_portable', lambda self: self.fail('x'))))), 1)
        self.assertEqual(len(run_ci.problems(run(case('test_portable', lambda self: 1 / 0)))), 1)
        self.assertEqual(run_ci.problems(run()), ['no tests ran'])


if __name__ == '__main__':
    unittest.main()
