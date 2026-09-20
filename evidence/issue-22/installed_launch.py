"""Installed launch qualification; CLI uses clean env/cwd, injections only extend waits.

VENV/bin/python -I evidence/issue-22/installed_launch.py NEW_OUTPUT DEPENDENCIES
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
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


def wait(condition, timeout=5):
    end = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < end, 'Evidence condition timed out'
        time.sleep(.005)


def worker(name, generation, artifacts, kdotool, mode):
    from agent_desktop import applications
    from agent_desktop.contracts import ContractError
    from agent_desktop.worker import run
    original = applications.LaunchTask
    folder = Path(artifacts) / 'generations' / generation
    class WaitingLaunch(original):
        def step(self, now):
            if self.phase != 'done':
                result = super().step(now)
                if result is None:
                    return None
                save(folder / 'post-launch-wait.json', {'request_id': self.request.request_id,
                    'at': time.monotonic(), 'result': result})
            if mode == 'failure':
                raise ContractError('timeout', 'Injected post-launch wait failed.')
            return None
        def request_cancel(self, reason):
            entered = time.monotonic()
            super().request_cancel(reason)
            save(folder / 'cancel-observed.json', {'reason': reason, 'at': entered,
                                                   'request_id': self.request.request_id})
    applications.LaunchTask = WaitingLaunch
    run(name, generation, artifacts=artifacts, managed=True, desktop=True, kdotool=kdotool)


def controller(name, artifacts, dependencies, mode):
    from agent_desktop.lifecycle import Manager, Systemd
    from agent_desktop.prerequisites import check
    from agent_desktop.contracts import make_request
    kdotool = check(dependencies, time.monotonic() + 30)['kdotool']['executable']
    class Injected(Systemd):
        def start(self, data, runtime, command, deadline):
            return super().start(data, runtime, [sys.executable, '-I', str(SCRIPT), 'worker',
                name, data['generation'], artifacts, kdotool, mode], deadline)
    req = make_request('session.start', session=name, caller_cwd='/',
        arguments={'artifacts': artifacts, 'dependency_root': dependencies})
    print(json.dumps(Manager(systemd=Injected()).start(req)), flush=True)


def main(output, dependencies):
    output, dependencies = Path(output).resolve(), Path(dependencies).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = output / 'artifacts'
    spec = importlib.util.spec_from_file_location('fixture_build', PROJECT / 'tools/private_harness.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    binary, build = harness.build(output / 'fixture-build')
    receipt = {'source_commit': subprocess.check_output(['git', '-C', str(PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
        'installed_module': agent_desktop.__file__, 'interpreter': sys.executable, 'fixture': build,
        'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (PROJECT / 'src/agent_desktop').glob('*.py')},
        'installed_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(agent_desktop.__file__).parent.glob('*.py')},
        'cases': {}, 'release_qualified': False, 'replacement_issue': 35}
    assert receipt['source_hashes'] == receipt['installed_hashes']
    with tempfile.TemporaryDirectory(prefix='a22-') as temp:
        runtime = Path(temp)
        os.environ['XDG_RUNTIME_DIR'] = temp
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': temp}
        caller = output / 'caller'
        caller.mkdir(mode=0o700)
        cli = Path(sys.executable).with_name('agent-desktop')
        def invoke(args, *, cwd=caller):
            started = time.monotonic()
            result = subprocess.run([str(cli), '--json', *args], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=65)
            value = json.loads(result.stdout)
            return {'response': value, 'exit_code': result.returncode,
                    'elapsed_seconds': time.monotonic() - started, 'stderr': result.stderr}
        def start(name, mode=None):
            if mode is None:
                result = invoke(['session', 'start', '--session', name, '--artifacts', str(artifacts),
                                 '--dependency-root', str(dependencies)])['response']
            else:
                child = subprocess.run([sys.executable, '-I', str(SCRIPT), 'controller', name,
                    str(artifacts), str(dependencies), mode], env=env, cwd='/', capture_output=True, text=True, timeout=65)
                assert child.returncode == 0, child.stderr
                result = json.loads(child.stdout)
            assert result['ok'], result
            generation = result['session']['generation']
            data = read(runtime / 'agent-desktop/g' / generation / 'lifecycle.json')
            return result, data, artifacts / 'generations' / generation
        def stop(name, data):
            result = invoke(['session', 'stop', '--session', name])
            assert result['response']['ok'], result
            group = Path('/sys/fs/cgroup' + data['cgroup'])
            assert not group.exists() or 'populated 0' in (group / 'cgroup.events').read_text()
            return result
        def launch(name, args, flags=()):
            return invoke(['launch', '--session', name, *flags, '--', *args])
        def app_record(folder, result):
            return folder / 'applications' / result['application']['application_id'] / 'record.json'
        def exited(folder, result):
            return wait(lambda: (value if (value := read(app_record(folder, result)))['state'] in
                ('all-exited', 'launch-failed') else None))
        def events(path):
            try:
                return [json.loads(line) for line in Path(path).read_text().splitlines() if line]
            except (ValueError, FileNotFoundError):
                return []
        name = 'a22-public'
        started, data, folder = start(name)
        public = receipt['cases']['public'] = {'start': started, 'metadata': data, 'launches': []}
        try:
            from agent_desktop.lifecycle import Systemd
            from agent_desktop.service_cleanup import membership
            props = subprocess.check_output(['/usr/bin/systemctl', '--user', 'show', data['unit'],
                '-p', 'Delegate', '-p', 'DelegateControllers', '-p', 'DelegateSubgroup', '-p', 'MainPID'],
                env=harness.manager_env(), text=True)
            public['properties'] = dict(line.split('=', 1) for line in props.splitlines())
            assert public['properties']['Delegate'] == 'yes' and not public['properties']['DelegateControllers']
            assert public['properties']['DelegateSubgroup'] == 'supervisor'
            assert membership(int(public['properties']['MainPID'])) == data['cgroup'] + '/supervisor'
            public['subtree_control'] = (Path('/sys/fs/cgroup' + data['cgroup']) / 'cgroup.subtree_control').read_text()
            native = launch(name, [str(binary), '--autonomous', '--window-delay-ms', '150',
                                   '--exit-after-ms', '650', '--exit-code', '9'])
            assert native['response']['ok'], native
            result = native['response']['result']
            wait(lambda: any(x['event'] == 'committed' for x in events(result['logs']['stdout'])))
            native['terminal'] = exited(folder, result)
            assert native['terminal']['exit_code'] == 9
            native['events'] = events(result['logs']['stdout'])
            public['launches'].append(native)
            probe = caller / 'probe'
            probe.write_text('#!/usr/bin/python3\nimport json,os,sys\nprint(json.dumps({"argv":sys.argv,"cwd":os.getcwd(),"override":os.environ.get("TEST_OVERRIDE"),"runtime":os.environ["XDG_RUNTIME_DIR"],"wayland":os.environ["WAYLAND_DISPLAY"],"bus":os.environ["DBUS_SESSION_BUS_ADDRESS"]}),flush=True)\nprint("durable stderr",file=sys.stderr,flush=True)\n')
            probe.chmod(0o700)
            for command, flags in ((str(probe), []), ('./probe', []), ('probe', ['--env', 'PATH=' + str(caller)]),
                                    ('probe', ['--env', 'PATH=:/usr/bin'])):
                response = launch(name, [command, 'space value', ';$HOME', ''], ['--env', 'TEST_OVERRIDE=explicit', *flags])
                assert response['response']['ok'], response
                result = response['response']['result']
                response['terminal'] = exited(folder, result)
                observed = read(result['logs']['stdout'])
                assert observed['argv'][1:] == ['space value', ';$HOME', '']
                assert observed['cwd'] == str(caller) and observed['override'] == 'explicit'
                assert observed['runtime'].startswith(temp + '/agent-desktop/g/' + data['generation'])
                assert Path(result['logs']['stderr']).read_text() == 'durable stderr\n'
                response['observed'] = observed
                public['launches'].append(response)
            denied = launch(name, ['/bin/true'], ['--env', 'WAYLAND_DISPLAY=host'])
            assert not denied['response']['ok'] and denied['response']['error']['code'] == 'invalid_arguments'
            public['protected_environment'] = denied
            descendant = launch(name, [str(binary), '--autonomous', '--exit-after-ms', '0', '--descendant-ms', '1200'])
            assert descendant['response']['ok'], descendant
            result = descendant['response']['result']
            wait(lambda: read(app_record(folder, result))['state'] == 'root-exited')
            ev = events(result['logs']['stdout'])
            spawned = next(x for x in ev if x['event'] == 'descendant_spawned')
            pid = spawned['descendant_pid']
            descendant['live_descendant'] = {'pid': pid, 'cgroup': membership(pid),
                'stat': Path('/proc', str(pid), 'stat').read_text()}
            assert membership(pid) == read(app_record(folder, result))['cgroup']
            count = len(list((folder / 'applications').iterdir()))
            conflict = launch(name, ['/bin/true'])
            assert not conflict['response']['ok'] and conflict['response']['error']['code'] == 'application_active'
            assert len(list((folder / 'applications').iterdir())) == count
            descendant['conflict'] = conflict
            descendant['terminal'] = exited(folder, result)
            public['descendant'] = descendant
            later = launch(name, ['/bin/true'])
            assert later['response']['ok'], later
            exited(folder, later['response']['result'])
            public['sequential'] = later
            public['wait_window'] = launch(name, ['/bin/true'], ['--wait-window'])
            assert public['wait_window']['response']['error']['code'] == 'unsupported_operation'
        finally:
            public['stop'] = stop(name, data)
        for mode in ('failure', 'cancel', 'disconnect'):
            name = 'a22-' + mode
            started, data, folder = start(name, mode)
            case = receipt['cases'][mode] = {'start': started, 'metadata': data}
            try:
                if mode == 'failure':
                    result = launch(name, ['/bin/sleep', '30'])
                    assert result['response']['error']['code'] == 'timeout', result
                    case['response'] = result
                    rid = read(folder / 'post-launch-wait.json')['request_id']
                else:
                    from agent_desktop.contracts import make_request
                    from agent_desktop.paths import normalize
                    from agent_desktop.protocol import encode, CancelRequest
                    count = 64 if mode == 'cancel' else 0
                    script = 'import subprocess,time; p=[subprocess.Popen(["/bin/sleep","30"]) for _ in range(' + str(count) + ')]; print("ready",flush=True); time.sleep(30)'
                    req = normalize(make_request('launch', session=name, expected_generation=data['generation'],
                        caller_cwd=str(caller), arguments={'argv': [sys.executable, '-I', '-c', script]}))
                    rid = req.request_id
                    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    conn.connect(str(runtime / 'agent-desktop/g' / data['generation'] / 'control.sock'))
                    conn.sendall(encode(req.payload()))
                    marker = wait(lambda: read(folder / 'post-launch-wait.json') if (folder / 'post-launch-wait.json').exists() else None)
                    wait(lambda: 'ready' in Path(marker['result']['logs']['stdout']).read_text())
                    if mode == 'cancel':
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as priority:
                            priority.connect(str(runtime / 'agent-desktop/g' / data['generation'] / 'priority.sock'))
                            sent = time.monotonic()
                            priority.sendall(encode(CancelRequest(uuid.uuid4().hex, name, data['generation'], rid).payload()))
                            wait(lambda: (folder / 'cancel-observed.json').exists())
                        case['cancellation_dispatch_seconds'] = read(folder / 'cancel-observed.json')['at'] - sent
                        assert case['cancellation_dispatch_seconds'] < .1
                        case['member_count'] = count + 1
                    conn.close()
                request_folder = folder / 'requests' / rid
                request_path = wait(lambda: next(request_folder.glob('*/record.json'), None))
                terminal = wait(lambda: value if (value := read(request_path))['phase'] == 'terminal' else None)
                case['terminal_request'] = terminal
                refs = terminal['references']
                assert refs['application']['generation'] == data['generation'] and refs['process']['pid'] > 0
                assert set(refs['logs']) == {'stdout', 'stderr'}
                assert terminal['outcome'] in ('partial', 'unknown')
                app = read(folder / 'applications' / refs['application']['application_id'] / 'record.json')
                assert Path('/proc', str(refs['process']['pid'])).exists()
                assert app['authorized']
                assert len(list((folder / 'applications').iterdir())) == 1
                case['retained_application'] = app
            finally:
                case['stop'] = stop(name, data)
            assert Path(refs['logs']['stdout']).exists() and Path(refs['logs']['stderr']).exists()
        receipt['passed'] = True
        save(output / 'launch.json', receipt)
    print(json.dumps({'passed': True, 'cases': list(receipt['cases']), 'output': str(output)}))


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(*sys.argv[2:])
    elif sys.argv[1] == 'controller':
        controller(*sys.argv[2:])
    else:
        main(*sys.argv[1:])
