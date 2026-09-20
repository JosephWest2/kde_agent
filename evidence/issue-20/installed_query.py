"""Requalify the rebuilt pinned query helper in an owned installed desktop.

Run: .local/issue20-venv/bin/python -I evidence/issue-20/installed_query.py \
     /absolute/new/run-directory

Only this test fixture keeps the native application's stdin open and launches
qualification queries. No public desktop action or production injection flag is
introduced. All worker imports come from the non-editable installed wheel.
"""
from __future__ import annotations
import hashlib
import importlib.util
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import agent_desktop
from agent_desktop.contracts import make_request
from agent_desktop.lifecycle import Manager, atomic

PROJECT = Path(__file__).resolve().parents[2]
EXPECTED_BINARY = 'b7a300d5a2f0b95a21d71dca5757328382bb6dd887e4ac975fffb59e2351bd21'
REPETITIONS = 24


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19],
            'cgroup': Path('/proc', str(pid), 'cgroup').read_text().strip()}


def vanished(value):
    try:
        fields = Path('/proc', str(value['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] != value['start_ticks'] or fields[0] == 'Z'
    except FileNotFoundError:
        return True


def native(binary, fifo):
    fd = os.open(fifo, os.O_RDWR | os.O_NOFOLLOW)
    os.dup2(fd, 0)
    os.close(fd)
    os.execv(binary, [binary])


def worker(name, generation, artifacts, fixture_binary, kdotool):
    from agent_desktop.worker import run
    from agent_desktop.readiness import Readiness
    from agent_desktop.provisional_windows import encoded, snapshot
    from gi.repository import GLib
    root = Path(artifacts) / 'generations' / generation
    query_root = root / 'qualification'
    query_root.mkdir(mode=0o700)
    holder = {'provider': None, 'fixture': None, 'query': None, 'pending': False,
              'index': 0, 'next_at': 0, 'receipts': [], 'uuid': None, 'done': False}
    fifo = query_root / 'native-control'
    os.mkfifo(fifo, 0o600)
    events = query_root / 'native.jsonl'

    def factory(*args):
        provider = Readiness(*args)
        holder['provider'] = provider
        return provider

    def observe(desktop):
        if desktop.phase != 'constructed' or holder['done']:
            return
        if holder['fixture'] is None:
            with events.open('xb') as out, (query_root / 'native.stderr').open('xb') as err:
                holder['fixture'] = desktop.launch(
                    [sys.executable, '-I', str(Path(__file__).resolve()), '_native', fixture_binary, str(fifo)],
                    '/', {'HARNESS_GENERATION': generation}, stdout=out, stderr=err)
            holder['owner'] = {'fixture': identity(holder['fixture'].process.pid),
                               'worker': identity(os.getpid()), 'bus': identity(desktop.bus.process.pid),
                               'compositor': identity(desktop.compositor.process.pid),
                               'private': desktop.private}
            atomic(query_root / 'owner.json', holder['owner'])
            return
        assert holder['fixture'].returncode is None, 'Native fixture exited during qualification'
        provider = holder['provider']
        if provider is None or provider.state != 'ready':
            return
        provider.bus.tick()
        assert time.monotonic() < provider.deadline + 30, 'Overall qualification work deadline'
        if not any(json.loads(line).get('event') == 'presented' for line in events.read_text().splitlines()):
            return
        if holder['pending']:
            return
        if holder['query'] is not None:
            current = holder['current']
            now = time.monotonic()
            assert now < current['work_deadline'], 'Query exceeded .5s work budget'
            if holder['query'].returncode is None:
                return
            assert holder['query'].returncode == 0, 'Query helper failed'
            assert holder['query'] not in desktop.children.owned, 'Completed query was not reaped'
            assert current['stdout'].stat().st_size <= 1024 * 1024
            data = snapshot(json.loads(current['stdout'].read_text()), current['request_id'])
            matches = [row for row in data['windows'] if row['pid'] == holder['owner']['fixture']['pid']]
            assert len(matches) == 1 and len(data['windows']) == 1, 'Expected sole populated native window'
            row = matches[0]
            assert row['title'] == 'KDE Agent Native Fixture'
            assert row['class'] == 'org.kde_agent.fixture'
            assert row['active'] is True and data['active_uuid'] == row['uuid']
            assert row['client']['width'] == 640 and row['client']['height'] == 360
            assert row['frame']['width'] >= 640 and row['frame']['height'] >= 360
            assert 0 <= row['frame']['x'] and 0 <= row['frame']['y']
            assert row['frame']['x'] + row['frame']['width'] <= 1280
            assert row['frame']['y'] + row['frame']['height'] <= 720
            if holder['uuid'] is None:
                holder['uuid'] = row['uuid']
            assert row['uuid'] == holder['uuid'], 'Native window UUID changed between queries'
            assert time.monotonic() < current['work_deadline'], 'Late query metadata validation'
            current['validated_at'] = time.monotonic()
            cleanup_deadline = current['validated_at'] + 1.5
            holder['pending'] = True
            def cleaned(value, _fds, error):
                assert not error and value.unpack() == (False,), 'Query script remained loaded'
                assert time.monotonic() < cleanup_deadline, 'Late cleanup receipt'
                receipt = {'index': holder['index'], 'request_id': current['request_id'],
                           'helper': current['identity'], 'started_at': current['started_at'],
                           'validated_at': current['validated_at'], 'cleaned_at': time.monotonic(),
                           'work_seconds': current['validated_at'] - current['started_at'],
                           'cleanup_seconds': time.monotonic() - current['validated_at'],
                           'work_deadline': current['work_deadline'], 'cleanup_deadline': cleanup_deadline,
                           'reaped': True, 'script_loaded': False, 'snapshot': data,
                           'live_health': json.loads(json.dumps(provider.snapshot()))}
                atomic(current['folder'] / 'receipt.json', receipt)
                holder['receipts'].append(receipt)
                holder['index'] += 1
                holder['query'] = None
                holder['pending'] = False
                holder['next_at'] = time.monotonic() + .30
                if holder['index'] == REPETITIONS:
                    assert all(vanished(item['helper']) for item in holder['receipts'])
                    passed_times = {item['live_health']['health']['compositor']['observed_at']
                                    for item in holder['receipts']}
                    assert len(passed_times) >= 5, 'Expected repeated independent periodic compositor checks'
                    assert desktop.children.owned == {desktop.bus, desktop.compositor, holder['fixture']}, 'Unexpected live child'
                    atomic(query_root / 'complete.json', {
                        'queries': holder['receipts'], 'uuid': holder['uuid'], 'count': REPETITIONS,
                        'periodic_health_observations': len(passed_times), 'owner': holder['owner'],
                        'readiness': provider.snapshot(), 'first_readiness_query': json.loads(
                            (root / 'readiness/query/stdout').read_text()), 'all_query_children_reaped': True,
                        'remaining_direct_children': [identity(child.process.pid) for child in desktop.children.owned]})
                    holder['done'] = True
            provider.bus.call('qualification-' + str(holder['index']), 'org.kde.KWin', '/Scripting',
                              'org.kde.kwin.Scripting', 'isScriptLoaded',
                              GLib.Variant('(s)', (current['request_id'],)), '(b)', cleanup_deadline, cleaned)
            return
        if time.monotonic() < holder['next_at']:
            return
        index = holder['index']
        folder = query_root / f'query-{index:02d}'
        folder.mkdir(mode=0o700)
        request_id = 'qualification-' + generation + '-' + str(index)
        script = folder / 'input.js'
        script.write_text(encoded({'request_id': request_id}) + files('agent_desktop').joinpath('readiness_query.js').read_text())
        script.chmod(0o600)
        started = time.monotonic()
        with (folder / 'stdout').open('xb') as out, (folder / 'stderr').open('xb') as err:
            child = desktop.launch([kdotool, '--name', request_id, 'kwinscript', '--file', str(script)],
                                   str(desktop.root), {}, stdout=out, stderr=err)
        holder['query'] = child
        holder['current'] = {'folder': folder, 'stdout': folder / 'stdout', 'request_id': request_id,
                             'started_at': started, 'work_deadline': started + .5, 'identity': identity(child.process.pid)}

    run(name, generation, artifacts=artifacts, managed=True, desktop=True,
        desktop_observer=observe, kdotool=kdotool, startup_deadline=time.monotonic() + 30,
        readiness_factory=factory)


def main(run_directory):
    assert not Path(agent_desktop.__file__).is_relative_to(PROJECT / 'src')
    run_root = Path(run_directory).resolve()
    run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = run_root / 'artifacts'
    binary = PROJECT / '.local/dependencies/bin/kdotool'
    assert digest(binary) == EXPECTED_BINARY
    spec = importlib.util.spec_from_file_location('fixture_builder', PROJECT / 'tools/private_harness.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    fixture, build = harness.build(run_root / 'build')
    receipt = {'script_sha256': digest(__file__), 'fixture_build': build,
               'installed_module': agent_desktop.__file__, 'kdotool_sha256': digest(binary),
               'kdotool_build': json.loads((PROJECT / '.local/dependencies/build.json').read_text()),
               'installed_source_sha256': {str(path.relative_to(Path(agent_desktop.__file__).parent)): digest(path)
                                          for path in Path(agent_desktop.__file__).parent.glob('*.py')}}
    atomic(run_root / 'build-receipt.json', receipt)
    manager_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
                   'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
    with tempfile.TemporaryDirectory(prefix='q20-') as temporary:
        runtime = Path(temporary) / 'r'
        runtime.mkdir(mode=0o700)
        os.environ['XDG_RUNTIME_DIR'] = str(runtime)
        name = 'qualified-query'
        def command(data):
            return [sys.executable, '-I', str(Path(__file__).resolve()), '_worker', name, data['generation'],
                    str(artifacts), str(fixture), str(binary)]
        manager = Manager(worker_command=command)
        generation = None
        try:
            start = manager.start(make_request('session.start', caller_cwd='/', session=name,
                                  arguments={'artifacts': str(artifacts), 'dependency_root': str(PROJECT / '.local/dependencies')}))
            receipt['start'] = start
            assert start['ok'], start
            generation = start['session']['generation']
            root = artifacts / 'generations' / generation
            complete = root / 'qualification/complete.json'
            deadline = time.monotonic() + 40
            while not complete.exists():
                assert time.monotonic() < deadline, 'Qualification timed out'
                if (root / 'startup-failure.json').exists():
                    raise AssertionError((root / 'startup-failure.json').read_text())
                time.sleep(.05)
            receipt['qualification'] = json.loads(complete.read_text())
            status = Manager().handle(make_request('session.status', caller_cwd='/', session=name, arguments={}, expected_generation=generation))
            receipt['status'] = status
            assert status['ok'] and status['result']['desktop_ready'], status
            data_path = runtime / 'agent-desktop/g' / generation / 'lifecycle.json'
            data = json.loads(data_path.read_text())
            receipt['ownership'] = data
            assert all(data['unit'] in value['cgroup'] for key, value in
                       receipt['qualification']['owner'].items() if key != 'private')
            stopped_at = time.monotonic()
            stop = Manager().handle(make_request('session.stop', caller_cwd='/', session=name, arguments={}, expected_generation=generation))
            receipt['stop'] = stop
            receipt['stop_seconds'] = time.monotonic() - stopped_at
            assert stop['ok'] and receipt['stop_seconds'] < 15, stop
            cgroup = Path('/sys/fs/cgroup' + data['cgroup'])
            assert not cgroup.exists() or 'populated 0' in (cgroup / 'cgroup.events').read_text()
            assert all(vanished(value) for key, value in receipt['qualification']['owner'].items() if key != 'private')
            receipt['cgroup_empty'] = True
            receipt['all_owned_lifetimes_exited'] = True
            receipt['qualified'] = True
        except BaseException as error:
            receipt['error'] = str(error)
            raise
        finally:
            for path in (runtime / 'agent-desktop/g').glob('*/lifecycle.json'):
                unit = 'agent-desktop-' + path.parent.name + '.service'
                subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit], env=manager_env, capture_output=True, timeout=8)
                subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', unit], env=manager_env, capture_output=True, timeout=3)
            atomic(run_root / 'summary.json', receipt)
    print(str(run_root / 'summary.json'))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_native':
        native(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == '_worker':
        worker(*sys.argv[2:])
    else:
        main(sys.argv[1])
