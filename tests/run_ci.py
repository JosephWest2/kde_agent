"""CI entry point: the documented discovery, with skips checked by test id, not output text.

PYTHONPATH=src python tests/run_ci.py
"""
import os
from pathlib import Path
import sys
import unittest

TESTS = Path(__file__).resolve().parent
# Modules whose tests may skip for a missing host facility (host_facilities.py).
HOST_MODULES = frozenset({'test_input_connection', 'test_libei_probe', 'test_lifecycle_process'})


def module(test):
    # Ordinary tests and subtests have ids 'module.Class.method...'. Anything
    # else (class or module setup skips) is not attributed to a host module.
    return test.id().split('.', 1)[0]


def problems(result):
    found = ['unexpected skip outside the host modules: ' + test.id() + ': ' + reason
             for test, reason in result.skipped if module(test) not in HOST_MODULES]
    if result.testsRun == 0:
        found.append('no tests ran')
    if result.errors or result.failures or result.unexpectedSuccesses:
        found.append(f'{len(result.failures)} failures, {len(result.errors)} errors, '
                     f'{len(result.unexpectedSuccesses)} unexpected successes')
    return found


def summary(result):
    skipped = len(result.skipped)
    lines = [f'Tests run: {result.testsRun}, skipped: {skipped}, executed: {result.testsRun - skipped}']
    lines += ['- skipped ' + test.id() + ': ' + reason for test, reason in result.skipped]
    return lines + problems(result)


def main():
    suite = unittest.defaultTestLoader.discover(str(TESTS), top_level_dir=str(TESTS))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    lines = summary(result)
    print('\n'.join(lines))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('\n'.join(lines) + '\n')
    return 1 if problems(result) else 0


if __name__ == '__main__':
    sys.exit(main())
