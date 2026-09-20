"""Installed-wheel discovery evidence; fault seams exist only in this runner.

VENV/bin/python -I evidence/issue-23/installed_windows.py NEW_OUTPUT DEPENDENCIES
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

import agent_desktop

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
assert not Path(agent_desktop.__file__).resolve().is_relative_to(PROJECT / 'src')


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    from agent_desktop.lifecycle import atomic
    atomic(Path(path), value)


def wait(condition, seconds=5):
    end = time.monotonic() + seconds
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < end, 'evidence wait expired'
        time.sleep(.005)


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19], 'cgroup': Path('/proc', str(pid), 'cgroup').read_text().strip()}


def worker(name, generation, artifacts, binary, fixture):
    from gi.repository import GLib
    from agent_desktop import windows
    from agent_desktop.worker import run
    folder = Path(artifacts) / 'generations' / generation
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = {'last': time.monotonic(), 'max_gap': 0}
    def heartbeat():
        now = time.monotonic()
        samples['max_gap'] = max(samples['max_gap'], now - samples['last'])
        samples['last'] = now
        return True
    GLib.timeout_add(5, heartbeat)
    original = windows.Query
    class EvidenceQuery(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            control = folder / 'fault.json'
            self.mode = read(control)['mode'] if control.exists() and not self.request_id.startswith('readiness') else 'normal'
            self.stop_sent = False
            self.saved = False
            self.cancel_at = None
        def _spawn(self, remove=False):
            if not remove and self.mode != 'normal':
                # Fixed evidence-only scripts; no public request carries source.
                scripts = {'malformed': 'output_result("not-json");',
                    'missing': 'callDBus = function () {};',
                    'stopped': 'callDBus = function () {};',
                    'slow': 'var until = Date.now() + 800; while (Date.now() < until) {} output_result("late");',
                    'remover_stall': 'var until = Date.now() + 800; while (Date.now() < until) {} output_result("late");'}
                if self.mode in scripts:
                    (self.folder / 'input.js').write_text(scripts[self.mode])
            if remove and self.mode == 'remover_stall':
                self.remover = self.desktop.children.start(['/usr/bin/sleep', '10'], env=self.desktop.env,
                    cwd=str(self.desktop.root))
                return
            super()._spawn(remove)
            if not remove and self.mode in ('stopped', 'missing', 'remover_stall'):
                save(folder / ('running-' + self.request_id + '.json'), {'request_id': self.request_id,
                    'name': self.name, 'pid': self.child.process.pid, 'at': time.monotonic(), 'mode': self.mode})
        def step(self):
            result = super().step()
            if self.mode == 'stopped' and self.child is not None and not self.stop_sent:
                # Separate async evidence observation of exact registered name.
                if self.bus.connection is not None and 'evidence-loaded' not in self.bus.pending:
                    def loaded(value, fds, error):
                        if not error and value.unpack() == (True,) and self.child.returncode is None and not self.stop_sent:
                            os.kill(self.child.process.pid, signal.SIGSTOP)
                            self.stop_sent = True
                            save(folder / ('stopped-' + self.request_id + '.json'), {'at': time.monotonic(), 'name': self.name})
                    self.bus.call('evidence-loaded', 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                        'isScriptLoaded', GLib.Variant('(s)', (self.name,)), '(b)', self.deadline, loaded)
            if result is not None:
                self.receipt(True)
            return result
        def cancel(self, cause='cancelled'):
            if self.cancel_at is None:
                self.cancel_at = time.monotonic()
            super().cancel(cause)
        def cleanup(self, deadline=None):
            done = super().cleanup(deadline)
            if done or time.monotonic() >= self.cleanup_deadline:
                self.receipt(done)
            return done
        def receipt(self, clean):
            if self.saved:
                return
            self.saved = True
            save(folder / ('query-' + self.request_id + '.json'), {'mode': self.mode, 'request_id': self.request_id,
                'name': self.name, 'started_deadline': self.deadline, 'accepted_at': self.accepted_at,
                'cancel_at': self.cancel_at, 'cleanup_deadline': self.cleanup_deadline, 'clean': clean,
                'error': None if self.error is None else self.error.code, 'script_absent': self.absent,
                'child_reaped': self.child is None or self.child.returncode is not None,
                'returncode': None if self.child is None else self.child.returncode,
                'remover_returncode': None if self.remover is None else self.remover.returncode,
                'temporary_removed': self.folder is None or not self.folder.exists(),
                'stopped_after_registration': self.stop_sent, 'max_glib_gap': samples['max_gap'],
                'stdout': bytes(self.buffers['stdout']).decode('utf8', errors='replace'),
                'stderr': bytes(self.buffers['stderr']).decode('utf8', errors='replace'), 'at': time.monotonic()})
    windows.Query = EvidenceQuery
    unassociated = []
    def infrastructure(desktop):
        if desktop.phase == 'constructed' and not unassociated:
            path = folder / 'unassociated-events.jsonl'
            with path.open('xb') as output:
                child = desktop.launch([fixture, '--autonomous', '--exit-after-ms', '60000'], str(desktop.root), {}, stdout=output, stderr=output)
            unassociated.append(child)
            save(folder / 'unassociated.json', identity(child.process.pid))
    run(name, generation, artifacts=artifacts, managed=True, desktop=True, kdotool=binary, desktop_observer=infrastructure)


def controller(name, artifacts, dependencies, fixture):
    from agent_desktop.lifecycle import Manager, Systemd
    from agent_desktop.prerequisites import check
    from agent_desktop.contracts import make_request
    binary = check(dependencies, time.monotonic() + 30)['kdotool']['executable']
    class Injected(Systemd):
        def start(self, data, runtime, command, deadline):
            return super().start(data, runtime, [sys.executable, '-I', str(SCRIPT), 'worker', name,
                data['generation'], artifacts, binary, fixture], deadline)
    req = make_request('session.start', session=name, caller_cwd='/',
                       arguments={'artifacts': artifacts, 'dependency_root': dependencies})
    print(json.dumps(Manager(systemd=Injected()).start(req)), flush=True)


def percentiles(values):
    values = sorted(values)
    return {'count': len(values), 'p50': statistics.median(values),
            'p95': values[int(.95 * (len(values)-1))], 'p99': values[int(.99 * (len(values)-1))], 'max': max(values)}


def main(output, dependencies):
    output, dependencies = Path(output).resolve(), Path(dependencies).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = output / 'artifacts'
    spec = importlib.util.spec_from_file_location('fixture_build', PROJECT / 'tools/private_harness.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    fixture, build = harness.build(output / 'fixture-build')
    def hashes(root):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.suffix in ('.py', '.js')}
    receipt = {'source_commit': subprocess.check_output(['git', '-C', str(PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
        'installed_module': agent_desktop.__file__, 'interpreter': sys.executable, 'fixture': build,
        'source_hashes': hashes(PROJECT / 'src/agent_desktop'), 'installed_hashes': hashes(Path(agent_desktop.__file__).parent),
        'cases': {}, 'release_qualified': False, 'replacement_issue': 35}
    assert receipt['source_hashes'] == receipt['installed_hashes']
    save(output / 'receipt.json', receipt)
    with tempfile.TemporaryDirectory(prefix='a23-') as runtime:
        os.environ['XDG_RUNTIME_DIR'] = runtime
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': runtime}
        cli = Path(sys.executable).with_name('agent-desktop')
        def invoke(args):
            started = time.monotonic()
            result = subprocess.run([str(cli), '--json', *args], cwd='/', env=env,
                                    capture_output=True, text=True, timeout=65)
            return {'response': json.loads(result.stdout), 'exit_code': result.returncode,
                    'elapsed': time.monotonic() - started, 'stderr': result.stderr}
        def start(name):
            child = subprocess.run([sys.executable, '-I', str(SCRIPT), 'controller', name, str(artifacts),
                str(dependencies), str(fixture)], env=env, cwd='/', capture_output=True, text=True, timeout=65)
            assert child.returncode == 0, child.stderr
            result = json.loads(child.stdout)
            assert result['ok'], result
            gen = result['session']['generation']
            data = read(Path(runtime) / 'agent-desktop/g' / gen / 'lifecycle.json')
            folder = artifacts / 'generations' / gen
            return result, data, folder
        def stop(name, data):
            result = invoke(['session', 'stop', '--session', name])
            group = Path('/sys/fs/cgroup' + data['cgroup'])
            wait(lambda: not group.exists() or 'populated 0' in (group / 'cgroup.events').read_text(), 16)
            assert result['response']['ok'], result
            return result
        name = 'a23-native'
        started, data, folder = start(name)
        case = receipt['cases']['native'] = {'start': started, 'metadata': data, 'unassociated': read(folder / 'unassociated.json')}
        try:
            launch = invoke(['launch', '--session', name, '--', str(fixture), '--autonomous', '--exit-after-ms', '50000', '--sibling', '--dialog', '--child-window-ms', '45000'])
            assert launch['response']['ok'], launch
            app = launch['response']['result']['application']
            case['launch'] = launch
            appref = app['generation'] + ':' + app['application_id']
            queries = []
            for _ in range(10):
                q = invoke(['windows', '--session', name])
                queries.append(q)
                if q['response']['ok'] and len([r for r in q['response']['result']['windows'] if r['app'] == app]) == 4:
                    break
                time.sleep(.03)
            assert q['response']['ok'], q
            rows = q['response']['result']['windows']
            owned = [r for r in rows if r['app'] == app]
            assert len(owned) == 4 and len(rows) == 5, rows
            assert sorted((r['client']['width'], r['client']['height']) for r in owned) == [(320,180),(400,240),(480,300),(640,360)]
            assert len({r['window']['window_id'] for r in owned}) == 4
            assert len({r['title'] for r in owned}) == 1
            assert any(r['pid'] != launch['response']['result']['process']['pid'] for r in owned)
            assert [r for r in rows if r['pid'] == case['unassociated']['pid']][0]['app'] is None
            case['initial_queries'] = queries
            filtered = invoke(['windows', '--session', name, '--app', appref])
            assert filtered['response']['ok'] and len(filtered['response']['result']['windows']) == 4, filtered
            case['filtered'] = filtered
            batches = {}
            for label, count, cadence in (('sequential',100,0), ('paced',30,.1)):
                timings = []
                ids = []
                start_at = time.monotonic()
                pending = []
                started_times = []
                for index in range(count):
                    if cadence:
                        time.sleep(max(0, start_at + index * cadence - time.monotonic()))
                        started_at = time.monotonic()
                        child = subprocess.Popen([str(cli), '--json', 'windows', '--session', name], cwd='/', env=env,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        pending.append((child, started_at))
                        started_times.append(started_at)
                    else:
                        q = invoke(['windows', '--session', name])
                        assert q['response']['ok'], q
                        timings.append(q['elapsed'])
                        ids.append(q['response']['request_id'])
                for child, started_at in pending:
                    stdout, stderr = child.communicate(timeout=10)
                    q = json.loads(stdout)
                    assert q['ok'], (q, stderr)
                    ids.append(q['request_id'])
                # Receipts use worker acceptance timestamps, not delayed communicate time.
                worker_timings = []
                for ident in ids:
                    r = read(folder / ('query-' + ident + '.json'))
                    worker_timings.append(r['accepted_at'] - (r['started_deadline'] - .5))
                batches[label] = {'worker_seconds': percentiles(worker_timings), 'request_ids': ids}
                if timings:
                    batches[label]['cli_seconds'] = percentiles(timings)
                if started_times:
                    batches[label]['actual_start_intervals'] = [b-a for a,b in zip(started_times, started_times[1:])]
            case['batches'] = batches
            case['normal_receipts'] = [read(p) for p in folder.glob('query-*.json')]
        finally:
            case['stop'] = stop(name, data)
            save(output / 'receipt.json', receipt)
        # Bounded generations keep native faults separate from ordinary timings.
        for mode in ('malformed', 'missing', 'stopped', 'slow', 'remover_stall'):
            name = 'a23-' + mode.replace('_','-')
            started, data, folder = start(name)
            fault = receipt['cases'][mode] = {'start': started, 'metadata': data}
            try:
                save(folder / 'fault.json', {'mode': mode})
                q = invoke(['windows', '--session', name])
                fault['query'] = q
                assert not q['response']['ok'], q
                request_id = q['response']['request_id']
                wait(lambda: (folder / ('query-' + request_id + '.json')).exists(), 4)
                fault['receipt'] = read(folder / ('query-' + request_id + '.json'))
                if mode != 'remover_stall':
                    assert fault['receipt']['clean'] and fault['receipt']['script_absent'] and fault['receipt']['temporary_removed'], fault
                    assert fault['receipt']['max_glib_gap'] < .1, fault
                    save(folder / 'fault.json', {'mode': 'normal'})
                    fault['recovery'] = invoke(['windows', '--session', name])
                    assert fault['recovery']['response']['ok'], fault
                if mode == 'stopped':
                    assert fault['receipt']['stopped_after_registration'], fault
                if mode == 'remover_stall':
                    assert not fault['receipt']['clean'] and not fault['receipt']['script_absent'], fault
            finally:
                fault['stop'] = stop(name, data)
                save(output / 'receipt.json', receipt)
    print(json.dumps({'receipt': str(output / 'receipt.json'), 'cases': list(receipt['cases'])}))


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(*sys.argv[2:])
    elif sys.argv[1] == 'controller':
        controller(*sys.argv[2:])
    else:
        main(*sys.argv[1:])
