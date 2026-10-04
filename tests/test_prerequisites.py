"""Installed runtime prerequisite policy, finite helpers, and doctor's claims."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_desktop import prerequisites as pre
from agent_desktop.contracts import OPERATIONS, ContractError, make_request


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'bin').mkdir()
        self.binary = self.root / 'bin/kdotool'
        self.binary.write_text('#!/bin/sh\nprintf "kdotool v0.3.0\\n"\n')
        self.binary.chmod(0o700)
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.receipt = {
            'revision': pre.KDOTool_REVISION, 'cargo_lock_sha256': pre.KDOTool_LOCK_SHA256,
            'binary_sha256': self.digest, 'release': pre.KDOTool_RELEASE, 'patches': [],
            'clean_checkout': True,
            'build_command': 'cargo build --release --locked --manifest-path <pinned-source>/Cargo.toml',
            'toolchain': {'rustc': 'recorded', 'cargo': 'recorded'},
            'resolved_cargo': {
                'lock_packages': [{'name': 'kdotool', 'version': '0.3.0'}],
                'resolved_nodes': [{'id': 'pinned', 'dependencies': [], 'features': [], 'deps': []}],
            },
        }
        self.write_receipt()

    def write_receipt(self):
        (self.root / 'build.json').write_text(json.dumps(self.receipt))

    def test_pinned_receipt_works_without_checkout_or_build_toolchain(self):
        result = pre._kdotool(self.root, time.monotonic() + 2)
        self.assertTrue(result['provenance_verified'])
        self.assertFalse(result['source_checkout_required'])
        self.assertEqual(result['executable'], str(self.binary))
        self.assertFalse((self.root / 'kdotool-source').exists())

    def test_changed_binary_rejected_before_execution_even_with_valid_receipt(self):
        marker = self.root / 'executed'
        self.binary.write_text(f'#!/bin/sh\ntouch {marker}\n')
        with self.assertRaises(pre._Failure) as caught:
            pre._kdotool(self.root, time.monotonic() + 2)
        self.assertEqual(caught.exception.reason, 'binary_digest_mismatch')
        self.assertFalse(marker.exists())

    def test_rebuild_at_pinned_revision_is_accepted_when_it_matches_its_receipt(self):
        # The binary hash itself is not pinned: a newer rustc changes it.
        result = pre._kdotool(self.root, time.monotonic() + 2)
        self.assertEqual(result['binary_sha256'], self.digest)

    def test_receipt_policy_and_resolved_graph_fail_closed(self):
        original = json.loads(json.dumps(self.receipt))
        mutations = [
            ('revision', 'changed'), ('cargo_lock_sha256', 'changed'), ('patches', ['unreviewed']),
            ('clean_checkout', 1), ('release', '9.9'), ('toolchain', {}),
            ('build_command', 'cargo build'), ('resolved_cargo', {'lock_packages': [], 'resolved_nodes': []}),
            ('resolved_cargo', {'lock_packages': [{'name': 'kdotool', 'version': '0.3'}],
                                'resolved_nodes': [{'id': 'x', 'dependencies': [], 'features': [],
                                                    'deps': [{'name': 'x', 'pkg': 'x', 'dep_kinds': [{}]}]}]}),
        ]
        for key, value in mutations:
            with self.subTest(field=key, value=value):
                receipt = original | {key: value}
                with self.assertRaises(pre._Failure):
                    pre._validate_receipt(receipt)

    def test_read_rejects_fifo_and_oversized_state_without_blocking(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        start = time.monotonic()
        with self.assertRaises(pre._Failure):
            pre._read(fifo, start + 1, 100)
        self.assertLess(time.monotonic() - start, .2)
        large = self.root / 'large'
        large.write_bytes(b'a' * 101)
        with self.assertRaises(pre._Failure):
            pre._read(large, start + 1, 100)

    def test_libei_failures_from_the_runtime_probe_are_reported(self):
        with self.assertRaises(pre._Failure) as caught:
            pre._libei({'worker': True, 'libei_error': {'reason': 'missing_libei_symbols', 'observed': ['ei_now']}})
        self.assertEqual(caught.exception.reason, 'missing_libei_symbols')
        self.assertEqual(caught.exception.observed, ['ei_now'])
        with self.assertRaises(pre._Failure) as caught:
            pre._libei({'worker': False})
        self.assertEqual(caught.exception.reason, 'not_checked')
        self.assertEqual(pre._libei({'worker': True, 'libei': {'version': '1.6.0'}}), {'version': '1.6.0'})

    def test_unimportable_worker_explains_pythonpath(self):
        with self.assertRaises(pre._Failure) as caught:
            pre._worker({'worker': False, 'error': 'ModuleNotFoundError'})
        self.assertEqual(caught.exception.code, 'prerequisite_missing')
        self.assertEqual(caught.exception.reason, 'worker_not_importable')
        self.assertIn('PYTHONPATH', caught.exception.repair)

    def test_runtime_probe_reproduces_service_isolation(self):
        # Under -I from /, a source tree reachable only via PYTHONPATH is invisible.
        result = pre._runtime_probe(time.monotonic() + 10)
        importable = subprocess.run([sys.executable, '-I', '-c', 'import agent_desktop'], cwd='/',
                                    env={'PATH': '/usr/bin:/bin'}, capture_output=True).returncode == 0
        self.assertEqual(result['worker'], importable)

    def test_libei_binding_checks_architecture_version_and_symbols(self):
        from agent_desktop import libei_binding
        old = self.root / 'libei.so.0.9.0'
        old.write_bytes(b'')
        with patch.object(libei_binding, 'SUPPORTED_LIBRARY', old):
            with self.assertRaises(libei_binding.Unsupported) as caught:
                libei_binding.describe()
        self.assertEqual(caught.exception.reason, 'unsupported_libei_version')
        with patch.object(libei_binding, 'SUPPORTED_LIBRARY', old), \
                patch.object(libei_binding.platform, 'machine', return_value='aarch64'):
            with self.assertRaises(libei_binding.Unsupported) as caught:
                libei_binding.describe()
        self.assertEqual(caught.exception.reason, 'unsupported_architecture')
        if libei_binding.SUPPORTED_LIBRARY.exists():
            info = libei_binding.describe()
            self.assertEqual(info['symbols_checked'], len(libei_binding.DECLARATIONS))

    def test_shadowed_binding_or_absent_fd_or_png_support_rejected(self):
        base = {'origins': {name: '/usr/lib/python/site-packages/' + name for name in ('gi', 'dbus', 'PIL')},
                'gio_unix_fd': True, 'png': True}
        bad = [base | {'origins': base['origins'] | {'gi': '/home/user/.local/gi'}},
               base | {'gio_unix_fd': False}, base | {'png': False}]
        for result in bad:
            with self.subTest(result=result), patch.object(pre, '_run', return_value=json.dumps(result)):
                with self.assertRaises(pre._Failure) as caught:
                    pre._bindings(time.monotonic() + 1)
                self.assertEqual(caught.exception.reason, 'unsupported_bindings')

    def test_helpers_exclude_ambient_desktop_credentials_and_python_paths(self):
        ambient = {'DBUS_SESSION_BUS_ADDRESS': 'host-secret', 'WAYLAND_DISPLAY': 'personal',
                   'DISPLAY': ':999', 'LD_PRELOAD': 'untrusted', 'PYTHONPATH': str(self.root),
                   'SECRET_TOKEN': 'secret'}
        marker = self.root / 'imported'
        (self.root / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "w").close()')
        with patch.dict(os.environ, ambient):
            output = pre._run([sys.executable, '-I', '-c', 'import json,os; print(json.dumps(dict(os.environ)))'], time.monotonic() + 2)
        result = json.loads(output)
        self.assertFalse(set(ambient) & set(result))
        self.assertFalse(marker.exists())

    def test_manager_helper_uses_only_explicit_user_manager_bus(self):
        output = pre._run([sys.executable, '-I', '-c', 'import os,json; print(json.dumps(dict(os.environ)))'], time.monotonic() + 2, manager=True)
        result = json.loads(output)
        self.assertEqual(result['DBUS_SESSION_BUS_ADDRESS'], f'unix:path=/run/user/{os.getuid()}/bus')
        self.assertNotIn('WAYLAND_DISPLAY', result)

    def test_helper_output_is_bounded_during_execution(self):
        start = time.monotonic()
        with self.assertRaises(pre._Failure) as caught:
            pre._run([sys.executable, '-I', '-c', 'import os,time; os.write(1,b"x"*200000); time.sleep(30)'], start + 1)
        self.assertEqual(caught.exception.reason, 'output_limit')
        self.assertLess(time.monotonic() - start, 1)

    def test_timeout_kills_helper_and_pipe_holding_descendant(self):
        pidfile = self.root / 'pid'
        code = ('import os,time; pid=os.fork(); '
                f'open({str(pidfile)!r},"w").write(str(pid)) if pid else None; '
                'time.sleep(30)')
        start = time.monotonic()
        with self.assertRaises(pre._Failure) as caught:
            pre._run([sys.executable, '-I', '-c', code], start + .4)
        self.assertEqual(caught.exception.reason, 'helper_timeout')
        self.assertLess(time.monotonic() - start, .6)
        pid = int(pidfile.read_text())
        for _ in range(30):
            try:
                state = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == 'Z':
                break
            time.sleep(.01)
        else:
            self.fail('prerequisite helper descendant survived timeout')

    def test_doctor_is_read_only_and_never_reports_capability_readiness(self):
        request = make_request('doctor', arguments={'dependency_root': str(self.root)}, caller_cwd='/')
        with \
                patch.object(pre, '_runtime_probe', return_value={'worker': True, 'libei': {}}), patch.object(pre, '_bindings', return_value={}), \
                patch.object(pre, '_executables', return_value={}), patch.object(pre, '_manager', return_value={}):
            before = sorted(str(p) for p in self.root.rglob('*'))
            result = pre.doctor(request)
            after = sorted(str(p) for p in self.root.rglob('*'))
        self.assertEqual(before, after)
        self.assertTrue(result['ok'])
        self.assertFalse(result['desktop_launched'])
        self.assertFalse(result['desktop_ready'])
        self.assertNotIn('release_qualified', result)
        self.assertEqual(set(result['capabilities'].values()), {'not_tested'})
        self.assertIn('click', result['unsupported_operations'])
        self.assertIn('launch', result['supported_operations'])
        self.assertEqual(set(result['supported_operations']) | set(result['unsupported_operations']),
                         set(OPERATIONS))
        self.assertFalse(set(result['supported_operations']) & set(result['unsupported_operations']))

    def test_doctor_error_retains_all_component_diagnostics_and_repairs(self):
        with patch.object(pre, '_runtime_probe', return_value={'worker': True, 'libei': {}}), patch.object(pre, '_bindings', return_value={}), \
                patch.object(pre, '_executables', return_value={}), patch.object(pre, '_manager', return_value={}):
            with self.assertRaises(ContractError) as caught:
                pre.check(self.root / 'missing', time.monotonic() + 2)
        error = caught.exception
        self.assertEqual(error.code, 'prerequisite_missing')
        self.assertEqual(error.context['component'], 'kdotool')
        report = error.context['prerequisite_report']
        self.assertEqual(len(report['dependencies']), 6)
        self.assertEqual(report['dependencies'][0]['status'], 'failed')
        self.assertTrue(report['dependencies'][0]['repair'])
        self.assertEqual(report['dependencies'][-1]['status'], 'passed')

    def test_untested_kwin_version_warns_without_failing(self):
        executables = {'kwin_version': 'kwin 6.9.0', 'kwin_matches_tested': False}
        with patch.object(pre, '_runtime_probe', return_value={'worker': True, 'libei': {'version': '1.6.0', 'tested_version': '1.6.0'}}), \
                patch.object(pre, '_bindings', return_value={}), patch.object(pre, '_kdotool', return_value={}), \
                patch.object(pre, '_executables', return_value=executables), patch.object(pre, '_manager', return_value={}):
            report = pre.check(self.root, time.monotonic() + 2)
        self.assertTrue(report['ok'])
        self.assertEqual([warning['code'] for warning in report['warnings']], ['untested_kwin_version'])

    def test_unknown_libei_version_warns(self):
        executables = {'kwin_version': 'kwin 6.7.5', 'kwin_matches_tested': True}
        with patch.object(pre, '_runtime_probe', return_value={'worker': True, 'libei': {'version': None, 'tested_version': '1.6.0'}}), \
                patch.object(pre, '_bindings', return_value={}), patch.object(pre, '_kdotool', return_value={}), \
                patch.object(pre, '_executables', return_value=executables), patch.object(pre, '_manager', return_value={}):
            report = pre.check(self.root, time.monotonic() + 2)
        self.assertTrue(report['ok'])
        self.assertEqual([warning['code'] for warning in report['warnings']], ['untested_libei_version'])

    def test_expired_deadline_launches_no_helper(self):
        with patch.object(pre, '_run') as run:
            with self.assertRaises(ContractError) as caught:
                pre.check(self.root, time.monotonic() - 1)
        run.assert_not_called()
        failures = caught.exception.context['prerequisite_report']['dependencies']
        self.assertTrue(all(item['reason'] == 'deadline_expired' for item in failures))

    def test_late_observation_cannot_pass(self):
        def delayed():
            time.sleep(.04)
            return {}
        with patch.object(pre, '_kdotool', side_effect=lambda *_: delayed()):
            with self.assertRaises(ContractError) as caught:
                pre.check(self.root, time.monotonic() + .01)
        self.assertEqual(caught.exception.context['reason'], 'deadline_expired')

    def test_doctor_resolves_root_against_caller_not_worker_cwd(self):
        request = make_request('doctor', arguments={'dependency_root': 'deps/../pinned'}, caller_cwd='/caller')
        with patch.object(pre, 'check', return_value={}) as check:
            pre.doctor(request)
        self.assertEqual(check.call_args.args[0], '/caller/pinned')


if __name__ == '__main__':
    unittest.main()
