"""Public logs, finalized log names, manifest failure/application/version records."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop.app_processes import Registry
from agent_desktop.artifacts import SUMMARY_APPLICATIONS, Store, failure_detail
from agent_desktop.cli import parse_request
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.lifecycle import inventory
from agent_desktop.logs import TAIL_BYTES, LogsTask, tail
from agent_desktop.worker import root_cause

GEN = 'a' * 32


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'artifacts', 'default', GEN, create=True)

    def launch(self, appid, argv=('/bin/true',), *, state=None):
        request = make_request('launch', session='default', expected_generation=GEN, caller_cwd='/',
                               arguments={'argv': list(argv)})
        token = self.store.request(request, time.monotonic(), time.monotonic() + 10)
        self.store.launch(token, list(argv), '/work')
        logs = {key: str(self.store.allocate(token, key)) for key in ('stdout', 'stderr')}
        self.store.application_prepare(token, appid, executable='/bin/true', logs=logs,
                                       cgroup='/owned/applications/' + appid)
        if state is not None:
            self.store.application_update(appid, state=state, process=None, exit_code=3,
                                          authorized=True, uncertain=False)
        return logs


class TailTests(unittest.TestCase):
    def write(self, data):
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(data)
        handle.close()
        return handle.name

    def test_last_lines_without_control_sequences(self):
        path = self.write(b'one\n\x1b[0;34mWARN\x1b[0m | two\x07\nthree\n')
        self.assertEqual(tail(path, 2), (os.path.getsize(path), ['WARN | two', 'three'], True))
        self.assertEqual(tail(path, 10)[1:], (['one', 'WARN | two', 'three'], False))
        self.assertEqual(tail(path, 0)[1:], ([], True))
        self.assertEqual(tail(self.write(b''), 5), (0, [], False))

    def test_large_log_reads_only_its_end_and_drops_the_partial_line(self):
        lines = [f'line {i:06d}' for i in range(5000)]
        path = self.write(('\n'.join(lines) + '\n').encode())
        size, shown, truncated = tail(path, 200)
        self.assertEqual(shown, lines[-200:])
        self.assertTrue(truncated)
        self.assertGreater(size, TAIL_BYTES)
        shown = tail(path, 5000)[1]
        self.assertLessEqual(sum(len(line) + 1 for line in shown), TAIL_BYTES)
        self.assertEqual(shown[-1], lines[-1])
        self.assertTrue(shown[0] in lines)  # Never a fragment.

    def test_invalid_utf8_and_symlinks(self):
        self.assertEqual(tail(self.write(b'ok \xff\n'), 1)[1], ['ok �'])
        link = self.write(b'x') + '.link'
        os.symlink('/etc/passwd', link)
        self.addCleanup(os.unlink, link)
        with self.assertRaises(OSError):
            tail(link, 1)


class LogsTaskTests(StoreCase):
    def task(self, applications, **arguments):
        request = make_request('logs', session='default', expected_generation=GEN, caller_cwd='/',
                               arguments=arguments)
        context = SimpleNamespace(work=SimpleNamespace(admission=SimpleNamespace(deadline=time.monotonic() + 3)))
        return LogsTask(request, context, self.store, applications, lambda: None)

    def registry(self):
        registry = object.__new__(Registry)
        registry.generation, registry.store, registry.active = GEN, self.store, None
        return registry

    def test_session_logs_only_before_any_launch(self):
        (self.store.path / 'logs' / 'compositor.log').write_text('started\n')
        result = self.task(self.registry(), tail='5').step(0)
        self.assertEqual([entry['source'] for entry in result['logs']], ['worker', 'compositor', 'bus'])
        compositor = result['logs'][1]
        self.assertEqual((compositor['tail'], compositor['bytes'], compositor['complete']), (['started'], 8, False))

    def test_latest_application_by_default_and_any_by_ref(self):
        older = self.launch('b' * 32, state='all-exited')
        newer = self.launch('c' * 32)
        Path(older['stderr']).write_text('old failure\n')
        Path(newer['stdout']).write_text('hello\n')
        result = self.task(self.registry(), source='application').step(0)
        self.assertEqual([(e['stream'], e['tail'], e['complete']) for e in result['logs']],
                         [('stdout', ['hello'], False), ('stderr', [], False)])
        self.assertEqual(result['logs'][0]['application']['application_id'], 'c' * 32)
        result = self.task(self.registry(), app=f'{GEN}:{"b" * 32}').step(0)
        self.assertEqual([(e['stream'], e['tail'], e['complete']) for e in result['logs']],
                         [('stdout', [], True), ('stderr', ['old failure'], True)])

    def test_app_with_a_session_source_is_rejected(self):
        self.launch('b' * 32)
        with self.assertRaises(ContractError) as caught:
            self.task(self.registry(), app=f'{GEN}:{"b" * 32}', source='worker').step(0)
        self.assertEqual(caught.exception.code, 'invalid_arguments')

    def test_arguments(self):
        for value in ('0', '200', 7):
            self.assertIn(make_request('logs', arguments={'tail': value}, caller_cwd='/').arguments['tail'],
                          (0, 200, 7))
        for value in ('201', '-1', 'x', 1.5, True):
            with self.assertRaises(ContractError):
                make_request('logs', arguments={'tail': value}, caller_cwd='/')
        self.assertEqual(make_request('logs', arguments={'source': 'bus'}, caller_cwd='/').arguments['tail'], 20)
        request = parse_request(['logs', '--tail', '3', '--source', 'application'], GEN, '/')[0]
        self.assertEqual((request.arguments['tail'], request.arguments['source']), (3, 'application'))


class ManifestTests(StoreCase):
    def test_application_logs_have_final_names(self):
        logs = self.launch('b' * 32)
        self.assertTrue(logs['stdout'].endswith('.stdout.log'))
        self.assertTrue(logs['stderr'].endswith('.stderr.log'))

    def test_first_failure_detail_is_kept_and_bounded(self):
        first = {'code': 'session_failed', 'message': 'An essential private desktop child exited.',
                 'context': {'component': 'compositor', 'returncode': -9, 'nested': {'dropped': True}}}
        self.store.generation_update(state='failed', failure='session_failed', detail=first)
        self.store.generation_update(state='failed', failure='timeout',
                                     detail={'code': 'timeout', 'message': 'later', 'context': {}})
        manifest = self.store.read()
        self.assertEqual(manifest['failure'], {'code': 'session_failed', 'message': first['message'],
                                               'context': {'component': 'compositor', 'returncode': -9}})
        self.assertEqual(manifest['first_failure'], 'session_failed')
        self.assertEqual(failure_detail({'code': 'timeout', 'message': 'x' * 2000})['message'], 'x' * 512)
        with self.assertRaises(ContractError):
            failure_detail({'code': 'not-a-code', 'message': 'x'})

    def test_applications_summary(self):
        self.launch('b' * 32, ['/usr/bin/app', '--flag', 'x' * 2000], state='all-exited')
        self.launch('c' * 32, ['/usr/bin/other'])
        self.store.summarize_applications(emptied=True)
        rows = self.store.read()['applications']
        self.assertEqual([row['application']['application_id'] for row in rows], ['b' * 32, 'c' * 32])
        first, second = rows
        self.assertEqual((first['argv'], first['argv_truncated']), (['/usr/bin/app', '--flag'], True))
        self.assertEqual((first['state'], first['exit_code'], first['ended_by_session_stop']), ('all-exited', 3, False))
        self.assertEqual((second['state'], second['ended_by_session_stop']), ('prepared', True))
        self.assertEqual(second['cwd'], '/work')
        self.assertTrue((self.store.path / first['launch_record']).exists())

    def test_summary_keeps_the_most_recent_applications(self):
        for index in range(SUMMARY_APPLICATIONS + 2):
            self.launch('%032x' % (index + 1))
        self.store.summarize_applications()
        manifest = self.store.read()
        self.assertEqual(len(manifest['applications']), SUMMARY_APPLICATIONS)
        self.assertEqual(manifest['applications_omitted'], 2)
        self.assertLess(len((self.store.path / 'manifest.json').read_bytes()), 65536)

    def test_inventory_from_prerequisite_report(self):
        report = {'dependencies': [
            {'name': 'kdotool', 'status': 'passed', 'observed': {'version': 'kdotool v0.3.0'}},
            {'name': 'libei', 'status': 'passed', 'observed': {'version': '1.6.0'}},
            {'name': 'python_bindings', 'status': 'passed', 'observed': {'versions': {'gi': '3.56.3'}}},
            {'name': 'runtime_executables', 'status': 'passed',
             'observed': {'kwin_version': 'kwin 6.7.5\n', 'systemd_version': 'systemd 262'}},
            {'name': 'user_manager', 'status': 'failed', 'observed': None}]}
        entries = inventory(report)
        self.assertEqual(entries, [{'component': 'kwin', 'version': 'kwin 6.7.5'},
                                   {'component': 'systemd', 'version': 'systemd 262'},
                                   {'component': 'libei', 'version': '1.6.0'},
                                   {'component': 'kdotool', 'version': 'kdotool v0.3.0'},
                                   {'component': 'gi', 'version': '3.56.3'}])
        self.store.provenance(dependencies=entries)
        self.assertEqual(self.store.read()['dependencies']['inventory'], entries)


class RootCauseTests(unittest.TestCase):
    def foundation(self, after=0):
        calls = []
        def tick():
            calls.append(time.monotonic())
            if len(calls) > after:
                raise ContractError('session_failed', 'An essential private desktop child exited.',
                                    context={'component': 'compositor', 'returncode': -9})
        return SimpleNamespace(tick=tick), calls

    def test_a_child_exit_replaces_its_symptom(self):
        symptom = ContractError('session_failed', 'Resumed input capability was lost.',
                                context={'component': 'input_resumed'})
        foundation, calls = self.foundation(after=2)
        self.assertEqual(root_cause(symptom, foundation).context['component'], 'compositor')
        self.assertEqual(len(calls), 3)

    def test_unrelated_failures_are_kept_after_a_short_wait(self):
        symptom = ContractError('internal_error', 'bug')
        healthy = SimpleNamespace(tick=lambda: None)
        started = time.monotonic()
        self.assertIs(root_cause(symptom, healthy, wait=.05), symptom)
        self.assertLess(time.monotonic() - started, .2)
        child = ContractError('session_failed', 'x', context={'component': 'bus'})
        self.assertIs(root_cause(child, self.foundation()[0]), child)
        self.assertIs(root_cause(symptom, None), symptom)


if __name__ == '__main__':
    unittest.main()
