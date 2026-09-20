"""Finite installed-wheel issue 25 close evidence; private evidence seams only.

python -I installed_close.py prepare NEW_PREPARED --commit HEAD
NEW_PREPARED/venv/bin/python -I installed_close.py run NEW_OUTPUT DEPENDENCIES NEW_PREPARED
Repeat --case NAME to select cases. Outputs must be new; failed runs are retained.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import traceback

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
SUPPORT = PROJECT / 'evidence/issue-24/installed_targeting.py'
spec = importlib.util.spec_from_file_location('issue24_evidence_support', SUPPORT)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.SCRIPT = SCRIPT
read, save, digest, hashes = base.read, base.save, base.digest, base.hashes
wait, events, identity = base.wait, base.events, base.identity
CASES = ('normal-window', 'normal-app', 'braced-window', 'ambiguous-app',
         'selected-sibling', 'selected-dialog', 'confirmation', 'refusal',
         'delay-within', 'delay-beyond', 'descendant', 'queued-vanish',
         'queued-dialog', 'queued-expiry', 'noop', 'cancel-before', 'cancel-after',
         'disconnect', 'stop-wait', 'bus-death', 'kwin-death', 'worker-death',
         'slow-query', 'stopped-query', 'stopped-close', 'hook-success',
         'hook-timeout', 'restart')


def prepare(output, commit):
    base.prepare(output, commit)
    receipt = read(Path(output) / 'prepared.json')
    source = Path(receipt['source'])
    receipt.update(runner_sha256=digest(source / 'evidence/issue-25/installed_close.py'),
                   support_sha256=digest(source / 'evidence/issue-24/installed_targeting.py'))
    save(Path(output) / 'prepared.json', receipt)
    print(json.dumps(receipt))


def worker(name, generation, artifacts, binary):
    """Observe actual owners; hooks replace only the evidence request task."""
    from agent_desktop import windows, closing, app_processes, children
    from agent_desktop.contracts import ContractError
    folder = Path(artifacts) / 'generations' / generation
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = (folder / 'close-trace.jsonl').open('x', buffering=1)
    counts = {'events': 0, 'bytes': 0, 'children_polls': 0, 'registry_ticks': 0}
    def emit(event, **values):
        raw = json.dumps({'at': time.monotonic(), 'event': event, **values}, allow_nan=False) + '\n'
        counts['events'] += 1
        counts['bytes'] += len(raw)
        assert counts['events'] <= 20000 and counts['bytes'] <= 8 * 1024 * 1024
        stream.write(raw)
    def mode():
        path = folder / 'fault.json'
        return read(path)['mode'] if path.exists() else 'normal'
    poll_original, tick_original = children.Children.poll, app_processes.Registry.tick
    def poll(self, *args, **kwargs):
        counts['children_polls'] += 1
        return poll_original(self, *args, **kwargs)
    def tick(self, *args, **kwargs):
        counts['registry_ticks'] += 1
        return tick_original(self, *args, **kwargs)
    children.Children.poll, app_processes.Registry.tick = poll, tick
    native_original, query_original = windows.NativeClose, windows.Query
    class NativeClose(native_original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.evidence_mode, self.evidence_done = mode(), False
            emit('native_start', kind='close', request_id=self.request_id, query_id=self.id,
                 deadline=self.deadline, window=self.window, mode=self.evidence_mode)
        def _prepare_script(self):
            if self.evidence_mode == 'noop':
                query_original._prepare_script(self)
                (self.folder / 'input.js').write_text('output_result("noop");')
            else:
                super()._prepare_script()
        def _argv(self):
            if self.evidence_mode == 'noop':
                return [self.owner.binary, '--name', self.name, 'kwinscript', '--file', str(self.folder / 'input.js')]
            return super()._argv()
        def _spawn(self, remove=False):
            super()._spawn(remove)
            child = self.remover if remove else self.child
            if child is None:
                return
            emit('native_spawn', kind='close', request_id=self.request_id, query_id=self.id,
                 remove=remove, pid=child.process.pid, argv=child.process.args, script_name=self.name)
            if not remove and self.evidence_mode == 'stopped-close' and child.returncode is None:
                # Only this owned adapter child is faulted, never an application PID.
                os.kill(child.process.pid, signal.SIGSTOP)
                emit('native_stopped', kind='close', request_id=self.request_id,
                     query_id=self.id, pid=child.process.pid)
        def receipt(self, clean):
            if self.evidence_done:
                return
            self.evidence_done = True
            emit('native_complete', kind='close', request_id=self.request_id, query_id=self.id,
                 deadline=self.deadline, accepted_at=self.accepted_at, clean=clean,
                 spawned=self.spawned, script_absent=self.absent,
                 reaped=self.child is None or self.child.returncode is not None,
                 returncode=None if self.child is None else self.child.returncode,
                 temporary_removed=self.folder is None or not self.folder.exists(),
                 cleanup_deadline=self.cleanup_deadline,
                 error=None if self.error is None else self.error.code)
        def step(self):
            result = super().step()
            if result is not None:
                self.receipt(True)
            return result
        def cleanup(self, deadline=None):
            clean = super().cleanup(deadline)
            if clean or time.monotonic() >= self.cleanup_deadline:
                self.receipt(clean)
            return clean
    windows.NativeClose = NativeClose
    task_original = closing.CloseTask
    class ObservedClose(task_original):
        def __init__(self, request, context, adapter, registry, healthy):
            self.evidence_request = request
            self.evidence_phase = None
            self.hook = None
            if mode() in ('hook-success', 'hook-timeout'):
                self.context = context
                # Give this independent hook a shorter hard bound so its own
                # terminal tick occurs before the outer scheduler's cancellation.
                self.hook_deadline = context.work.admission.deadline - (.35 if mode() == 'hook-timeout' else 0)
                self.hook = closing.CloseHook(request.request_id, generation, adapter, registry,
                    healthy, context.effects, window=request.arguments.get('window'),
                    application=request.arguments.get('app'))
                self.owner = None
                assert self.hook.owner is None and adapter.active is None
                emit('hook_constructed', request_id=request.request_id, effect_free=True,
                     supplied_deadline=self.hook_deadline,
                     children_polls=counts['children_polls'], registry_ticks=counts['registry_ticks'])
            else:
                super().__init__(request, context, adapter, registry, healthy)
            emit('close_start', request_id=request.request_id,
                 admitted_at=context.work.admission.admitted_at,
                 deadline=context.work.admission.deadline, mode=mode())
        def step(self, now):
            if self.hook is not None:
                result = self.hook(now, self.hook_deadline)
                self.owner = self.hook.owner
                if result is not None:
                    assert self.hook(now + 1, self.hook_deadline + 10) is result
                    emit('hook_terminal', request_id=self.evidence_request.request_id, result=result,
                         supplied_deadline=self.hook_deadline, actual_deadline=self.owner.deadline,
                         children_polls=counts['children_polls'], registry_ticks=counts['registry_ticks'],
                         terminal_idempotent=True, production_shutdown_wiring=False)
                    if result['state'] != 'complete':
                        raise ContractError(result['error'], 'Evidence hook reached its supplied bound.')
                    return result['result']
            else:
                result = super().step(now)
            if self.owner.phase != self.evidence_phase:
                self.evidence_phase = self.owner.phase
                emit('close_phase', request_id=self.evidence_request.request_id,
                     phase=self.owner.phase, projection=self.owner.projection())
            if result is not None:
                emit('close_complete', request_id=self.evidence_request.request_id, result=result)
            return result
        def request_cancel(self, reason):
            emit('close_cancel', request_id=self.evidence_request.request_id, reason=reason)
            if self.owner is not None:
                super().request_cancel(reason)
        def cleanup(self, now):
            if self.owner is None:
                return True
            return super().cleanup(now)
    closing.CloseTask = ObservedClose
    try:
        # Existing support instruments Query/Activation, GLib gaps, artifact writes.
        base.worker(name, generation, artifacts, binary)
    finally:
        emit('close_worker_end', counts=counts)
        stream.close()


class Run(base.Run):
    def __init__(self, output, dependencies, prepared):
        import agent_desktop
        self.output = Path(output).resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.dependencies = Path(dependencies).resolve()
        self.prepared = read(Path(prepared) / 'prepared.json')
        self.source = Path(self.prepared['source'])
        assert Path(sys.prefix).resolve() == (Path(prepared) / 'venv').resolve()
        installed = Path(agent_desktop.__file__).resolve()
        assert not installed.is_relative_to(self.source / 'src')
        assert not installed.is_relative_to(PROJECT / 'src')
        assert hashes(self.source / 'src/agent_desktop') == hashes(installed.parent)
        assert digest(SCRIPT) == digest(self.source / 'evidence/issue-25/installed_close.py')
        assert digest(SUPPORT) == digest(self.source / 'evidence/issue-24/installed_targeting.py')
        assert digest(self.prepared['wheel']) == self.prepared['wheel_sha256']
        spec = importlib.util.spec_from_file_location('fixture_build', self.source / 'tools/private_harness.py')
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        self.fixture, fixture_build = harness.build(self.output / 'fixture-build')
        from agent_desktop.prerequisites import check
        self.receipt = {'prepared': self.prepared, 'runner_sha256': digest(SCRIPT),
            'support_sha256': digest(SUPPORT), 'installed_module': str(installed),
            'installed_hashes': hashes(installed.parent), 'fixture': fixture_build,
            'dependencies': check(str(self.dependencies), time.monotonic() + 30),
            'cases': {}, 'release_qualified': False, 'broad_latency_qualified': False,
            'qualification': 'Functional cases and separately measured selected controls only; all GLib gap negatives retained.',
            'diagnostic_durability': 'atomic-replace-or-jsonl-without-fsync'}
        self.artifacts = self.output / 'artifacts'
        self.temporary = tempfile.TemporaryDirectory(prefix='a25-')
        self.runtime = Path(self.temporary.name)
        os.environ['XDG_RUNTIME_DIR'] = str(self.runtime)
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': str(self.runtime)}
        self.cli = Path(sys.executable).with_name('agent-desktop')
        self.persist()

    @contextmanager
    def generation(self, label):
        self.name = 'a25-' + label
        self.case = self.receipt['cases'].setdefault(label, {})
        child = subprocess.run([sys.executable, '-I', str(SCRIPT), 'controller', self.name,
            str(self.artifacts), str(self.dependencies)], cwd='/', env=self.env,
            capture_output=True, text=True, timeout=65)
        self.case['controller'] = {'stdout': child.stdout, 'stderr': child.stderr, 'exit_code': child.returncode}
        self.persist()
        assert child.returncode == 0, self.case
        started = json.loads(child.stdout)
        assert started['ok'], started
        self.gen = started['session']['generation']
        self.folder = self.artifacts / 'generations' / self.gen
        data = read(self.runtime / 'agent-desktop/g' / self.gen / 'lifecycle.json')
        self.case.update(start=started, metadata=data)
        self.launched = None
        try:
            yield
            self.case['functional_passed'] = True
        except Exception:
            self.case.update(functional_passed=False, failure=traceback.format_exc())
            raise
        finally:
            if self.launched:
                self.case['fixture_events_before_stop'] = self.fixture_events()
            stop = self.case.get('explicit_stop')
            if stop is None:
                stop = self.invoke(['session', 'stop', '--session', self.name])
                self.case['stop'] = stop
            stop_deadline = stop['started_at'] + 15
            group = Path('/sys/fs/cgroup' + data['cgroup'])
            def settled():
                empty = not group.exists() or 'populated 0' in (group / 'cgroup.events').read_text()
                recorded = (self.folder / 'terminal.json').exists()
                return empty and recorded
            try:
                wait(settled, max(0, stop_deadline - time.monotonic()))
                self.case.update(cleanup_confirmed=True, cleanup_observed_at=time.monotonic(),
                                 cleanup_deadline=stop_deadline)
                assert self.case['cleanup_observed_at'] <= stop_deadline
            except Exception:
                self.case.update(cleanup_confirmed=False, cleanup_failure=traceback.format_exc())
            self.case['trace'] = self.native_trace()
            self.case['retained_service_records'] = {name: read(self.folder / name)
                for name in ('terminal.json', 'reconciliation.json', 'manifest.json', 'shutdown.json', 'startup-failure.json')
                if (self.folder / name).exists()}
            self.case['request_records'] = [read(p) for p in self.folder.glob('requests/*/*/record.json')]
            self.case['fixture_events'] = self.fixture_events() if self.launched else []
            starts = {}
            for event in self.case['trace']:
                if event['event'] == 'native_start' and event['kind'] == 'query':
                    starts.setdefault(event['request_id'], []).append(event['at'])
            intervals = [b - a for values in starts.values() for a, b in zip(values, values[1:])]
            self.case['query_poll_intervals'] = {'scope': 'query starts per request; native close excluded',
                'count': len(intervals), 'minimum': min(intervals, default=None), 'samples': intervals}
            self.case['glib_gap_negatives'] = [e for e in self.case['trace'] if e['event'] == 'glib_gap']
            self.case['passed'] = self.case.get('functional_passed', False) and self.case['cleanup_confirmed']
            self.persist()
            assert self.case['cleanup_confirmed'], self.case.get('cleanup_failure')

    def native_trace(self):
        return sorted(events(self.folder / 'targeting-trace.jsonl') + events(self.folder / 'close-trace.jsonl'),
                      key=lambda e: e['at'])

    def close_args(self, row=None, timeout='2'):
        return self.args('close', '--window', self.wref(row), '--timeout', timeout) if row else self.args(
            'close', '--app', self.appref, '--timeout', timeout)

    def row(self, label='primary', count=1):
        widths = {'primary': 640, 'sibling': 480, 'dialog': 320, 'child': 400}
        rows = wait(lambda: (found if len(found := self.rows()) == count else None))
        matches = [r for r in rows if abs(r['client']['width'] - widths[label]) < .5]
        assert len(matches) == 1, rows
        return matches[0]

    def close_receipts(self, expected=1, label='primary'):
        receipts = [e for e in self.fixture_events() if e['event'] == 'close_requested']
        assert len(receipts) == expected, receipts
        if expected:
            assert all(e['surface'] == label for e in receipts), receipts
            assert [e['request_count'] for e in receipts] == list(range(1, expected + 1))
        self.case['close_receipts'] = receipts
        return receipts

    def live_root(self):
        value = identity(self.launched['process']['pid'])
        self.case.setdefault('live_root_observations', []).append({'at': time.monotonic(), **value})
        return value

    def assert_timeout(self, response):
        assert not response['ok'] and response['error']['code'] == 'timeout', response
        result = response['error']['partial_result']
        assert result['application'] == self.app and result['exited'] is False, result
        assert result['close_state']['dispatch'] == 'transport_completed', result
        assert result['remaining_processes'] is None and result['process_state']['subtree_populated'] is True
        return result

    def normal(self, selector):
        self.launch('--exit-after-ms', '12000', flags=('--wait-window',))
        row = self.row()
        self.case['root_before'] = self.live_root()
        if selector == 'braced-window':
            args = self.args('close', '--window', self.gen + ':{' + row['window']['window_id'].upper() + '}', '--timeout', '3')
        else:
            args = self.close_args(None if selector == 'normal-app' else row, '3')
        result = self.call(args)
        assert result['exited'] is True and result['window'] == row['window'] and result['application'] == self.app, result
        assert result['process_state']['all_exited'] is True, result
        self.close_receipts()
        self.call(self.wait_args('exit', timeout='.5'))

    def selection(self, label):
        self.launch('--sibling', '--dialog', '--exit-after-ms', '12000', flags=('--wait-window',))
        row = self.row(label, 3)
        response = self.call(self.close_args(row, '1.2'), error='timeout')
        self.assert_timeout(response)
        self.close_receipts(label=label)
        remaining = self.rows()
        assert len(remaining) == 2 and all(r['window'] != row['window'] for r in remaining), remaining
        self.live_root()

    def ambiguous(self):
        self.launch('--sibling', '--dialog', '--exit-after-ms', '12000', flags=('--wait-window',))
        self.row('primary', 3)
        response = self.call(self.close_args(), error='target_ambiguous')
        assert len(response['error']['context']['candidates']) == 3
        self.close_receipts(0)
        assert not any(e['event'] == 'native_start' and e['kind'] == 'close' for e in self.native_trace())

    def refusal(self, confirmation=False):
        mode = 'confirmation' if confirmation else 'refuse'
        self.launch('--close-mode', mode, '--exit-after-ms', '15000', flags=('--wait-window',))
        row = self.row()
        result = self.assert_timeout(self.call(self.close_args(row, '1.4'), error='timeout'))
        self.close_receipts()
        before = self.live_root()
        if confirmation:
            dialog = self.row('dialog', 2)
            assert dialog['window'] in result['windows'], result
            self.call(self.wait_args('window'))
            self.call(self.args('focus', '--window', self.wref(dialog)))
            fixtures = self.fixture_events()
            assert len([e for e in fixtures if e['event'] == 'confirmation_opened']) == 1
            assert not any(e['event'] == 'confirmation_accepted' for e in fixtures)
            assert not any(e['surface'] == 'dialog' and e['event'] in
                           ('close_requested', 'key', 'button', 'motion', 'axis', 'destroy')
                           for e in fixtures if 'surface' in e)
            self.case['confirmation_dialog'] = dialog
            self.case['confirmation_acknowledgment_qualified'] = False
        else:
            assert len(self.rows()) == 1
        assert self.live_root() == before

    def delay(self, beyond=False):
        delay = '2400' if beyond else '300'
        self.launch('--close-mode', 'delay', '--close-delay-ms', delay, '--exit-after-ms', '15000', flags=('--wait-window',))
        row = self.row()
        if beyond:
            self.assert_timeout(self.call(self.close_args(row, '.9'), error='timeout'))
            self.live_root()
            assert len(self.rows()) == 1
            self.call(self.wait_args('exit', timeout='4'))
        else:
            result = self.call(self.close_args(row, '3'))
            assert result['exited'] is True
        self.close_receipts()
        assert any(e['event'] == 'destroy' and e['source'] == 'delayed_compositor' for e in self.fixture_events())

    def descendant(self):
        self.launch('--descendant-ms', '3200', '--exit-after-ms', '12000', flags=('--wait-window',))
        row = self.row()
        receipt = wait(lambda: next((e for e in self.fixture_events() if e['event'] == 'descendant_spawned'), None))
        self.case['descendant_identity'] = identity(receipt['descendant_pid'])
        fd = os.pidfd_open(receipt['descendant_pid'])
        try:
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            result = self.assert_timeout(self.call(self.close_args(row, '.9'), error='timeout'))
            assert result['process_state']['root_reaped'] is True
            assert not poll.poll(0), 'descendant must remain alive after close timeout'
            self.case['root_exited_record'] = self.app_record()
            self.call(self.wait_args('exit', timeout='4'))
            assert poll.poll(100), 'descendant exit must independently be observable'
            self.close_receipts()
        finally:
            os.close(fd)

    def queued(self, mode):
        if mode == 'dialog':
            result = self.call(self.args('launch', '--wait-window', '--', sys.executable, '-I', str(SCRIPT),
                'fixture-control', str(self.fixture), '1500', '12000'))
            self.launched, self.app = result, result['application']
            self.appref = self.gen + ':' + self.app['application_id']
            row = self.row()
            start = wait(lambda: next((e for e in self.fixture_events() if e['event'] == 'fixture_control_started'), None))
            mutation_at = start['at'] + 1.5
        else:
            extra = ('--destroy-after-ms', 'primary:1500') if mode == 'vanish' else ()
            self.launch('--sibling', '--exit-after-ms', '12000', *extra, flags=('--wait-window',))
            row = self.row('primary', 2)
            schedule = next((e for e in self.fixture_events() if e['event'] == 'surface_schedule'), None)
            mutation_at = schedule['started_ns'] / 1e9 + 1.5 if schedule else time.monotonic() + 1.3
        budget = mutation_at + .2 - time.monotonic()
        assert .4 < budget < 2, 'setup missed finite queued mutation window'
        blocker = self.pending(self.wait_args('exit', timeout=str(budget)))
        self.target_started(blocker[1]['started_at'], 'exit')
        queued = self.pending(self.close_args(None if mode == 'dialog' else row, '.3' if mode == 'expiry' else '3'))
        if mode == 'cancel':
            time.sleep(.1)
            at = time.monotonic()
            queued[0].send_signal(signal.SIGINT)
            self.case['cancel_sent_at'] = at
        response = self.remember_pending(queued)['response']
        self.remember_pending(blocker)
        code = {'dialog': 'target_ambiguous', 'vanish': 'target_not_found', 'expiry': 'timeout', 'cancel': 'cancelled'}[mode]
        assert response and not response['ok'] and response['error']['code'] == code, response
        self.close_receipts(0)
        if mode in ('cancel', 'expiry'):
            assert not any(e['event'] == 'native_start' and e['kind'] == 'close' for e in self.native_trace())
        self.live_root()

    def noop(self):
        self.launch('--exit-after-ms', '12000', flags=('--wait-window',))
        row = self.row()
        save(self.folder / 'fault.json', {'mode': 'noop'})
        self.assert_timeout(self.call(self.close_args(row, '1'), error='timeout'))
        self.close_receipts(0)
        native = [e for e in self.native_trace() if e['event'] == 'native_complete' and e['kind'] == 'close']
        assert len(native) == 1 and native[0]['returncode'] == 0 and native[0]['clean'], native
        save(self.folder / 'fault.json', {'mode': 'normal'})
        assert len(self.rows()) == 1
        self.live_root()

    def status_measurement(self):
        status = self.invoke(['session', 'status', '--session', self.name])
        self.case['status_during_close'] = status
        assert status['response']['ok'], status
        matches = [read(path) for path in self.folder.glob('requests/*/*/record.json')
                   if (value := read(path)).get('operation') == 'session.status'
                   and status['started_at'] <= value.get('admitted_at', 0) <= status['ended_at']]
        assert len(matches) == 1, matches
        elapsed = matches[0]['terminal_observed_monotonic'] - matches[0]['admitted_at']
        self.case['status_worker_seconds'] = elapsed
        self.case['status_worker_under_100ms'] = elapsed < .1

    def close_control(self, kind):
        self.launch('--close-mode', 'refuse', '--exit-after-ms', '15000', flags=('--wait-window',))
        row = self.row()
        before = self.live_root()
        pending = self.pending(self.close_args(row, '5'))
        started = wait(lambda: next((e for e in self.native_trace() if e['event'] == 'close_start'
                                  and e['at'] >= pending[1]['started_at']), None))
        wait(lambda: any(e['event'] == 'close_requested' for e in self.fixture_events()))
        if kind in ('cancel', 'disconnect'):
            self.status_measurement()
            at = time.monotonic()
            pending[0].send_signal(signal.SIGKILL if kind == 'disconnect' else signal.SIGINT)
            response = self.remember_pending(pending)['response']
            if kind == 'cancel':
                assert response['error']['code'] == 'cancelled', response
            else:
                assert response is None, response
            cancelled = wait(lambda: next((e for e in self.native_trace() if e['event'] == 'close_cancel'
                and e['request_id'] == started['request_id']), None))
            self.case['cancel_dispatch_seconds'] = cancelled['at'] - at
            self.case['cancel_dispatch_under_100ms'] = 0 <= self.case['cancel_dispatch_seconds'] < .1
            assert self.live_root() == before
            assert len(self.rows()) == 1
            self.case['retained_application_after_control'] = self.app_record()
            self.close_receipts()
            return
        if kind == 'stop':
            self.case['explicit_stop'] = self.invoke(['session', 'stop', '--session', self.name])
        else:
            group = Path('/sys/fs/cgroup' + self.case['metadata']['cgroup'])
            matches = []
            for member in group.rglob('cgroup.procs'):
                for pid in member.read_text().split():
                    try:
                        cmd = Path('/proc', pid, 'cmdline').read_bytes()
                        comm = Path('/proc', pid, 'comm').read_text().strip()
                        if ((kind == 'worker' and str(SCRIPT).encode() in cmd and b'\x00worker\x00' in cmd)
                            or (kind != 'worker' and comm == ('dbus-daemon' if kind == 'bus' else 'kwin_wayland'))):
                            matches.append(identity(int(pid)))
                    except FileNotFoundError:
                        pass
            assert len(matches) == 1, matches
            self.case['fault_identity'] = matches[0]
            fd = os.pidfd_open(matches[0]['pid'])
            try:
                assert identity(matches[0]['pid']) == matches[0]
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        response = self.remember_pending(pending)['response']
        assert response and not response['ok'], response
        expected = ('session_unavailable', 'session_failed', 'completion_unknown') if kind == 'worker' else (
            'cancelled',) if kind == 'stop' else ('session_failed',)
        assert response['error']['code'] in expected, response
        self.case['signals_scope'] = 'Explicit independent session stop or essential-service fault; never close timeout escalation.'
        self.close_receipts()

    def adapter_fault(self, kind):
        self.launch('--close-mode', 'refuse', '--exit-after-ms', '15000', flags=('--wait-window',))
        row = self.row()
        fault = {'slow-query': 'slow', 'stopped-query': 'stopped', 'stopped-close': 'stopped-close'}[kind]
        save(self.folder / 'fault.json', {'mode': fault})
        pending = self.pending(self.close_args(row, '2'))
        if kind != 'slow-query':
            wait(lambda: any(e['event'] == 'native_stopped' and e['at'] >= pending[1]['started_at'] for e in self.native_trace()))
        else:
            wait(lambda: any(e['event'] == 'native_spawn' and e['at'] >= pending[1]['started_at'] for e in self.native_trace()))
        self.status_measurement()
        if kind != 'slow-query':
            at = time.monotonic()
            pending[0].send_signal(signal.SIGINT)
            self.case['cancel_sent_at'] = at
        response = self.remember_pending(pending)['response']
        assert response and not response['ok'] and response['error']['code'] in ('cancelled', 'timeout', 'window_query_failed'), response
        if kind != 'slow-query':
            cancelled = next(e for e in self.native_trace() if e['event'] == 'close_cancel'
                             and e['at'] >= self.case['cancel_sent_at'])
            self.case['cancel_dispatch_seconds'] = cancelled['at'] - self.case['cancel_sent_at']
            self.case['cancel_dispatch_under_100ms'] = 0 <= self.case['cancel_dispatch_seconds'] < .1
        save(self.folder / 'fault.json', {'mode': 'normal'})
        assert len(self.rows()) == 1
        self.live_root()
        completes = [e for e in self.native_trace() if e['event'] == 'native_complete']
        assert all(e['clean'] and e['reaped'] and e['temporary_removed'] and
                   (not e['spawned'] or e['script_absent']) for e in completes), completes
        if kind != 'stopped-close':
            self.close_receipts(0)
        else:
            receipts = [e for e in self.fixture_events() if e['event'] == 'close_requested']
            assert len(receipts) <= 1, receipts
            self.case['close_receipts'] = receipts

    def hook(self, timeout=False):
        extra = ('--close-mode', 'refuse') if timeout else ()
        self.launch('--exit-after-ms', '15000', *extra, flags=('--wait-window',))
        row = self.row()
        save(self.folder / 'fault.json', {'mode': 'hook-timeout' if timeout else 'hook-success'})
        result = self.call(self.close_args(row, '1.4' if timeout else '3'), error='timeout' if timeout else None)
        if timeout:
            self.case['hook_timeout_policy'] = 'Hard hook bound may leave adapter cleanup unconfirmed; outer test owner retains independent session cleanup responsibility.'
        else:
            assert result['exited'] is True
        terminal = next(e for e in self.native_trace() if e['event'] == 'hook_terminal')
        constructed = next(e for e in self.native_trace() if e['event'] == 'hook_constructed')
        assert terminal['actual_deadline'] <= terminal['supplied_deadline']
        assert terminal['children_polls'] > constructed['children_polls']
        assert terminal['registry_ticks'] > constructed['registry_ticks']
        assert terminal['terminal_idempotent'] and not terminal['production_shutdown_wiring']
        self.case['hook_qualification'] = terminal
        self.close_receipts()

    def execute(self, selected):
        failed = []
        for label in selected:
            try:
                with self.generation(label):
                    if label in ('normal-window', 'normal-app', 'braced-window'): self.normal(label)
                    elif label == 'ambiguous-app': self.ambiguous()
                    elif label in ('selected-sibling', 'selected-dialog'): self.selection(label.removeprefix('selected-'))
                    elif label == 'confirmation': self.refusal(True)
                    elif label == 'refusal': self.refusal()
                    elif label in ('delay-within', 'delay-beyond'): self.delay(label == 'delay-beyond')
                    elif label == 'descendant': self.descendant()
                    elif label.startswith('queued-'): self.queued(label.removeprefix('queued-'))
                    elif label == 'cancel-before': self.queued('cancel')
                    elif label == 'noop': self.noop()
                    elif label in ('cancel-after', 'disconnect', 'stop-wait', 'bus-death', 'kwin-death', 'worker-death'):
                        self.close_control({'cancel-after': 'cancel', 'disconnect': 'disconnect', 'stop-wait': 'stop',
                                            'bus-death': 'bus', 'kwin-death': 'kwin', 'worker-death': 'worker'}[label])
                    elif label in ('slow-query', 'stopped-query', 'stopped-close'): self.adapter_fault(label)
                    elif label.startswith('hook-'): self.hook(label == 'hook-timeout')
                    elif label == 'restart':
                        self.launch('--exit-after-ms', '12000', flags=('--wait-window',))
                        oldapp, oldwindow, oldgen = self.appref, self.wref(self.row()), self.gen
                if label == 'restart':
                    with self.generation('restart-new'):
                        assert oldgen != self.gen
                        before = time.monotonic()
                        self.call(self.args('close', '--window', oldwindow), error='generation_mismatch')
                        self.call(self.args('close', '--app', oldapp), error='generation_mismatch')
                        assert not any(e['event'] == 'native_start' and e['at'] >= before for e in self.native_trace())
            except Exception:
                failed.append(label)
                self.receipt['cases'].setdefault(label, {}).update(passed=False, failure=traceback.format_exc())
                self.persist()
        self.receipt['failed_cases'] = failed
        self.persist()
        self.temporary.cleanup()
        print(json.dumps({'receipt': str(self.output / 'receipt.json'), 'failed_cases': failed}))
        return bool(failed)


def fixture_control(binary, delay, duration):
    """Finite owned application helper for a dialog introduced while close queues."""
    delay, duration = int(delay), int(duration)
    assert 0 < delay < duration <= 30000
    child = subprocess.Popen([binary, '--exit-after-ms', str(duration)], stdin=subprocess.PIPE,
        env=os.environ | {'HARNESS_GENERATION': os.environ.get('HARNESS_GENERATION', '0' * 32)})
    started = time.monotonic()
    print(json.dumps({'event': 'fixture_control_started', 'at': started,
                      'fixture_pid': os.getpid(), 'child_pid': child.pid}), flush=True)
    try:
        time.sleep(delay / 1000)
        child.stdin.write(b'open dialog\n')
        child.stdin.flush()
        return child.wait(timeout=duration / 1000 + 2)
    finally:
        child.stdin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('output')
    prep.add_argument('--commit', default='HEAD')
    run = commands.add_parser('run')
    for name in ('output', 'dependencies', 'prepared'): run.add_argument(name)
    run.add_argument('--case', action='append', choices=CASES)
    for name, count in (('worker', 4), ('controller', 3), ('fixture-control', 3)):
        commands.add_parser(name).add_argument('values', nargs=count)
    args = parser.parse_args()
    if args.command == 'prepare': prepare(args.output, args.commit)
    elif args.command == 'worker': worker(*args.values)
    elif args.command == 'controller': base.controller(*args.values)
    elif args.command == 'fixture-control': return fixture_control(*args.values)
    else: return Run(args.output, args.dependencies, args.prepared).execute(args.case or CASES)


if __name__ == '__main__':
    raise SystemExit(main())
