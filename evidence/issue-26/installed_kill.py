"""Finite installed-wheel issue 26 kill evidence. Never run suites concurrently.

python -I installed_kill.py prepare NEW_PREPARED --commit HEAD
NEW_PREPARED/venv/bin/python -I installed_kill.py run NEW_OUTPUT DEPENDENCIES NEW_PREPARED
Repeat --case NAME to select cases. All output directories must be new.
Fault seams and finite fixtures are evidence-only; public requests use installed CLI.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time
import traceback

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
SUPPORT = PROJECT / 'evidence/issue-25/installed_close.py'
FIXTURE = SCRIPT.with_name('kill_fixture.py')
spec = importlib.util.spec_from_file_location('issue25_kill_support', SUPPORT)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
base = support.base
support.SCRIPT = base.SCRIPT = SCRIPT
read, save, digest, hashes = base.read, base.save, base.digest, base.hashes
wait, events, identity = base.wait, base.events, base.identity
CASES = ('normal', 'tree', 'root-first', 'late', 'kill-late', 'stopped-child', 'migration', 'cancel', 'disconnect',
         'queued-expiry', 'cancel-before', 'timeout-recovery', 'confirmation',
         'dispatch-timeout',
         'window-timeout', 'launch-timeout', 'sentinels', 'restart',
         'stop', 'bus-death', 'kwin-death', 'worker-death')


def prepare(output, commit):
    base.prepare(output, commit)
    receipt = read(Path(output) / 'prepared.json')
    source = Path(receipt['source'])
    receipt['runner_sha256'] = digest(source / 'evidence/issue-26/installed_kill.py')
    receipt['support_hashes'] = {name: digest(source / name) for name in (
        'evidence/issue-24/installed_targeting.py', 'evidence/issue-25/installed_close.py',
        'evidence/issue-26/kill_fixture.py')}
    save(Path(output) / 'prepared.json', receipt)
    print(json.dumps(receipt))


def worker(name, generation, artifacts, binary):
    """Record actual signal submission and task phases without granting authority."""
    from agent_desktop import terminating, app_processes
    folder = Path(artifacts) / 'generations' / generation
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = (folder / 'kill-trace.jsonl').open('x', buffering=1)
    counts = {'events': 0, 'bytes': 0}
    active = {'request_id': None}
    fault_state = {'kill_started': None, 'reported': False, 'migration': None, 'retained_reported': False}
    def fault():
        path = folder / 'kill-fault.json'
        return read(path) if path.exists() else {}
    def emit(event, **values):
        raw = json.dumps({'event': event, 'at': time.monotonic(), **values}, allow_nan=False) + '\n'
        counts['events'] += 1
        counts['bytes'] += len(raw)
        assert counts['events'] < 30000 and counts['bytes'] < 8 * 1024 * 1024
        stream.write(raw)
    original_signal = signal.pidfd_send_signal
    def observed_signal(fd, sig, *args, **kwargs):
        entered_at = time.monotonic()
        # fdinfo is evidence only. Actual authority remains the product's fd.
        info = Path('/proc/self/fdinfo', str(fd)).read_text()
        pid = next((int(line.split()[1]) for line in info.splitlines() if line.startswith('Pid:')), -1)
        try:
            target = identity(pid) if pid > 0 else None
        except FileNotFoundError:
            target = None
        at = time.monotonic()
        try:
            result = original_signal(fd, sig, *args, **kwargs)
        except OSError as exc:
            emit('pidfd_signal', wrapper_entered_at=entered_at, submitted_at=at,
                 wrapper_identity_seconds=at - entered_at, fd=fd, signal=int(sig), identity=target,
                 request_id=active['request_id'], submitted=False, errno=exc.errno)
            raise
        emit('pidfd_signal', wrapper_entered_at=entered_at, submitted_at=at,
             wrapper_identity_seconds=at - entered_at, fd=fd, signal=int(sig), identity=target,
             request_id=active['request_id'], submitted=True)
        return result
    signal.pidfd_send_signal = observed_signal
    original_visit = app_processes.Termination.visit
    def visit(self, key, fd):
        injected = fault()
        if self.phase == 'kill' and injected.get('mode') == 'defer-kill':
            if not fault_state['reported']:
                emit('evidence_dispatch_deferral', mode='defer-kill', scope='KILL visitation only',
                     request_id=active['request_id'], deadline=self.deadline)
                fault_state['reported'] = True
            return
        if (self.phase == 'kill' and injected.get('mode') == 'kill-late'
                and key[0] == injected['held_pid']):
            started = fault_state['kill_started']
            if started is None or time.monotonic() < started + .2:
                return
        before = self.counts['kill_submitted']
        result = original_visit(self, key, fd)
        if (injected.get('mode') == 'kill-late' and self.counts['kill_submitted'] > before
                and fault_state['kill_started'] is None):
            fault_state['kill_started'] = time.monotonic()
            Path(injected['trigger']).write_text('first real KILL submitted\n')
            emit('evidence_late_trigger', first_kill_at=fault_state['kill_started'],
                 held_pid=injected['held_pid'], bounded_deferral_seconds=.2)
        return result
    app_processes.Termination.visit = visit
    original_tick = app_processes.Registry.tick
    def registry_tick(self):
        injected = fault()
        app = self.active
        if injected.get('mode') != 'migration' or app is None:
            return original_tick(self)
        key = (injected['pid'], injected['start_ticks'])
        if app.termination is None:
            result = original_tick(self)
            if key in app.handles and not fault_state['retained_reported']:
                assert app_processes.live(app.handles[key])
                emit('migration_retained', identity=identity(key[0]), retained_key=key,
                     retained_fd=app.handles[key], application=app.handle)
                fault_state['retained_reported'] = True
            return result
        fd = app.handles.get(key)
        # Acquisition is observed, never inferred from elapsed time or a log PID.
        if fault_state['migration'] is None:
            assert fd is not None and fault_state['retained_reported']
            assert app_processes.live(fd) and self.identity_index[key[0]][:2] == (app, key)
            before = identity(key[0])
            assert before['start_ticks'] == key[1] and before['cgroup'] == '0::' + app.cgroup
            destination = self.root / 'evidence-migrated'
            destination.mkdir(mode=0o700)
            (destination / 'cgroup.procs').write_text(str(key[0]))
            after = identity(key[0])
            assert after['start_ticks'] == key[1] and after['cgroup'] == '0::' + self.cgroup + '/evidence-migrated'
            fault_state['migration'] = time.monotonic()
            Path(injected['trigger']).write_text('verified child migration completed\n')
            emit('controlled_migration', request_id=active['request_id'], before=before, after=after,
                 retained_fd=fd, retained_live=app_processes.live(fd), application=app.handle)
        assert time.monotonic() - fault_state['migration'] < 2, 'Finite controlled root exit did not settle'
        if app.child.returncode is None or app.populated():
            # Children remains pumped by the real worker. Only the evidence
            # observer is deferred until the exact retained-live/empty condition.
            return
        live = app_processes.live(fd)
        assert live and not app.completed
        emit('migration_empty_before_observer', request_id=active['request_id'], root_returncode=app.child.returncode,
             subtree_populated=False, retained_live=live, retained_identity=identity(key[0]), completed=app.completed)
        try:
            return original_tick(self)
        finally:
            emit('migration_observer_returned', request_id=active['request_id'], completed=app.completed,
                 active_owner_retained=self.active is app,
                 ownership_uncertain=app.uncertain, retained_live=fd in app.handles.values() and app_processes.live(fd),
                 process_state=app.process_state)
    app_processes.Registry.tick = registry_tick
    original_task = terminating.KillTask
    class ObservedKill(original_task):
        def __init__(self, request, context, *args, **kwargs):
            self.evidence_request = request
            self.evidence_phase = None
            self.evidence_context = context
            super().__init__(request, context, *args, **kwargs)
            active['request_id'] = request.request_id
            emit('kill_start', request_id=request.request_id,
                 application=request.arguments['app'],
                 admitted_at=context.work.admission.admitted_at,
                 deadline=context.work.admission.deadline)
        def step(self, now):
            try:
                result = super().step(now)
            except Exception:
                emit('kill_failed', request_id=self.evidence_request.request_id, projection=self.projection())
                raise
            partial = self.evidence_context.work.partial or {}
            state = partial.get('kill_state', {})
            signature = json.dumps(state, sort_keys=True)
            if signature != self.evidence_phase:
                self.evidence_phase = signature
                emit('kill_phase', request_id=self.evidence_request.request_id, kill_state=state)
            if result is not None:
                emit('kill_complete', request_id=self.evidence_request.request_id, result=result)
                active['request_id'] = None
            return result
        def request_cancel(self, reason):
            super().request_cancel(reason)
            emit('kill_cancel', request_id=self.evidence_request.request_id, reason=reason,
                 projection=self.projection())
            active['request_id'] = None
    terminating.KillTask = ObservedKill
    try:
        base.worker(name, generation, artifacts, binary)
    finally:
        emit('kill_worker_end', counts=counts)
        stream.close()


class Run(support.Run):
    def __init__(self, output, dependencies, prepared):
        base.Run.__init__(self, output, dependencies, prepared)
        for name, expected in self.prepared['support_hashes'].items():
            assert digest(PROJECT / name) == expected == digest(self.source / name), name
        shutil.copyfile(SCRIPT, self.output / 'tested-runner.py')
        shutil.copyfile(SUPPORT, self.output / 'tested-close-support.py')
        shutil.copyfile(base.__file__, self.output / 'tested-targeting-support.py')
        shutil.copyfile(FIXTURE, self.output / 'tested-fixture.py')
        self.process_fixture = self.source / 'evidence/issue-26/kill_fixture.py'
        self.receipt.update(runner_sha256=digest(SCRIPT), support_hashes=self.prepared['support_hashes'],
            broad_latency_qualified=False,
            qualification='Finite functional scenarios only; GLib/control latency negatives retained; no third-party qualification.',
            pid_reuse_qualification='No claim of actual kernel PID reuse; deterministic reuse is separately tested.')
        save(self.output / 'inventory.json', {'python': sys.version, 'executable': sys.executable,
            'installed_hashes': self.receipt['installed_hashes'], 'prepared': self.prepared,
            'runner_sha256': digest(SCRIPT), 'fixture_sha256': digest(FIXTURE),
            'support_hashes': self.prepared['support_hashes'], 'dependencies': self.receipt['dependencies']})
        self.persist()

    @contextmanager
    def generation(self, label):
        with super().generation(label):
            try:
                yield
            finally:
                self.audit_signals()
        began = self.case.get('independent_shutdown_started_at')
        if began is not None:
            self.case['independent_shutdown_deadline'] = began + 15
            self.case['independent_shutdown_within_original_deadline'] = self.case['cleanup_observed_at'] <= began + 15
            self.persist()
            assert self.case['independent_shutdown_within_original_deadline'], self.case

    def audit_signals(self):
        trace = self.native_trace()
        starts = {e['request_id']: e for e in trace if e['event'] == 'kill_start'}
        summaries = []
        for request_id, start in starts.items():
            signals = self.signals(request_id)
            states = [e['kill_state'] for e in trace if e['event'] == 'kill_phase' and e['request_id'] == request_id]
            states += [e['projection']['kill_state'] for e in trace
                       if e['event'] in ('kill_cancel', 'kill_failed') and e['request_id'] == request_id]
            states += [e['result']['kill_state'] for e in trace
                       if e['event'] == 'kill_complete' and e['request_id'] == request_id]
            if not signals:
                continue
            assert states, (start, signals)
            state = states[-1]
            group = self.case['metadata']['cgroup'] + '/applications/' + start['application']['application_id']
            unique = set()
            for event in signals:
                target = event['identity']
                assert target is not None, event
                observed_group = target['cgroup'].removeprefix('0::')
                assert observed_group == group or observed_group.startswith(group + '/'), event
                phase = 'term' if event['signal'] == signal.SIGTERM else 'kill'
                assert event['signal'] in (signal.SIGTERM, signal.SIGKILL), event
                cutoff = state['term_cutoff'] if phase == 'term' else state['signal_cutoff']
                assert state['started_at'] <= event['wrapper_entered_at'] <= event['submitted_at'] < cutoff, (state, event)
                if phase == 'kill':
                    assert event['wrapper_entered_at'] >= state['term_cutoff'], (state, event)
                token = (target['pid'], target['start_ticks'], phase)
                assert token not in unique, event
                unique.add(token)
            for phase, sig in (('term', signal.SIGTERM), ('kill', signal.SIGKILL)):
                matching = [e for e in signals if e['signal'] == sig]
                assert state['counts'][phase + '_attempted'] == len(matching), (state, matching)
                assert state['counts'][phase + '_submitted'] == sum(e['submitted'] for e in matching), (state, matching)
            summaries.append({'request_id': request_id, 'application_cgroup': group,
                              'signals': len(signals), 'counts': state['counts'],
                              'max_wrapper_identity_seconds': max(e['wrapper_identity_seconds'] for e in signals)})
        self.case['dispatch_audit'] = summaries
        self.case['dispatch_audit_qualification'] = ('Evidence wrapper reads fdinfo and process identity before syscall; '
            'entry and actual submission timestamps plus added read time are retained. Both must precede the fixed cutoff. '
            'This instrumentation does not qualify atomicity of product verification and dispatch.')

    def native_trace(self):
        return sorted(events(self.folder / 'targeting-trace.jsonl') + events(self.folder / 'kill-trace.jsonl'),
                      key=lambda item: item['at'])

    def launch_tree(self, mode='resistant', native=False):
        args = [sys.executable, '-I', str(self.process_fixture), mode, '--duration', '25']
        if mode in ('kill-late', 'migration'):
            args += ['--trigger', str(self.folder / 'late-trigger')]
        if native:
            args += ['--native', str(self.fixture)]
        result = self.call(self.args('launch', *(['--wait-window'] if native else []), '--', *args))
        self.launched, self.app = result, result['application']
        self.appref = self.gen + ':' + self.app['application_id']
        ready = wait(lambda: [e for e in self.fixture_events() if e['event'] == 'fixture_ready'])
        if mode in ('tree', 'root-first', 'late', 'kill-late', 'migration'):
            ready = wait(lambda: (value if len(value := [e for e in self.fixture_events()
                if e['event'] == 'fixture_ready']) >= (2 if mode == 'migration' else 3) else None))
        self.case['birth_identities'] = ready
        return result

    def kill_args(self, timeout='3', ref=None):
        return self.args('kill', '--app', ref or self.appref, '--timeout', timeout)

    def signals(self, request_id=None):
        return [e for e in self.native_trace() if e['event'] == 'pidfd_signal' and
                (request_id is None or e['request_id'] == request_id)]

    def kill_started(self, pending):
        return wait(lambda: next((e for e in self.native_trace() if e['event'] == 'kill_start'
                    and e['at'] >= pending[1]['started_at']), None))

    def term_started(self, pending):
        started = self.kill_started(pending)
        wait(lambda: any(e['signal'] == signal.SIGTERM and e['submitted']
                        for e in self.signals(started['request_id'])))
        return started

    def success(self, result):
        assert result['exited'] is True and result['application'] == self.app, result
        assert result['remaining_processes'] == [], result
        assert result.get('descendant_exit_codes') is None, result
        submits = [e for e in self.signals() if e['submitted'] and e['request_id'] is not None]
        identities = [(e['request_id'], e['identity']['pid'], e['identity']['start_ticks'], e['signal'])
                      for e in submits if e['identity'] is not None]
        assert len(identities) == len(set(identities)), 'Duplicate per-phase lifetime signal'
        requests = {e['request_id'] for e in self.native_trace() if e['event'] == 'kill_start'}
        assert not [e for e in self.native_trace() if e['event'] == 'native_start' and e['request_id'] in requests]
        self.case.setdefault('completed_applications', []).append(self.app_record())
        self.call(self.wait_args('exit', timeout='1'))
        status = self.invoke(['session', 'status', '--session', self.name])
        self.case.setdefault('status_after_kill', []).append(status)
        assert status['response']['ok'], status

    def normal(self):
        self.launch_tree('normal', native=True)
        assert self.rows(), 'Native fixture must be discovered before kill'
        oldref, oldapp, oldlogs = self.appref, self.app, self.launched['logs']
        self.success(self.call(self.kill_args()))
        assert any(e['event'] == 'term_received' and e['role'] == 'root' for e in self.fixture_events())
        self.case['first_fixture_events'] = self.fixture_events()
        self.case['first_logs'] = oldlogs
        self.launch_tree('resistant')
        before, at = self.live_root(), time.monotonic()
        result = self.call(self.kill_args(ref=oldref))
        assert result['exited'] and result['application'] == oldapp, result
        assert not [e for e in self.signals() if e['at'] >= at], 'Completed old handle must not signal new app'
        assert self.live_root() == before
        self.success(self.call(self.kill_args()))

    def tree(self, mode):
        self.launch_tree('tree' if mode == 'stopped-child' else mode)
        births = self.case['birth_identities']
        if mode == 'stopped-child':
            stopped = next(item for item in births if item['role'] == 'child')
            fd = os.pidfd_open(stopped['pid'])
            try:
                assert identity(stopped['pid'])['start_ticks'] == stopped['start_ticks']
                signal.pidfd_send_signal(fd, signal.SIGSTOP)
                def is_stopped():
                    stat = Path('/proc', str(stopped['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
                    return stat[0] == 'T' and int(stat[19]) == stopped['start_ticks']
                wait(is_stopped)
                self.case['stopped_descendant'] = {'identity': identity(stopped['pid']),
                    'state': 'T', 'observed_at': time.monotonic(), 'fault_scope': 'Owned fixture descendant via independent pidfd'}
            finally:
                os.close(fd)
        if mode == 'kill-late':
            grandchild = next(item for item in births if item['role'] == 'grandchild')
            save(self.folder / 'kill-fault.json', {'mode': 'kill-late', 'held_pid': grandchild['pid'],
                                                  'trigger': str(self.folder / 'late-trigger')})
            self.case['fault_qualification'] = ('Evidence-only 200ms KILL visitation deferral of one real '
                'grandchild lets it fork after the first actual KILL; product verification and fixed cutoffs remain active.')
        fds = [os.pidfd_open(item['pid']) for item in births if item['role'] != 'child' or mode != 'root-first']
        try:
            result = self.call(self.kill_args())
            self.success(result)
            for fd in fds:
                poller = select.poll()
                poller.register(fd, select.POLLIN)
                assert poller.poll(0), 'Independent descendant pidfd must report exit'
            submitted = [e for e in self.signals() if e['submitted']]
            assert any(e['signal'] == signal.SIGTERM for e in submitted)
            assert any(e['signal'] == signal.SIGKILL for e in submitted)
            receipts = self.fixture_events()
            assert any(e['event'] == 'term_received' for e in receipts)
            if mode == 'root-first':
                assert result['exit_status'] == 0, result
                root_exit = next(e['at'] for e in receipts if e['event'] == 'term_exit' and e['role'] == 'root')
                descendant_kills = [e['submitted_at'] for e in submitted if e['signal'] == signal.SIGKILL
                                    and e['identity'] and e['identity']['pid'] != self.launched['process']['pid']]
                assert descendant_kills and root_exit < min(descendant_kills), (root_exit, descendant_kills)
            if mode in ('late', 'kill-late'):
                assert any(e.get('role') == 'late-descendant' for e in receipts), receipts
            if mode == 'kill-late':
                born = next(e for e in receipts if e.get('role') == 'late-descendant' and e['event'] == 'fixture_ready')
                first_kill = min(e['submitted_at'] for e in submitted if e['signal'] == signal.SIGKILL)
                assert born['at'] > first_kill
                late_signals = [e for e in submitted if e['identity'] and e['identity']['pid'] == born['pid']]
                assert late_signals and all(e['signal'] == signal.SIGKILL for e in late_signals), late_signals
            if mode == 'stopped-child':
                assert any(e['signal'] == signal.SIGKILL and e['identity']['pid'] == stopped['pid'] for e in submitted)
                assert not [e for e in receipts if e['event'] == 'term_received' and e['pid'] == stopped['pid']]
        finally:
            for fd in fds:
                os.close(fd)

    def migration(self):
        self.launch_tree('migration')
        child = next(item for item in self.case['birth_identities'] if item['role'] == 'child')
        fd = os.pidfd_open(child['pid'])
        try:
            assert identity(child['pid'])['start_ticks'] == child['start_ticks']
            save(self.folder / 'kill-fault.json', {'mode': 'migration', 'pid': child['pid'],
                'start_ticks': child['start_ticks'], 'trigger': str(self.folder / 'late-trigger')})
            retained = wait(lambda: next((e for e in self.native_trace() if e['event'] == 'migration_retained'), None))
            assert retained['retained_key'] == [child['pid'], child['start_ticks']], retained
            pending = self.pending(self.kill_args('4'))
            response = self.remember_pending(pending)['response']
            assert response and not response['ok'], response
            assert response['error']['code'] in ('ownership_uncertain', 'session_failed', 'session_unavailable',
                                                 'completion_unknown', 'application_state_uncertain'), response
            trace = self.native_trace()
            moved = next(e for e in trace if e['event'] == 'controlled_migration')
            gate = next(e for e in trace if e['event'] == 'migration_empty_before_observer')
            observed = next(e for e in trace if e['event'] == 'migration_observer_returned')
            assert retained['at'] < moved['at'] < gate['at'] <= observed['at']
            assert gate['root_returncode'] == 0 and gate['subtree_populated'] is False
            assert gate['retained_live'] and gate['completed'] is False
            assert observed['completed'] is False and observed['active_owner_retained']
            assert observed['ownership_uncertain'] and observed['retained_live'], observed
            assert not self.signals(), 'Controlled migration precedes any application dispatch'
            assert not [e for e in trace if e['event'] == 'kill_complete']
            self.case['migration_receipts'] = {'retained': retained, 'moved': moved, 'empty_gate': gate, 'observed': observed}
            self.case['independent_shutdown_started_at'] = observed['at']
            self.case['retained_application_after_migration'] = self.app_record()
            assert self.case['retained_application_after_migration']['state'] != 'all-exited'
            self.case['migration_qualification'] = ('A real retained live child moved by the evidence owner into '
                'an independent subgroup of this same service. Root exit and original subtree emptiness precede '
                'the next Registry observer tick; ownership failure drives separate service cleanup, never kill success.')
            self.case['explicit_stop'] = self.invoke(['session', 'stop', '--session', self.name])
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            wait(lambda: poller.poll(0))
            self.case['migrated_descendant_exit_after_independent_cleanup_at'] = time.monotonic()
        finally:
            os.close(fd)

    def control(self, kind):
        self.launch_tree('resistant')
        before = self.live_root()
        pending = self.pending(self.kill_args('5'))
        started = self.term_started(pending)
        if kind in ('cancel', 'disconnect'):
            self.status_measurement()
            at = time.monotonic()
            pending[0].send_signal(signal.SIGINT if kind == 'cancel' else signal.SIGKILL)
            response = self.remember_pending(pending)['response']
            if kind == 'cancel':
                assert response and response['error']['code'] == 'cancelled', response
            else:
                assert response is None, response
            cancelled = wait(lambda: next((e for e in self.native_trace()
                if e['event'] == 'kill_cancel' and e['request_id'] == started['request_id']), None))
            self.case['cancel_dispatch_seconds'] = cancelled['at'] - at
            self.case['cancel_dispatch_under_100ms'] = 0 <= cancelled['at'] - at < .1
            # Past the entire admitted deadline also passes any planned KILL cutoff.
            while time.monotonic() <= started['deadline'] + .1:
                assert identity(before['pid']) == before
                time.sleep(.02)
            assert not [e for e in self.signals(started['request_id']) if e['submitted_at'] > cancelled['at']]
            assert not [e for e in self.signals() if cancelled['at'] < e['at'] <= started['deadline'] + .1]
            self.case['survived_original_deadline_at'] = time.monotonic()
            self.case['retained_application'] = self.app_record()
            self.success(self.call(self.kill_args()))
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
            self.case['independent_service_fault'] = matches[0]
            fd = os.pidfd_open(matches[0]['pid'])
            try:
                assert identity(matches[0]['pid']) == matches[0]
                self.case['independent_shutdown_started_at'] = time.monotonic()
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        response = self.remember_pending(pending)['response']
        assert response and not response['ok'], response
        assert response['error']['code'] in ('cancelled', 'session_failed', 'session_unavailable', 'completion_unknown'), response
        self.case['signals_scope'] = 'App request interrupted; independent session cleanup is not successful application kill.'

    def queued(self, cancel=False):
        self.launch_tree()
        blocker = self.pending(self.wait_args('exit', timeout='1.3'))
        self.target_started(blocker[1]['started_at'], 'exit')
        pending = self.pending(self.kill_args('3' if cancel else '.15'))
        if cancel:
            time.sleep(.08)
            pending[0].send_signal(signal.SIGINT)
        response = self.remember_pending(pending)['response']
        self.remember_pending(blocker)
        assert response and response['error']['code'] == ('cancelled' if cancel else 'timeout'), response
        assert not self.signals(), self.signals()
        self.live_root()
        self.success(self.call(self.kill_args()))

    def timeout_recovery(self):
        self.launch_tree('tree')
        # A genuine tiny deadline tests public admission/remaining retention. It
        # makes no claim that SIGKILL was ignored or that dispatch necessarily began.
        response = self.call(self.kill_args('.001'), error='timeout')
        self.case['tiny_deadline_partial'] = response['error'].get('partial_result')
        self.case['tiny_deadline_qualification'] = 'Real 1ms total request deadline; may expire while queued or before first dispatch.'
        self.case['retained_after_timeout'] = self.app_record()
        self.live_root()
        self.success(self.call(self.kill_args()))

    def dispatch_timeout(self):
        self.launch_tree('tree')
        before = self.live_root()
        save(self.folder / 'kill-fault.json', {'mode': 'defer-kill'})
        response = self.call(self.kill_args('1.2'), error='timeout')
        partial = response['error']['partial_result']
        assert partial['exited'] is False and partial['kill_state']['enumeration_incomplete'], partial
        assert partial['kill_state']['counts']['term_submitted'] > 0, partial
        assert partial['kill_state']['counts']['kill_submitted'] == 0, partial
        state = partial['kill_state']
        samples = partial['remaining_processes']
        assert state['enumeration'] == 'sampled' and isinstance(samples, list) and 0 < len(samples) <= 64, partial
        assert state['remaining_processes'] == samples and state['enumeration_incomplete'] is True
        assert all(isinstance(item['pid'], int) and item['pid'] > 0 and
                   isinstance(item['start_time_ticks'], int) and item['start_time_ticks'] > 0 and
                   state['started_at'] <= item['observed_at'] <= state['deadline'] for item in samples), samples
        assert identity(before['pid']) == before
        self.case['fault_qualification'] = 'Evidence-only KILL visitation deferral with real TERM-resistant processes; no claim SIGKILL was ignored.'
        self.case['retained_after_timeout'] = self.app_record()
        save(self.folder / 'kill-fault.json', {'mode': 'normal'})
        at = time.monotonic()
        time.sleep(.3)
        assert not [e for e in self.signals() if e['at'] >= at], 'Ended request must leave no background signals'
        self.success(self.call(self.kill_args()))

    def prior_timeout(self, mode):
        if mode == 'confirmation':
            self.launch('--close-mode', 'confirmation', '--exit-after-ms', '25000', flags=('--wait-window',))
            response = self.call(self.close_args(self.row(), '.8'), error='timeout')
            self.case['preceding_response'] = response
            self.row('dialog', 2)
            assert not any(e['event'] == 'confirmation_accepted' for e in self.fixture_events())
        elif mode == 'launch-timeout':
            response = self.call(self.args('launch', '--wait-window', '--timeout', '.3', '--',
                str(self.fixture), '--autonomous', '--window-delay-ms', '10000', '--exit-after-ms', '25000'), error='timeout')
            self.launched = response['error']['partial_result']
            self.app = self.launched['application']
            self.appref = self.gen + ':' + self.app['application_id']
        else:
            self.launch_tree()
            self.call(self.wait_args('window', timeout='.3'), error='timeout')
        assert not self.signals(), self.signals()
        self.live_root()
        self.case['retained_before_explicit_kill'] = self.app_record()
        self.success(self.call(self.kill_args()))

    def sentinels(self):
        path = self.output / (self.name + '-foreign.jsonl')
        with path.open('x') as stream:
            child = subprocess.Popen([sys.executable, '-I', str(self.process_fixture), 'resistant', '--duration', '25'],
                                     stdout=stream, stderr=subprocess.STDOUT, cwd='/', env=self.env)
            fd = os.pidfd_open(child.pid)
            try:
                wait(lambda: events(path))
                foreign = identity(child.pid)
                self.launch_tree('tree')
                group = Path('/sys/fs/cgroup' + self.case['metadata']['cgroup'])
                desktop = [identity(int(pid)) for file in (group / 'supervisor').rglob('cgroup.procs')
                           for pid in file.read_text().split()]
                assert desktop, 'Live supervisor/private desktop sentinels required'
                self.case['foreign_sentinel_before'] = foreign
                self.case['desktop_sentinels_before'] = desktop
                self.success(self.call(self.kill_args()))
                assert identity(child.pid) == foreign
                assert all(identity(item['pid']) == item for item in desktop)
                assert not any(e['event'] == 'term_received' for e in events(path))
                self.case['foreign_sentinel_after'] = identity(child.pid)
                self.case['desktop_sentinels_after'] = [identity(item['pid']) for item in desktop]
            finally:
                if child.poll() is None:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                child.wait(timeout=3)
                os.close(fd)
                self.case['foreign_sentinel_events'] = events(path)

    def execute(self, selected):
        failed = []
        for label in selected:
            try:
                with self.generation(label):
                    if label == 'normal': self.normal()
                    elif label in ('tree', 'root-first', 'late', 'kill-late', 'stopped-child'): self.tree(label)
                    elif label == 'migration': self.migration()
                    elif label in ('cancel', 'disconnect', 'stop'): self.control(label)
                    elif label.endswith('-death'): self.control(label.removesuffix('-death'))
                    elif label in ('queued-expiry', 'cancel-before'): self.queued(label == 'cancel-before')
                    elif label == 'timeout-recovery': self.timeout_recovery()
                    elif label == 'dispatch-timeout': self.dispatch_timeout()
                    elif label in ('confirmation', 'window-timeout', 'launch-timeout'): self.prior_timeout(label)
                    elif label == 'sentinels': self.sentinels()
                    elif label == 'restart':
                        self.launch_tree()
                        oldref, oldgen = self.appref, self.gen
                if label == 'restart':
                    with self.generation('restart-new'):
                        assert self.gen != oldgen
                        self.launch_tree()
                        before = self.live_root()
                        self.call(self.kill_args(ref=oldref), error='generation_mismatch')
                        assert not self.signals(), self.signals()
                        assert self.live_root() == before
                        self.success(self.call(self.kill_args()))
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
    for name in ('output', 'dependencies', 'prepared'):
        run.add_argument(name)
    run.add_argument('--case', action='append', choices=CASES)
    for name, count in (('worker', 4), ('controller', 3)):
        commands.add_parser(name).add_argument('values', nargs=count)
    args = parser.parse_args()
    if args.command == 'prepare': prepare(args.output, args.commit)
    elif args.command == 'worker': worker(*args.values)
    elif args.command == 'controller': base.controller(*args.values)
    else: return Run(args.output, args.dependencies, args.prepared).execute(args.case or CASES)


if __name__ == '__main__':
    raise SystemExit(main())
