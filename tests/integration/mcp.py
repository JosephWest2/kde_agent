#!/usr/bin/env python3
"""MCP integration tests for an installed agent-desktop: a minimal MCP client over stdio.

Each scenario runs `agent-desktop mcp` as an MCP client would, starts its own
session through it, and requires that `session stop` leaves no process in the
generation's cgroup and no systemd unit:

    flow        (legacy initialize handshake) session_start, launch, windows,
                focus, type, key, screenshot (window, also copied to `output`,
                and full screen, each a valid PNG image block matching the stored
                capture), session_stop
    cancel      (modern per-request _meta) notifications/cancelled during
                `key --hold 2`: the fixture sees the release within 1s, no
                response is sent for the cancelled call, and input works after
    disconnect  the client closes stdin during `key --hold 2`: release within
                1s, the server exits, and the session keeps running
    server-kill SIGKILL the server during `key --hold 2`: the call process's
                parent-death signal releases within 1s

    python tests/integration/mcp.py --cli .local/dependencies/venv/bin/agent-desktop [SCENARIO ...] [--loop N]

Not part of the unit-test suite; it needs a KDE Plasma 6 host. See docs/TESTING.md.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import failures  # noqa: E402
import smoke  # noqa: E402
from smoke import ROOT, SmokeFailure, log_events  # noqa: E402
from failures import ok, smoke_wait  # noqa: E402

LEGACY = '2025-11-25'
MODERN_META = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
               'io.modelcontextprotocol/clientCapabilities': {},
               'io.modelcontextprotocol/clientInfo': {'name': 'agent-desktop-integration', 'version': '1'}}


class Client:
    """A minimal MCP client: newline-delimited JSON-RPC over the server's stdin and stdout."""

    def __init__(self, cli, era, args, stderr):
        self.era = era
        self.process = subprocess.Popen([cli, 'mcp', *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=stderr)
        self.replies = {}
        self.condition = threading.Condition()
        self.next_id = 0
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            with self.condition:
                if 'id' in message:
                    self.replies[message['id']] = message
                self.condition.notify_all()

    def write(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def send(self, method, params=None, rpc_id=None):
        if rpc_id is None:
            self.next_id += 1
            rpc_id = self.next_id
        params = dict(params or {})
        if self.era == 'modern':
            params['_meta'] = MODERN_META
        self.write({'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params})
        return rpc_id

    def reply(self, rpc_id, timeout):
        with self.condition:
            if not self.condition.wait_for(lambda: rpc_id in self.replies, timeout):
                raise SmokeFailure('mcp', f'no reply to request {rpc_id!r} within {timeout}s')
            return self.replies.pop(rpc_id)

    def request(self, method, params=None, timeout=10):
        message = self.reply(self.send(method, params), timeout)
        if 'error' in message:
            raise SmokeFailure('mcp', f'{method}: JSON-RPC error {message["error"]}')
        return message['result']

    def notify(self, method, params):
        self.write({'jsonrpc': '2.0', 'method': method, 'params': params})

    def open(self):
        if self.era == 'legacy':
            result = self.request('initialize', {'protocolVersion': LEGACY, 'capabilities': {},
                                                 'clientInfo': {'name': 'agent-desktop-integration', 'version': '1'}})
            if result['protocolVersion'] != LEGACY:
                raise SmokeFailure('mcp: initialize', f'negotiated {result["protocolVersion"]}')
            self.notify('notifications/initialized', {})
        else:
            result = self.request('server/discover')
            if '2026-07-28' not in result['supportedVersions']:
                raise SmokeFailure('mcp: discover', f'versions {result["supportedVersions"]}')
        tools = {tool['name'] for tool in self.request('tools/list')['tools']}
        missing = {'session_start', 'launch', 'focus', 'type', 'key', 'screenshot', 'session_stop'} - tools
        if missing:
            raise SmokeFailure('mcp: tools/list', f'missing tools {sorted(missing)}')
        ok(f'mcp: {self.era} open', f'{len(tools)} tools')

    def close(self, timeout=10):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=5)


class MCPScenario(failures.Scenario):
    def __init__(self, cli, dependency_root, artifacts, fixture, verbose):
        super().__init__(cli, dependency_root, artifacts, fixture, verbose)
        self.stderr = open(artifacts / 'mcp-server.stderr', 'w+')
        self.client = None

    def server(self, era):
        client = Client(self.cli, era, ['--session', self.session, '--dependency-root', str(self.dependency_root),
                                        '--artifacts', str(self.artifacts)], self.stderr)
        client.open()
        return client

    def tool(self, step, name, arguments=None, *, timeout=40, expect_ok=True, client=None):
        started = time.monotonic()
        result = (client or self.client).request('tools/call', {'name': name, 'arguments': arguments or {}},
                                                 timeout=timeout)
        payload = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        if json.loads(result['content'][0]['text']) != payload or result['isError'] is payload['ok']:
            raise SmokeFailure(step, f'text, structuredContent and isError disagree: {json.dumps(result)[:400]}')
        if expect_ok and not payload['ok']:
            error = payload['error']
            raise SmokeFailure(step, f"{error['code']}: {error['message']} {json.dumps(error['context'])[:300]}", payload)
        ok(step, f'{time.monotonic() - started:5.2f}s')
        if self.verbose:
            print('      ' + json.dumps(payload.get('result') or payload.get('error'))[:400])
        return payload, result

    def start_session(self):
        self.start_attempted = True
        payload, _ = self.tool('mcp session_start', 'session_start', timeout=60)
        self.generation = payload['session']['generation']
        self.pids.append(payload['result']['worker_pid'])

    def launch_fixture(self, label='fixture'):
        payload, _ = self.tool(f'{label}: launch', 'launch', {'argv': [str(self.fixture), '--autonomous',
                                                                       '--exit-after-ms', '60000'],
                                                              'wait_window': True})
        result = payload['result']
        self.pids.append(result['process']['pid'])
        app = result['application']['ref']
        windows = smoke_wait(lambda: self.tool(f'{label}: windows', 'windows', {'app': app})[0]['result']['windows'],
                             lambda rows: len(rows) == 1, timeout=5)
        if len(windows) != 1:
            raise SmokeFailure(f'{label}: windows', f'expected one window, saw {len(windows)}')
        window = windows[0]['window']['ref']
        self.tool(f'{label}: focus', 'focus', {'window': window})
        return app, window, Path(result['logs']['stdout'])

    def check_image(self, step, result, payload):
        images = [block for block in result['content'] if block['type'] == 'image']
        capture = payload['result']
        if len(images) != 1 or images[0]['mimeType'] != 'image/png' or not capture['image']['included']:
            raise SmokeFailure(step, f'expected one PNG image block: {json.dumps(capture)[:300]}')
        data = base64.b64decode(images[0]['data'], validate=True)
        if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
            raise SmokeFailure(step, 'image block is not a PNG')
        dimensions = list(struct.unpack('>II', data[16:24]))
        if (dimensions != capture['dimensions'] or hashlib.sha256(data).hexdigest() != capture['png_sha256']
                or Path(capture['path']).read_bytes() != data):
            raise SmokeFailure(step, f'image block {dimensions} does not match the capture {capture["dimensions"]}')
        self.screenshots.append(Path(capture['path']))
        ok(step, f'PNG {dimensions[0]}x{dimensions[1]}, {len(data)} bytes, matches {Path(capture["path"]).name}')

    # Scenarios -------------------------------------------------------------

    def flow(self):
        self.client = self.server('legacy')
        self.start_session()
        app, window, log = self.launch_fixture()
        self.tool("fixture: type 'aB!'", 'type', {'window': window, 'text': 'aB!'})
        self.tool('fixture: key ctrl+shift+t', 'key', {'window': window, 'chord': 'ctrl+shift+t'})
        smoke.wait_for_keys(log, 16)
        observed = [(row['key'], row['state']) for row in log_events(log, {'key'})]
        expected = [(30, 1), (30, 0), (42, 1), (48, 1), (48, 0), (42, 0), (42, 1), (2, 1), (2, 0), (42, 0),
                    (29, 1), (42, 1), (20, 1), (20, 0), (42, 0), (29, 0)]
        if observed != expected:
            raise SmokeFailure('fixture: key acknowledgements', f'expected {expected}, saw {observed}')
        ok('fixture: key acknowledgements', 'typed text and chord arrived in order')
        copy = self.artifacts / 'window-copy.png'
        payload, result = self.tool('screenshot (window)', 'screenshot', {'window': window, 'output': str(copy)})
        self.check_image('screenshot (window): image', result, payload)
        if payload['result'].get('output') != str(copy) or copy.read_bytes() != Path(payload['result']['path']).read_bytes():
            raise SmokeFailure('screenshot (window): output', f'output copy differs: {json.dumps(payload["result"])[:300]}')
        ok('screenshot (window): output', f'copied to {copy.name}')
        payload, result = self.tool('screenshot (full)', 'screenshot')
        self.check_image('screenshot (full): image', result, payload)
        if payload['result']['dimensions'] != [1280, 720]:
            raise SmokeFailure('screenshot (full)', f'dimensions {payload["result"]["dimensions"]}')
        self.tool('fixture: close', 'close', {'app': app})
        payload, _ = self.tool('mcp session_stop', 'session_stop', timeout=60)
        self.stopped = True
        if payload['result'].get('cleanup') != 'complete':
            raise SmokeFailure('mcp session_stop', json.dumps(payload)[:400], payload)

    def hold(self, label):
        """Start `key --hold 2 w` through MCP and wait until the fixture has the key down."""
        _, window, log = self.launch_fixture()
        rpc_id = self.client.send('tools/call', {'name': 'key', 'arguments': {'window': window, 'chord': 'w',
                                                                              'hold': 2}}, rpc_id=label)
        rows = smoke_wait(lambda: self.keys(log), lambda rows: any(state == 1 for _, state, _ in rows), timeout=5)
        if not any(state == 1 for _, state, _ in rows):
            raise SmokeFailure(label, 'the fixture never saw the key go down')
        time.sleep(.2)
        return rpc_id, window, log

    def cancel(self):
        self.client = self.server('modern')
        self.start_session()
        rpc_id, window, log = self.hold('cancel')
        interrupted = time.monotonic_ns()
        self.client.notify('notifications/cancelled', {'requestId': rpc_id, 'reason': 'integration test'})
        self.released_after('cancel: release', log, interrupted)
        self.client.request('tools/list')  # Later requests are answered,
        time.sleep(.5)
        if rpc_id in self.client.replies:  # but the cancelled one never is.
            raise SmokeFailure('cancel', f'the cancelled call was answered: {json.dumps(self.client.replies[rpc_id])[:300]}')
        ok('cancel', 'no response for the cancelled call')
        self.tool('cancel: key afterwards', 'key', {'window': window, 'chord': 'a'})
        payload, _ = self.tool('mcp session_stop', 'session_stop', timeout=60)
        self.stopped = True

    def disconnect(self):
        self.client = self.server('legacy')
        self.start_session()
        _, window, log = self.hold('disconnect')
        started, interrupted = time.monotonic(), time.monotonic_ns()
        self.client.process.stdin.close()
        self.released_after('disconnect: release', log, interrupted)
        try:
            code = self.client.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise SmokeFailure('disconnect', 'the server did not exit after stdin closed') from None
        if code != 0:
            raise SmokeFailure('disconnect', f'server exit status {code}')
        ok('disconnect: server exit', f'status 0 after {time.monotonic() - started:.2f}s')
        self.after_server_gone('disconnect', window)

    def server_kill(self):
        self.client = self.server('legacy')
        self.start_session()
        _, window, log = self.hold('server-kill')
        interrupted = time.monotonic_ns()
        self.client.process.kill()
        self.client.process.wait(timeout=5)
        self.released_after('server-kill: release', log, interrupted)
        self.after_server_gone('server-kill', window)

    def released_after(self, step, log, interrupted_ns):
        """The fixture's key release, measured from the interruption on the same monotonic clock."""
        self.expect_released(step, log, within=1.0)
        late = (self.keys(log)[-1][2] - interrupted_ns) / 1e9
        if not 0 <= late <= .5:
            raise SmokeFailure(step, f'released {late:.3f}s after the interruption, expected within 0.5s')
        ok(step, f'released {late * 1000:.0f}ms after the interruption')

    def after_server_gone(self, label, window):
        status = self.run_cli(f'{label}: session status (CLI)', 'session', 'status', '--session', self.session)
        if status['result'].get('state') != 'ready':
            raise SmokeFailure(label, f'session not ready after the server left: {json.dumps(status)[:300]}')
        ok(label, 'the session kept running')
        self.desktop(f'{label}: key afterwards (CLI)', 'key', '--window', window, 'a')

    # Driver ----------------------------------------------------------------

    def run_mcp(self, name):
        started = time.monotonic()
        failure = None
        self.stopped = False
        try:
            getattr(self, SCENARIOS[name])()
        except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
            failure = error
        finally:
            if self.client is not None:
                self.client.close()
            if self.start_attempted:
                try:
                    if not self.stopped:
                        self.stop_session()
                    if self.generation is not None:
                        self.pids = [self.pids[0]] if self.pids else []
                        self.check_leaks()
                except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                    failure = failure or error
            self.stderr.seek(0)
            diagnostics = self.stderr.read().strip()
            if diagnostics:
                print('server stderr:\n' + diagnostics[:2000])
            self.stderr.close()
        if failure is not None:
            raise failure
        return time.monotonic() - started


SCENARIOS = {'flow': 'flow', 'cancel': 'cancel', 'disconnect': 'disconnect', 'server-kill': 'server_kill'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scenarios', nargs='*', metavar='SCENARIO', help='default: all of ' + ', '.join(SCENARIOS))
    parser.add_argument('--cli', default=shutil.which('agent-desktop'))
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=smoke.positive_int, default=1, metavar='N')
    parser.add_argument('--keep-artifacts', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    if not args.cli:
        parser.error('agent-desktop is not on PATH; install it (pip install .) or pass --cli')
    unknown = set(args.scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f'unknown scenario(s) {sorted(unknown)}; choose from {list(SCENARIOS)}')
    fixture = smoke.build_fixture()
    results = []
    for iteration in range(1, args.loop + 1):
        for name in args.scenarios or list(SCENARIOS):
            artifacts = Path(tempfile.mkdtemp(prefix=f'agent-desktop-mcp-{name}-'))
            artifacts.chmod(0o700)
            print(f'{name} ({iteration}/{args.loop})')
            scenario = MCPScenario(args.cli, args.dependency_root, artifacts, fixture, args.verbose)
            try:
                elapsed = scenario.run_mcp(name)
            except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                print(f'FAIL {name}: {error}')
                payload = getattr(error, 'payload', None)
                if payload is not None:
                    print(json.dumps(payload, indent=2)[:3000])
                print(f'artifacts kept at {artifacts}')
                return 1
            if args.keep_artifacts:
                print(f'artifacts kept at {artifacts}')
            else:
                shutil.rmtree(artifacts, ignore_errors=True)
            results.append(elapsed)
            print(f'{name} passed in {elapsed:.1f}s')
    print(f'PASS {len(results)} scenario run(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
