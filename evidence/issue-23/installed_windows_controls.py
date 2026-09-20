"""Installed-wheel control/lifetime evidence, using private Python fault seams only.

VENV/bin/python -I evidence/issue-23/installed_windows_controls.py NEW_OUTPUT DEPENDENCIES
"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import time
import uuid

SCRIPT = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location('windows_evidence', SCRIPT.with_name('installed_windows.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
read, save, wait = base.read, base.save, base.wait


def worker(*args):
    from agent_desktop import windows
    from gi.repository import GLib
    folder = Path(args[2]) / 'generations' / args[1]
    original = windows.Query
    class CompletionSuppressed(original):
        def _spawn(self, remove=False):
            if not remove and getattr(self, 'mode', '') == 'missing':
                # Same fixed seam as the installed dependency's own completion
                # regression: suppress its final callback, without blocking KWin.
                (self.folder / 'input.js').write_text('callDBus = function () {};')
            super()._spawn(remove)
        def step(self):
            result = super().step()
            if (getattr(self, 'mode', '') == 'missing' and self.child is not None
                    and self.bus.connection is not None and not getattr(self, 'evidence_registered', False)
                    and 'controls-loaded' not in self.bus.pending):
                def loaded(value, fds, error):
                    if not error and value.unpack() == (True,):
                        self.evidence_registered = True
                        save(folder / ('loaded-' + self.request_id + '.json'),
                             {'request_id': self.request_id, 'name': self.name, 'at': time.monotonic()})
                self.bus.call('controls-loaded', 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                              'isScriptLoaded', GLib.Variant('(s)', (self.name,)), '(b)', self.deadline, loaded)
            return result
    windows.Query = CompletionSuppressed
    base.worker(*args)


def main(output, dependencies):
    from agent_desktop.contracts import make_request
    from agent_desktop.protocol import CancelRequest, Decoder, encode
    import agent_desktop
    output, dependencies = Path(output).resolve(), Path(dependencies).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts = output / 'artifacts'
    spec = importlib.util.spec_from_file_location('fixture_build', base.PROJECT / 'tools/private_harness.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    fixture, build = harness.build(output / 'fixture-build')
    def hashes(root):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.suffix in ('.py', '.js')}
    receipt = {'source_commit': subprocess.check_output(['git', '-C', str(base.PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
               'installed_module': agent_desktop.__file__, 'interpreter': sys.executable,
               'source_hashes': hashes(base.PROJECT / 'src/agent_desktop'),
               'installed_hashes': hashes(Path(agent_desktop.__file__).parent),
               'fixture': build, 'cases': {}, 'release_qualified': False, 'replacement_issue': 35}
    save(output / 'receipt.json', receipt)
    assert receipt['source_hashes'] == receipt['installed_hashes']
    with tempfile.TemporaryDirectory(prefix='a23c-') as runtime:
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
            return result, read(Path(runtime) / 'agent-desktop/g' / gen / 'lifecycle.json'), artifacts / 'generations' / gen
        def stop(name, data):
            result = invoke(['session', 'stop', '--session', name])
            group = Path('/sys/fs/cgroup' + data['cgroup'])
            wait(lambda: not group.exists() or 'populated 0' in (group / 'cgroup.events').read_text(), 16)
            assert result['response']['ok'], result
            return result
        def send(payload, generation, priority=False):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3)
            client.connect(str(Path(runtime) / 'agent-desktop/g' / generation / ('priority.sock' if priority else 'control.sock')))
            client.sendall(encode(payload))
            return client
        def receive(client):
            decoder = Decoder()
            while True:
                data = client.recv(65536)
                assert data, 'response connection closed'
                value = decoder.feed(data)
                if value is not None:
                    return value
        name = 'a23c-controls'
        started, data, folder = start(name)
        case = receipt['cases']['controls'] = {'start': started, 'metadata': data}
        generation = started['session']['generation']
        try:
            launch = invoke(['launch', '--session', name, '--', str(fixture), '--autonomous', '--exit-after-ms', '60000'])
            assert launch['response']['ok'], launch
            case['launch'] = launch
            app = launch['response']['result']['application']
            appref = app['generation'] + ':' + app['application_id']
            pid = launch['response']['result']['process']['pid']
            before = base.identity(pid)
            case['identity_before'] = before
            case['initial'] = invoke(['windows', '--session', name, '--app', appref])
            assert case['initial']['response']['ok'] and len(case['initial']['response']['result']['windows']) == 1
            for mode in ('priority_cancel', 'disconnect'):
                control = case[mode] = {}
                save(folder / 'fault.json', {'mode': 'missing'})
                request = make_request('windows', session=name, expected_generation=generation, caller_cwd='/', arguments={'app': app})
                control['request'] = request.payload()
                client = send(request.payload(), generation)
                try:
                    loaded_path = folder / ('loaded-' + request.request_id + '.json')
                    wait(loaded_path.exists, .45)
                    control['native_registration'] = read(loaded_path)
                    status = make_request('session.status', session=name, expected_generation=generation, caller_cwd='/', arguments={})
                    at = time.monotonic()
                    with send(status.payload(), generation) as status_client:
                        control['status_while_stalled'] = receive(status_client)
                    control['status_elapsed'] = time.monotonic() - at
                    assert control['status_while_stalled']['ok'] and control['status_elapsed'] < .1, control
                    control['action_at'] = time.monotonic()
                    if mode == 'priority_cancel':
                        cancel = CancelRequest(uuid.uuid4().hex, name, generation, request.request_id)
                        control['cancel_request'] = cancel.payload()
                        with send(cancel.payload(), generation, True) as cancel_client:
                            control['cancel_response'] = receive(cancel_client)
                        assert control['cancel_response']['ok'] and control['cancel_response']['result']['cancel_requested'], control
                        control['query_response'] = receive(client)
                        assert control['query_response']['error']['code'] == 'cancelled', control
                    else:
                        client.close()
                    receipt_path = folder / ('query-' + request.request_id + '.json')
                    wait(receipt_path.exists, 3)
                    control['query_receipt'] = read(receipt_path)
                    query = control['query_receipt']
                    control['cancel_dispatch_seconds'] = query['cancel_at'] - control['action_at']
                    assert 0 <= control['cancel_dispatch_seconds'] < .1, control
                    assert query['clean'] and query['script_absent'] and query['child_reaped'] and query['temporary_removed'], control
                    control['identity_after'] = base.identity(pid)
                    assert control['identity_after'] == before, control
                    control['retained_application'] = read(folder / 'applications' / app['application_id'] / 'record.json')
                    assert control['retained_application']['state'] == 'running', control
                    save(folder / 'fault.json', {'mode': 'normal'})
                    control['recovery'] = invoke(['windows', '--session', name, '--app', appref])
                    assert control['recovery']['response']['ok'] and len(control['recovery']['response']['result']['windows']) == 1, control
                finally:
                    client.close()
                    save(output / 'receipt.json', receipt)
            def retire(launched):
                process = launched['response']['result']['process']
                handle = launched['response']['result']['application']
                fd = os.pidfd_open(process['pid'])
                try:
                    assert int(base.identity(process['pid'])['start_ticks']) == process['start_time_ticks']
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
                record_path = folder / 'applications' / handle['application_id'] / 'record.json'
                wait(lambda: read(record_path)['state'] == 'all-exited', 3)
                return read(record_path)
            case['fixture_retired_after_controls'] = retire(launch)
            metadata = case['metadata_modes'] = {}
            for mode in ('empty', 'omitted'):
                entry = metadata[mode] = {}
                entry['launch'] = invoke(['launch', '--session', name, '--', str(fixture), '--autonomous', '--exit-after-ms', '60000',
                                         '--title-mode', mode, '--app-id-mode', mode])
                assert entry['launch']['response']['ok'], entry
                handle = entry['launch']['response']['result']['application']
                entry['query'] = invoke(['windows', '--session', name, '--app', generation + ':' + handle['application_id']])
                assert entry['query']['response']['ok'] and len(entry['query']['response']['result']['windows']) == 1, entry
                entry['fixture_retired_after_query'] = retire(entry['launch'])
            closing = case['native_disappearance'] = {}
            closing['launch'] = invoke(['launch', '--session', name, '--', str(fixture), '--autonomous', '--exit-after-ms', '1600'])
            assert closing['launch']['response']['ok'], closing
            close_app = closing['launch']['response']['result']['application']
            close_ref = generation + ':' + close_app['application_id']
            closing['before'] = invoke(['windows', '--session', name, '--app', close_ref])
            assert closing['before']['response']['ok'] and len(closing['before']['response']['result']['windows']) == 1, closing
            closing['polls'] = []
            def absent():
                q = invoke(['windows', '--session', name, '--app', close_ref])
                closing['polls'].append(q)
                if not q['response']['ok']:
                    error = q['response']['error']
                    assert error['code'] == 'window_query_failed' and error['message'] == 'Application ownership changed during observation.', q
                    record = read(folder / 'applications' / close_app['application_id'] / 'record.json')
                    assert record['state'] == 'all-exited', record
                    cleanup = read(folder / ('query-' + q['response']['request_id'] + '.json'))
                    assert cleanup['clean'] and cleanup['child_reaped'] and cleanup['temporary_removed'], cleanup
                    assert not cleanup['spawned'] or cleanup['script_absent'], cleanup
                    q['retirement_race_cleanup'] = cleanup
                    return False
                return not q['response']['result']['windows']
            wait(absent, 5)
            closing['retained_application'] = read(folder / 'applications' / close_app['application_id'] / 'record.json')
            closing['old_window'] = closing['before']['response']['result']['windows'][0]['window']
        finally:
            case['stop'] = stop(name, data)
            save(output / 'receipt.json', receipt)
        started, data, folder = start(name)
        restart = receipt['cases']['restart'] = {'start': started, 'metadata': data}
        try:
            assert started['session']['generation'] != generation
            restart['stale_app'] = invoke(['windows', '--session', name, '--app', appref])
            restart['stale_generation'] = invoke(['windows', '--session', name, '--generation', generation])
            for key in ('stale_app', 'stale_generation'):
                assert restart[key]['response']['error']['code'] == 'generation_mismatch', restart
            restart['fresh'] = invoke(['windows', '--session', name])
            assert restart['fresh']['response']['ok'], restart
        finally:
            restart['stop'] = stop(name, data)
            save(output / 'receipt.json', receipt)
    print(json.dumps({'receipt': str(output / 'receipt.json'), 'cases': list(receipt['cases'])}))


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(*sys.argv[2:])
    elif sys.argv[1] == 'controller':
        base.SCRIPT = SCRIPT
        base.controller(*sys.argv[2:])
    else:
        main(*sys.argv[1:])
