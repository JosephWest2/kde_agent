"""Finite installed-wheel issue 24 evidence; all fault seams are private to this file.

python -I installed_targeting.py prepare NEW_PREPARED --commit HEAD
NEW_PREPARED/venv/bin/python -I installed_targeting.py run NEW_OUTPUT DEPENDENCIES NEW_PREPARED
Use run --case NAME repeatedly to select cases. Never run against host endpoints.
Preparation installs only into a newly created venv; it requires a committed tree.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
CASES = ('focus', 'noop', 'delayed', 'before-map-exit', 'launch-retention',
         'launch-cancel', 'launch-disconnect', 'queued-resize', 'queued-vanish',
         'focus-vanish', 'subtree-exit', 'slow-query', 'stopped-query',
         'stop-wait', 'bus-death', 'kwin-death', 'worker-death', 'focus-transition', 'restart')


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    """Diagnostic atomic replace without fsync; production durability is untouched."""
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.tmp')
    raw = json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False).encode()
    assert len(raw) <= 16 * 1024 * 1024
    with temporary.open('xb') as stream:
        stream.write(raw)
    os.replace(temporary, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes(root):
    return {p.name: digest(p) for p in Path(root).iterdir() if p.suffix in ('.py', '.js')}


def wait(condition, seconds=5):
    end = time.monotonic() + seconds
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < end, 'evidence wait expired'
        time.sleep(.005)


def events(path):
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except ValueError:
            # The final line may be being written. Earlier corrupt lines fail.
            assert line == lines[-1], line
    return result


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(fields[19]),
            'cgroup': Path('/proc', str(pid), 'cgroup').read_text().strip()}


def prepare(output, commit):
    output = Path(output).resolve()
    assert not subprocess.check_output(['git', '-C', str(PROJECT), 'status', '--porcelain',
                                         '--untracked-files=no'], text=True), 'Commit tracked changes first'
    resolved = subprocess.check_output(['git', '-C', str(PROJECT), 'rev-parse', commit + '^{commit}'], text=True).strip()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = output / 'source'
    source.mkdir(mode=0o700)
    archive = output / 'source.tar'
    subprocess.run(['git', '-C', str(PROJECT), 'archive', '--format=tar', '-o', str(archive), resolved], check=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter='data')
    # GI comes from the system distribution; package code is installed into this venv.
    subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(output / 'venv')], check=True)
    python = output / 'venv/bin/python'
    subprocess.run([str(python), '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    '--wheel-dir', str(output / 'wheels'), str(source)], check=True)
    wheel, = (output / 'wheels').glob('*.whl')
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps', str(wheel)], check=True)
    receipt = {'commit': resolved, 'archive_sha256': digest(archive), 'wheel': str(wheel),
               'wheel_sha256': digest(wheel), 'source': str(source), 'python': str(python),
               'runner_sha256': digest(source / 'evidence/issue-24/installed_targeting.py')}
    save(output / 'prepared.json', receipt)
    print(json.dumps(receipt))


def worker(name, generation, artifacts, binary):
    import agent_desktop
    from gi.repository import GLib
    from agent_desktop import windows, targeting
    from agent_desktop.worker import run
    assert not Path(agent_desktop.__file__).resolve().is_relative_to(PROJECT / 'src')
    folder = Path(artifacts) / 'generations' / generation
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = (folder / 'targeting-trace.jsonl').open('x', buffering=1)
    count = total = 0
    samples = {'last': time.monotonic(), 'max_glib_gap': 0, 'heartbeat_count': 0}
    def emit(event, **values):
        nonlocal count, total
        raw = json.dumps({'at': time.monotonic(), 'event': event, **values}, allow_nan=False) + '\n'
        count += 1
        total += len(raw)
        assert count <= 10000 and total <= 4 * 1024 * 1024, 'bounded evidence trace exhausted'
        stream.write(raw)
    def heartbeat():
        now = time.monotonic()
        samples['max_glib_gap'] = max(samples['max_glib_gap'], now - samples['last'])
        samples['last'] = now
        samples['heartbeat_count'] += 1
        return True
    GLib.timeout_add(5, heartbeat)
    def mode():
        path = folder / 'fault.json'
        return read(path)['mode'] if path.exists() else 'normal'
    def instrument(base, kind):
        class Native(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.evidence_mode = mode() if not self.request_id.startswith('readiness') else 'normal'
                self.evidence_done = self.evidence_stopped = False
                emit('native_start', kind=kind, request_id=self.request_id, query_id=self.id,
                     deadline=self.deadline, mode=self.evidence_mode,
                     window=getattr(self, 'window', None))
            def _prepare_script(self):
                if kind == 'activation' and self.evidence_mode == 'noop':
                    # Real successful native kwinscript, fixed source, no focus effect.
                    windows.Query._prepare_script(self)
                    (self.folder / 'input.js').write_text('output_result("noop");')
                else:
                    super()._prepare_script()
                    if kind == 'query' and self.evidence_mode in ('slow', 'stopped'):
                        script = ('var until = Date.now() + 800; while (Date.now() < until) {} output_result("late");'
                                  if self.evidence_mode == 'slow' else 'callDBus = function () {};')
                        (self.folder / 'input.js').write_text(script)
            def _argv(self):
                if kind == 'activation' and self.evidence_mode == 'noop':
                    return [self.owner.binary, '--name', self.name, 'kwinscript', '--file', str(self.folder / 'input.js')]
                return super()._argv()
            def _spawn(self, remove=False):
                super()._spawn(remove)
                child = self.remover if remove else self.child
                emit('native_spawn', kind=kind, request_id=self.request_id, query_id=self.id,
                     remove=remove, pid=child.process.pid, argv=child.process.args,
                     script_name=self.name)
            def step(self):
                result = super().step()
                if self.evidence_mode == 'stopped' and self.child is not None and not self.evidence_stopped:
                    if self.bus.connection is not None and 'evidence-loaded' not in self.bus.pending:
                        def loaded(value, fds, error):
                            if not error and value.unpack() == (True,) and self.child.returncode is None and not self.evidence_stopped:
                                os.kill(self.child.process.pid, signal.SIGSTOP)
                                self.evidence_stopped = True
                                emit('native_stopped', request_id=self.request_id, query_id=self.id, pid=self.child.process.pid)
                        self.bus.call('evidence-loaded', 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                                      'isScriptLoaded', GLib.Variant('(s)', (self.name,)), '(b)', self.deadline, loaded)
                if result is not None:
                    self.receipt(True)
                return result
            def cancel(self, cause='cancelled'):
                if self.error is None:
                    emit('native_cancel', request_id=self.request_id, query_id=self.id,
                         cause=getattr(cause, 'code', cause))
                return super().cancel(cause)
            def cleanup(self, deadline=None):
                clean = super().cleanup(deadline)
                if clean or time.monotonic() >= self.cleanup_deadline:
                    self.receipt(clean)
                return clean
            def receipt(self, clean):
                if self.evidence_done:
                    return
                self.evidence_done = True
                emit('native_complete', kind=kind, request_id=self.request_id, query_id=self.id,
                     deadline=self.deadline, accepted_at=self.accepted_at, clean=clean,
                     spawned=self.spawned, script_absent=self.absent,
                     reaped=self.child is None or self.child.returncode is not None,
                     returncode=None if self.child is None else self.child.returncode,
                     temporary_removed=self.folder is None or not self.folder.exists(),
                     cleanup_deadline=self.cleanup_deadline, error=None if self.error is None else self.error.code,
                     max_glib_gap=samples['max_glib_gap'])
        return Native
    windows.Query = instrument(windows.Query, 'query')
    windows.Activation = instrument(windows.Activation, 'activation')
    original = targeting.TargetTask
    class ObservedTarget(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.evidence_phase = None
            emit('target_start', request_id=self.request.request_id, condition=self.condition,
                 admitted_at=self.context.work.admission.admitted_at, deadline=self.deadline)
        def step(self, now):
            if self.phase != self.evidence_phase:
                emit('target_phase', request_id=self.request.request_id, phase=self.phase, polls=self.polls)
                self.evidence_phase = self.phase
            result = super().step(now)
            if result is not None:
                emit('target_complete', request_id=self.request.request_id, polls=self.polls)
            return result
        def request_cancel(self, reason):
            emit('target_cancel', request_id=self.request.request_id, reason=reason)
            return super().request_cancel(reason)
    targeting.TargetTask = ObservedTarget
    try:
        run(name, generation, artifacts=artifacts, managed=True, desktop=True, kdotool=binary)
    finally:
        emit('worker_trace_end', **samples)
        stream.close()


def controller(name, artifacts, dependencies):
    from agent_desktop.lifecycle import Manager, Systemd
    from agent_desktop.prerequisites import check
    from agent_desktop.contracts import make_request
    binary = check(dependencies, time.monotonic() + 30)['kdotool']['executable']
    class Injected(Systemd):
        def start(self, data, runtime, command, deadline):
            return super().start(data, runtime, [sys.executable, '-I', str(SCRIPT), 'worker', name,
                                data['generation'], artifacts, binary], deadline)
    request = make_request('session.start', session=name, caller_cwd='/',
                           arguments={'artifacts': artifacts, 'dependency_root': dependencies})
    print(json.dumps(Manager(systemd=Injected()).start(request)), flush=True)


class Run:
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
        assert hashes(self.source / 'src/agent_desktop') == hashes(installed.parent)
        assert digest(SCRIPT) == self.prepared['runner_sha256'], 'Runner must match prepared commit'
        assert digest(self.prepared['wheel']) == self.prepared['wheel_sha256']
        spec = importlib.util.spec_from_file_location('fixture_build', self.source / 'tools/private_harness.py')
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        self.fixture, fixture_build = harness.build(self.output / 'fixture-build')
        from agent_desktop.prerequisites import check
        dependency_receipt = check(str(self.dependencies), time.monotonic() + 30)
        self.receipt = {'prepared': self.prepared, 'installed_module': str(installed),
                        'installed_hashes': hashes(installed.parent), 'fixture': fixture_build,
                        'dependencies': dependency_receipt, 'cases': {}, 'release_qualified': False,
                        'diagnostic_durability': 'atomic-replace-or-jsonl-without-fsync'}
        self.artifacts = self.output / 'artifacts'
        self.temporary = tempfile.TemporaryDirectory(prefix='a24-')
        self.runtime = Path(self.temporary.name)
        os.environ['XDG_RUNTIME_DIR'] = str(self.runtime)
        self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': str(self.runtime)}
        self.cli = Path(sys.executable).with_name('agent-desktop')
        self.persist()

    def persist(self):
        save(self.output / 'receipt.json', self.receipt)

    def pending(self, args):
        argv = [str(self.cli), '--json', *args]
        record = {'argv': argv, 'started_at': time.monotonic()}
        child = subprocess.Popen(argv, cwd='/', env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        record['cli_pid'] = child.pid
        return child, record

    def collect(self, pending):
        child, record = pending
        stdout, stderr = child.communicate(timeout=65)
        record.update(ended_at=time.monotonic(), exit_code=child.returncode, stdout=stdout, stderr=stderr)
        record['elapsed'] = record['ended_at'] - record['started_at']
        record['response'] = json.loads(stdout) if stdout.strip() else None
        return record

    def invoke(self, args):
        return self.collect(self.pending(args))

    def call(self, args, *, error=None):
        result = self.invoke(args)
        # Store each public attempt before asserting, including every negative.
        self.case.setdefault('calls', []).append(result)
        self.persist()
        response = result['response']
        assert response is not None, result
        if error is None:
            assert response['ok'], result
        else:
            assert not response['ok'] and response['error']['code'] in (error if isinstance(error, tuple) else (error,)), result
        return response.get('result') if error is None else response

    @contextmanager
    def generation(self, label):
        self.name = 'a24-' + label
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
        try:
            yield
            self.case['passed'] = True
        except Exception:
            self.case['passed'] = False
            self.case['failure'] = traceback.format_exc()
            raise
        finally:
            self.case['stop'] = self.invoke(['session', 'stop', '--session', self.name])
            group = Path('/sys/fs/cgroup' + data['cgroup'])
            wait(lambda: not group.exists() or 'populated 0' in (group / 'cgroup.events').read_text(), 16)
            self.case['cgroup_empty_at'] = time.monotonic()
            self.case['trace'] = events(self.folder / 'targeting-trace.jsonl')
            self.case['retained_service_records'] = {name: read(self.folder / name)
                for name in ('terminal.json', 'reconciliation.json', 'manifest.json', 'shutdown.json', 'startup-failure.json')
                if (self.folder / name).exists()}
            starts = {}
            for event in self.case['trace']:
                if event['event'] == 'native_start' and event['kind'] == 'query':
                    starts.setdefault(event['request_id'], []).append(event['at'])
            intervals = [b - a for values in starts.values() for a, b in zip(values, values[1:])]
            self.case['query_poll_intervals'] = {'scope': 'query starts per request; activation excluded',
                'count': len(intervals), 'minimum': min(intervals, default=None), 'samples': intervals}
            self.case['request_records'] = [str(p) for p in self.folder.glob('requests/*/*/record.json')]
            self.persist()

    def args(self, operation, *args):
        return [operation, '--session', self.name, *args]

    def launch(self, *fixture_args, flags=()):
        result = self.call(self.args('launch', *flags, '--', str(self.fixture), '--autonomous', *fixture_args))
        self.launched = result
        self.app = result['application']
        self.appref = self.app['generation'] + ':' + self.app['application_id']
        return result

    def fixture_events(self):
        return events(self.launched['logs']['stdout'])

    def app_record(self):
        return read(self.folder / 'applications' / self.app['application_id'] / 'record.json')

    def rows(self):
        return self.call(self.args('windows', '--app', self.appref))['windows']

    def wref(self, row):
        return row['window']['generation'] + ':' + row['window']['window_id']

    def wait_args(self, condition, ref=None, timeout='2'):
        selector = ['--window', ref] if condition == 'focus' else ['--app', ref or self.appref]
        return self.args('wait', '--for', condition, *selector, '--timeout', timeout)

    def native_trace(self):
        return events(self.folder / 'targeting-trace.jsonl')

    def target_started(self, after, condition=None):
        return wait(lambda: next((e for e in self.native_trace() if e['event'] == 'target_start'
                    and e['at'] >= after and (condition is None or e['condition'] == condition)), None))

    def remember_pending(self, pending):
        value = self.collect(pending)
        self.case.setdefault('calls', []).append(value)
        self.persist()
        return value

    def focus(self):
        self.launch('--sibling', '--dialog', '--child-window-ms', '45000', '--exit-after-ms', '50000',
                    flags=('--wait-window',))
        rows = wait(lambda: (found if len(found := self.rows()) == 4 else None))
        self.case['owned_identities'] = [identity(pid) for pid in sorted({r['pid'] for r in rows})]
        before = len([e for e in self.native_trace() if e['event'] == 'native_start' and e['kind'] == 'activation'])
        ambiguity = self.call(self.args('focus', '--app', self.appref), error='target_ambiguous')
        assert len(ambiguity['error']['context']['candidates']) == 4, ambiguity
        after = len([e for e in self.native_trace() if e['event'] == 'native_start' and e['kind'] == 'activation'])
        assert before == after
        primary = next(r for r in rows if r['client']['width'] == 640)
        sibling = next(r for r in rows if r['client']['width'] == 480)
        self.case['transitions'] = []
        for index in range(10):
            row, label = (primary, 'primary') if index % 2 == 0 else (sibling, 'sibling')
            at = time.monotonic()
            focused = self.call(self.args('focus', '--window', self.wref(row)))
            assert focused['focused'] and focused['window'] == row['window']
            snapshot = self.call(self.args('windows', '--app', self.appref))
            assert snapshot['active_window'] == row['window']
            activated = wait(lambda: next((e for e in reversed(self.fixture_events())
                if e['event'] == 'configure' and e.get('surface') == label and e.get('activated')
                and e['monotonic_ns'] / 1e9 >= at), None))
            self.case['transitions'].append({'focus': focused, 'snapshot': snapshot, 'fixture': activated})
        self.call(self.wait_args('focus', self.wref(sibling)))
        self.call(self.args('focus', '--window', self.gen + ':{' + primary['window']['window_id'].upper() + '}'))
        self.call(self.args('focus', '--window', self.wref(sibling)))
        self.call(self.wait_args('focus', self.wref(primary), '.4'), error='timeout')
        missing = self.gen + ':00000000-0000-0000-0000-000000000001'
        self.call(self.args('focus', '--window', missing), error='target_not_found')

    def noop(self):
        self.launch('--sibling', '--exit-after-ms', '10000', flags=('--wait-window',))
        rows = wait(lambda: (found if len(found := self.rows()) == 2 else None))
        target, other = rows
        self.call(self.args('focus', '--window', self.wref(other)))
        save(self.folder / 'fault.json', {'mode': 'noop'})
        self.call(self.args('focus', '--window', self.wref(target), '--timeout', '1.2'), error='timeout')
        activation = [e for e in self.native_trace() if e['event'] == 'native_complete' and e['kind'] == 'activation'][-1]
        assert activation['returncode'] == 0 and activation['clean'], activation
        snapshot = self.call(self.args('windows', '--app', self.appref))
        assert snapshot['active_window'] == other['window'], snapshot

    def focus_transition(self):
        self.launch('--sibling', '--exit-after-ms', '7000', flags=('--wait-window',))
        rows = wait(lambda: (found if len(found := self.rows()) == 2 else None))
        primary = next(r for r in rows if r['client']['width'] == 640)
        sibling = next(r for r in rows if r['client']['width'] == 480)
        self.call(self.args('focus', '--window', self.wref(sibling)))
        pending = self.pending(self.wait_args('focus', self.wref(primary), '3'))
        self.target_started(pending[1]['started_at'], 'focus')
        # Fixed evidence-only control on this generation's private bus. This is
        # not public focus queued behind wait, and accepts no script source.
        root = self.runtime / 'agent-desktop/g' / self.gen / 'desktop'
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': str(root),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(root / 'bus'),
               'WAYLAND_DISPLAY': 'wayland-private'}
        binary = self.receipt['dependencies']['kdotool']['executable']
        name = 'evidence-focus-' + self.gen
        argv = [binary, '--name', name, 'windowactivate', '{' + primary['window']['window_id'] + '}']
        at = time.monotonic()
        child = subprocess.run(argv, env=env, cwd='/', capture_output=True, text=True, timeout=2)
        self.case['private_focus_transition'] = {'argv': argv, 'at': at, 'exit_code': child.returncode,
                                                'stdout': child.stdout, 'stderr': child.stderr,
                                                'private_bus': env['DBUS_SESSION_BUS_ADDRESS']}
        assert child.returncode == 0, self.case['private_focus_transition']
        response = self.remember_pending(pending)['response']
        assert response['ok'] and response['result']['focused'] and response['result']['window'] == primary['window'], response
        self.case['fixture_activation'] = wait(lambda: next((e for e in reversed(self.fixture_events())
            if e['event'] == 'configure' and e.get('surface') == 'primary' and e.get('activated')
            and e['monotonic_ns'] / 1e9 >= at), None))
        cleanup = subprocess.run([binary, '--remove', name], env=env, cwd='/', capture_output=True, text=True, timeout=2)
        self.case['private_focus_cleanup'] = {'exit_code': cleanup.returncode, 'stdout': cleanup.stdout, 'stderr': cleanup.stderr}
        assert cleanup.returncode == 0, self.case['private_focus_cleanup']

    def delayed(self):
        self.launch('--window-delay-ms', '500', '--sibling', '--exit-after-ms', '6000', flags=('--wait-window',))
        rows = self.rows()
        assert len(rows) == 2, rows
        self.call(self.wait_args('window'))
        self.call(self.args('focus', '--window', self.wref(rows[0])))

    def before_map_exit(self):
        self.call(self.args('launch', '--wait-window', '--', str(self.fixture), '--autonomous',
                           '--window-delay-ms', '3000', '--exit-after-ms', '100'), error='application_exited')

    def launch_retention(self):
        response = self.call(self.args('launch', '--wait-window', '--timeout', '.6', '--', str(self.fixture),
                           '--autonomous', '--window-delay-ms', '1800', '--exit-after-ms', '6000'), error='timeout')
        partial = response['error']['partial_result']
        self.launched, self.app = partial, partial['application']
        self.appref = self.gen + ':' + self.app['application_id']
        self.case['retained_identity'] = identity(partial['process']['pid'])
        self.case['retained_record'] = self.app_record()
        result = self.call(self.wait_args('window', timeout='3'))
        assert len(result['windows']) == 1
        self.case['fixture_events'] = self.fixture_events()

    def launch_control(self, disconnect=False):
        pending = self.pending(self.args('launch', '--wait-window', '--timeout', '5', '--', str(self.fixture),
                            '--autonomous', '--window-delay-ms', '2400', '--exit-after-ms', '7000'))
        started = self.target_started(pending[1]['started_at'], 'window')
        records = list((self.folder / 'applications').glob('*/record.json'))
        assert len(records) == 1
        record = read(records[0])
        self.case['retained_before'] = record
        root_before = identity(record['process']['pid'])
        self.case['root_identity_before'] = root_before
        status = self.invoke(['session', 'status', '--session', self.name])
        self.case['status_during_wait'] = status
        assert status['response']['ok'], status
        status_records = list(self.folder.glob('requests/' + status['response']['request_id'] + '/*/record.json'))
        assert len(status_records) == 1
        timing = read(status_records[0])
        self.case['status_worker_seconds'] = timing['terminal_observed_monotonic'] - timing['admitted_at']
        assert self.case['status_worker_seconds'] < .1, timing
        action = time.monotonic()
        pending[0].send_signal(signal.SIGKILL if disconnect else signal.SIGINT)
        control_response = self.remember_pending(pending)
        if not disconnect:
            assert control_response['response']['error']['code'] == 'cancelled', control_response
        cancelled = wait(lambda: next((e for e in self.native_trace() if e['event'] == 'target_cancel'
                            and e['request_id'] == started['request_id']), None))
        self.case['cancel_dispatch_seconds'] = cancelled['at'] - action
        assert 0 <= self.case['cancel_dispatch_seconds'] < .1
        self.appref = self.gen + ':' + records[0].parent.name
        self.app = {'generation': self.gen, 'application_id': records[0].parent.name}
        self.case['retained_after'] = self.app_record()
        self.case['root_identity_after'] = identity(record['process']['pid'])
        assert self.case['root_identity_after'] == root_before
        result = self.call(self.wait_args('window', timeout='3'))
        assert len(result['windows']) == 1

    def queued(self, vanished=False):
        mutation = ('--destroy-after-ms', 'primary:1300') if vanished else ('--resize-after-ms', 'primary:1300:700:400')
        self.launch('--sibling', '--exit-after-ms', '7000', *mutation, flags=('--wait-window',))
        rows = wait(lambda: (found if len(found := self.rows()) == 2 else None))
        primary = next(r for r in rows if r['client']['width'] == 640)
        sibling = next(r for r in rows if r['client']['width'] == 480)
        self.call(self.args('focus', '--window', self.wref(sibling)))
        schedule = next(e for e in self.fixture_events() if e['event'] == 'surface_schedule' and e['surface'] == 'primary')
        mutation_at = schedule['started_ns'] / 1e9 + 1.3
        block_seconds = mutation_at + .15 - time.monotonic()
        assert .3 < block_seconds < 1.5, 'fixture setup missed bounded queued-mutation window'
        blocker = self.pending(self.wait_args('exit', timeout=str(block_seconds)))
        self.target_started(blocker[1]['started_at'], 'exit')
        queued = self.pending(self.args('focus', '--window', self.wref(primary), '--timeout', '2'))
        assert queued[1]['started_at'] < mutation_at
        self.case['blocker'] = self.remember_pending(blocker)
        assert self.case['blocker']['response']['error']['code'] == 'timeout'
        response = self.remember_pending(queued)['response']
        if vanished:
            assert not response['ok'] and response['error']['code'] == 'target_not_found', response
        else:
            assert response['ok'] and response['result']['client']['width'] == 700 and response['result']['client']['height'] == 400, response
        self.case['fixture_events'] = self.fixture_events()
        event_name = 'scheduled_destroy' if vanished else 'scheduled_resize'
        assert any(e['event'] == event_name and e['applied'] for e in self.case['fixture_events'])

    def focus_vanish(self):
        self.launch('--sibling', '--destroy-after-ms', 'primary:1500', '--exit-after-ms', '5000', flags=('--wait-window',))
        rows = wait(lambda: (found if len(found := self.rows()) == 2 else None))
        primary = next(r for r in rows if r['client']['width'] == 640)
        sibling = next(r for r in rows if r['client']['width'] == 480)
        self.call(self.args('focus', '--window', self.wref(sibling)))
        self.call(self.wait_args('focus', self.wref(primary), '3'), error='target_lost')

    def subtree_exit(self):
        self.launch('--exit-after-ms', '250', '--descendant-ms', '1800')
        descendant = wait(lambda: next((e for e in self.fixture_events() if e['event'] == 'descendant_spawned'), None))
        self.case['descendant_identity'] = identity(descendant['descendant_pid'])
        fd = os.pidfd_open(descendant['descendant_pid'])
        try:
            self.call(self.wait_args('exit', timeout='.6'), error='timeout')
            self.case['root_exited_record'] = self.app_record()
            assert self.case['root_exited_record']['state'] == 'root-exited'
            signal.pidfd_send_signal(fd, 0)
            self.call(self.wait_args('exit', timeout='3'))
            self.call(self.wait_args('exit', timeout='.5'))
            import select
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            assert poll.poll(100), 'descendant pidfd must indicate exit'
        finally:
            os.close(fd)

    def query_fault(self, mode):
        self.launch('--window-delay-ms', '1500', '--exit-after-ms', '7000')
        save(self.folder / 'fault.json', {'mode': mode})
        pending = self.pending(self.wait_args('window', timeout='2'))
        if mode == 'stopped':
            wait(lambda: any(e['event'] == 'native_stopped' and e['at'] >= pending[1]['started_at'] for e in self.native_trace()))
            pending[0].send_signal(signal.SIGINT)
        response = self.remember_pending(pending)['response']
        assert response and not response['ok'], response
        assert response['error']['code'] in ('cancelled', 'timeout'), response
        save(self.folder / 'fault.json', {'mode': 'normal'})
        self.call(self.wait_args('window', timeout='3'))
        completed = [e for e in self.native_trace() if e['event'] == 'native_complete']
        assert all(e['clean'] and e['reaped'] and e['temporary_removed'] and (not e['spawned'] or e['script_absent']) for e in completed), completed

    def failure_during_wait(self, kind):
        self.launch('--exit-after-ms', '8000')
        pending = self.pending(self.wait_args('exit', timeout='5'))
        self.target_started(pending[1]['started_at'], 'exit')
        if kind == 'stop':
            self.case['explicit_stop'] = self.invoke(['session', 'stop', '--session', self.name])
        else:
            group = Path('/sys/fs/cgroup' + self.case['metadata']['cgroup'])
            matches = []
            for member in group.rglob('cgroup.procs'):
                for pid in member.read_text().split():
                    try:
                        if ((kind == 'worker' and str(SCRIPT).encode() in Path('/proc', pid, 'cmdline').read_bytes()
                             and b'\x00worker\x00' in Path('/proc', pid, 'cmdline').read_bytes())
                            or (kind != 'worker' and Path('/proc', pid, 'comm').read_text().strip() == ('dbus-daemon' if kind == 'bus' else 'kwin_wayland'))):
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
        if kind == 'worker':
            assert response['error']['code'] in ('session_unavailable', 'session_failed', 'completion_unknown'), response
            self.case['worker_death_response_class'] = ('authenticated-generation-failure' if response['error']['code'] == 'session_failed' else 'transport-completion-unknown')
        else:
            assert response['error']['code'] == ('cancelled' if kind == 'stop' else 'session_failed'), response

    def execute(self, selected):
        failed = []
        for label in selected:
            try:
                with self.generation(label):
                    if label == 'focus': self.focus()
                    elif label == 'noop': self.noop()
                    elif label == 'delayed': self.delayed()
                    elif label == 'before-map-exit': self.before_map_exit()
                    elif label == 'launch-retention': self.launch_retention()
                    elif label == 'launch-cancel': self.launch_control()
                    elif label == 'launch-disconnect': self.launch_control(True)
                    elif label == 'queued-resize': self.queued()
                    elif label == 'queued-vanish': self.queued(True)
                    elif label == 'focus-vanish': self.focus_vanish()
                    elif label == 'subtree-exit': self.subtree_exit()
                    elif label == 'slow-query': self.query_fault('slow')
                    elif label == 'stopped-query': self.query_fault('stopped')
                    elif label == 'stop-wait': self.failure_during_wait('stop')
                    elif label == 'bus-death': self.failure_during_wait('bus')
                    elif label == 'kwin-death': self.failure_during_wait('kwin')
                    elif label == 'worker-death': self.failure_during_wait('worker')
                    elif label == 'focus-transition': self.focus_transition()
                    elif label == 'restart':
                        self.launch('--exit-after-ms', '5000', flags=('--wait-window',))
                        oldapp, oldwindow, oldgen = self.appref, self.wref(self.rows()[0]), self.gen
                if label == 'restart':
                    with self.generation('restart-new'):
                        assert self.gen != oldgen
                        before = len(self.native_trace())
                        self.call(self.args('focus', '--window', oldwindow), error='generation_mismatch')
                        self.call(self.args('wait', '--for', 'exit', '--app', oldapp), error='generation_mismatch')
                        assert not any(e['event'] == 'native_start' for e in self.native_trace()[before:])
            except Exception:
                failed.append(label)
                self.receipt['cases'].setdefault(label, {}).update(passed=False, failure=traceback.format_exc())
                self.persist()
        self.receipt['failed_cases'] = failed
        self.persist()
        self.temporary.cleanup()
        print(json.dumps({'receipt': str(self.output / 'receipt.json'), 'failed_cases': failed}))
        return bool(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('output')
    prep.add_argument('--commit', default='HEAD')
    run = commands.add_parser('run')
    run.add_argument('output')
    run.add_argument('dependencies')
    run.add_argument('prepared')
    run.add_argument('--case', action='append', choices=CASES)
    worker_parser = commands.add_parser('worker')
    worker_parser.add_argument('values', nargs=4)
    controller_parser = commands.add_parser('controller')
    controller_parser.add_argument('values', nargs=3)
    args = parser.parse_args()
    if args.command == 'prepare': prepare(args.output, args.commit)
    elif args.command == 'worker': worker(*args.values)
    elif args.command == 'controller': controller(*args.values)
    else: return Run(args.output, args.dependencies, args.prepared).execute(args.case or CASES)


if __name__ == '__main__':
    raise SystemExit(main())
