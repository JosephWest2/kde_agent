#!/usr/bin/env python3
"""End-to-end smoke test for an installed agent-desktop.

Drives the real CLI through separate invocations against the native Wayland
fixture (exact key acknowledgements) and gnome-text-editor (a real GTK app):

    doctor, session start, launch, windows, focus, key, type, click,
    screenshot, wait, close, kill, session stop

After stop it checks that no process remains in the generation's cgroup, the
systemd unit is gone and the artifacts survived. Run it after system updates:

    python tests/integration/smoke.py [--loop 5]

Not part of the unit-test suite; it needs a KDE Plasma 6 host. See docs/TESTING.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CACHE = ROOT / '.local' / 'smoke' / 'fixture'
EDITOR = '/usr/bin/gnome-text-editor'

# key ctrl+shift+t, key --hold 0.2 w, type 'aB!'
EXPECTED_KEYS = [(29, 1), (42, 1), (20, 1), (20, 0), (42, 0), (29, 0),
                 (17, 1), (17, 0),
                 (30, 1), (30, 0), (42, 1), (48, 1), (48, 0), (42, 0), (42, 1), (2, 1), (2, 0), (42, 0)]
# Effective XKB modifiers (Shift=1, Ctrl=4) when each non-modifier key goes down.
EXPECTED_MODIFIERS = {20: 5, 17: 0, 30: 0, 48: 1, 2: 1}
MODIFIER_KEYS = {29, 42}
# click --x 100 --y 50; click --x 20 --y 30 --button right --count 2 (button, state, x, y)
EXPECTED_BUTTONS = [(272, 1, 100, 50), (272, 0, 100, 50)] + [(273, 1, 20, 30), (273, 0, 20, 30)] * 2
# gnome-text-editor's "New Tab" header-bar button, in client coordinates.
EDITOR_NEW_TAB = (107, 23)


class SmokeFailure(Exception):
    def __init__(self, step, message, payload=None):
        super().__init__(f'{step}: {message}')
        self.step, self.payload = step, payload


class Smoke:
    def __init__(self, cli, dependency_root, artifacts, fixture, editor, verbose):
        self.cli, self.dependency_root = cli, dependency_root
        self.artifacts, self.fixture, self.editor = artifacts, fixture, editor
        self.verbose = verbose
        self.session = 'smoke-' + uuid.uuid4().hex[:8]
        self.generation = None
        self.start_attempted = False
        self.pids = []
        self.screenshots = []

    def run_cli(self, step, *args, timeout=40, expect_ok=True):
        started = time.monotonic()
        argv = [self.cli, '--json', *args]
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            raise SmokeFailure(step, f'no JSON result (exit {process.returncode}): {process.stderr.strip()[:300]}')
        elapsed = time.monotonic() - started
        if expect_ok and not payload['ok']:
            error = payload['error']
            raise SmokeFailure(step, f"{error['code']}: {error['message']} {json.dumps(error['context'])[:300]}", payload)
        print(f'  ok  {step:<34} {elapsed:5.2f}s')
        if self.verbose:
            print('      ' + json.dumps(payload.get('result') or payload.get('error'))[:400])
        return payload

    def desktop(self, step, operation, *args, **kwargs):
        return self.run_cli(step, *operation.split(), '--session', self.session, *args, **kwargs)

    # Steps -----------------------------------------------------------------

    def start(self):
        self.start_attempted = True
        # Start may use its 30s budget plus a 15s cleanup reserve.
        payload = self.run_cli('session start', 'session', 'start', '--session', self.session,
                               '--artifacts', str(self.artifacts), '--dependency-root', str(self.dependency_root),
                               timeout=60)
        self.generation = payload['session']['generation']
        self.pids.append(payload['result']['worker_pid'])
        supported = set(payload['result']['supported_operations'])
        missing = {'launch', 'windows', 'focus', 'key', 'type', 'click', 'screenshot', 'close', 'kill'} - supported
        if missing:
            raise SmokeFailure('session start', f'operations not supported: {sorted(missing)}')

    def launch(self, label, *argv):
        payload = self.desktop(f'{label}: launch', 'launch', '--wait-window', '--', *argv)
        result = payload['result']
        self.pids.append(result['process']['pid'])
        app = result['application']['ref']
        windows = self.desktop(f'{label}: windows', 'windows', '--app', app)['result']['windows']
        if len(windows) != 1:
            raise SmokeFailure(f'{label}: windows', f'expected one window, saw {len(windows)}')
        return app, windows[0]['window']['ref'], result['logs']

    def screenshot(self, label, *args):
        result = self.desktop(f'{label}: screenshot {" ".join(a for a in args if a.startswith("--")) or "(full)"}',
                              'screenshot', *args)['result']
        width, height = png_size(Path(result['path']))
        if [width, height] != result['dimensions']:
            raise SmokeFailure(f'{label}: screenshot', f'PNG is {width}x{height}, result says {result["dimensions"]}')
        if hashlib.sha256(Path(result['path']).read_bytes()).hexdigest() != result['png_sha256']:
            raise SmokeFailure(f'{label}: screenshot', 'PNG hash does not match the result')
        self.screenshots.append(Path(result['path']))
        return result

    def fixture_flow(self):
        app, window, logs = self.launch('fixture', str(self.fixture), '--autonomous', '--exit-after-ms', '60000')
        self.desktop('fixture: focus', 'focus', '--window', window)
        self.desktop('fixture: key ctrl+shift+t', 'key', '--window', window, 'ctrl+shift+t')
        self.desktop('fixture: key --hold 0.2 w', 'key', '--window', window, '--hold', '0.2', 'w')
        self.desktop("fixture: type 'aB!'", 'type', '--window', window, 'aB!')
        self.desktop('fixture: click 100,50', 'click', '--window', window, '--x', '100', '--y', '50')
        self.desktop('fixture: double right-click', 'click', '--window', window, '--x', '20', '--y', '30',
                     '--button', 'right', '--count', '2')
        wait_for_keys(Path(logs['stdout']), len(EXPECTED_KEYS))
        self.screenshot('fixture', '--window', window)
        result = self.desktop('fixture: close', 'close', '--app', app)['result']
        if result['exited'] is not True:
            raise SmokeFailure('fixture: close', 'application did not exit')
        # The fixture has exited, so this is its complete log: no late extras.
        hold = check_key_log(Path(logs['stdout']))
        print(f'  ok  {"fixture: key acknowledgements":<34}       order, modifiers, text, hold {hold}ms')
        check_button_log(Path(logs['stdout']))
        print(f'  ok  {"fixture: button acknowledgements":<34}       position, button, count')

    def editor_flow(self):
        app, window, _ = self.launch('editor', self.editor)
        self.desktop('editor: focus', 'focus', '--window', window)
        self.desktop('editor: wait --for focus', 'wait', '--for', 'focus', '--window', window)
        text = 'agent desktop smoke ' + uuid.uuid4().hex[:6]
        self.desktop('editor: type', 'type', '--window', window, text)
        deadline = time.monotonic() + 5
        while True:
            rows = self.desktop('editor: windows (title check)', 'windows', '--app', app)['result']['windows']
            if any(text in (row['title'] or '') for row in rows):
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('editor: title check', f'typed text not in title: {[r["title"] for r in rows]}')
            time.sleep(.2)
        x, y = EDITOR_NEW_TAB
        self.desktop('editor: click New Tab', 'click', '--window', window, '--x', str(x), '--y', str(y))
        deadline = time.monotonic() + 5
        while True:  # The new empty document becomes current: same window, "New Document" title.
            rows = self.desktop('editor: windows (new tab check)', 'windows', '--app', app)['result']['windows']
            titles = [row['title'] or '' for row in rows if row['window']['ref'] == window]
            if titles and titles[0].startswith('New Document') and text not in titles[0]:
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('editor: new tab check', f'title still shows the typed text: '
                                   f'{[r["title"] for r in rows]}')
            time.sleep(.2)
        # Switching back to the first tab must show the typed document again.
        self.desktop('editor: key ctrl+page_up', 'key', '--window', window, 'ctrl+page_up')
        deadline = time.monotonic() + 5
        while True:
            rows = self.desktop('editor: windows (first tab check)', 'windows', '--app', app)['result']['windows']
            if any(text in (row['title'] or '') for row in rows if row['window']['ref'] == window):
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('editor: first tab check', f'typed document not back: {[r["title"] for r in rows]}')
            time.sleep(.2)
        self.desktop('editor: key ctrl+a', 'key', '--window', window, 'ctrl+a')
        self.screenshot('editor')
        self.screenshot('editor', '--window', window)
        result = self.desktop('editor: kill', 'kill', '--app', app)['result']
        if result.get('exited') is not True:
            raise SmokeFailure('editor: kill', f'application did not exit: {json.dumps(result)[:300]}')

    def stop(self):
        # Stop by name: it works even if start's response was lost before the
        # generation was known, and reports that generation for verification.
        payload = self.run_cli('session stop', 'session', 'stop', '--session', self.session, timeout=30)
        if self.generation is None and payload.get('session'):
            self.generation = payload['session'].get('generation')
        if payload['result'].get('cleanup') not in ('complete', None):
            raise SmokeFailure('session stop', f'cleanup {payload["result"].get("cleanup")}')

    def check_leaks(self):
        unit = f'agent-desktop-{self.generation}.service'
        marker = f'agent-desktop-{self.generation}'
        deadline = time.monotonic() + 5
        while True:
            leftovers = cgroup_members(marker)
            try:
                state = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '--value'],
                                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                       timeout=max(.5, deadline - time.monotonic())).stdout.split()
            except subprocess.TimeoutExpired:
                raise SmokeFailure('leak check', 'systemctl --user show did not answer') from None
            if not leftovers and state[:1] == ['not-found']:
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('leak check', f'processes in cgroup: {leftovers}; unit state: {state}')
            time.sleep(.1)
        alive = [pid for pid in self.pids if marker in read(f'/proc/{pid}/cgroup')]
        if alive:
            raise SmokeFailure('leak check', f'owned PIDs still alive: {alive}')
        print(f'  ok  {"leak check":<34}       no processes, unit gone')

    def check_artifacts(self):
        generation = self.artifacts / 'generations' / self.generation
        manifest = json.loads((generation / 'manifest.json').read_text())
        # A desktop that failed after the last step can still stop cleanly; that
        # is not a pass.
        if manifest.get('state') != 'stopped' or not all(path.is_file() for path in self.screenshots):
            raise SmokeFailure('artifacts', f'manifest state {manifest.get("state")}, screenshots {self.screenshots}')
        if not (generation / 'shutdown.json').is_file():
            raise SmokeFailure('artifacts', 'shutdown.json missing')
        print(f'  ok  {"artifacts":<34}       manifest, shutdown record, {len(self.screenshots)} screenshots')

    def run(self):
        started = time.monotonic()
        failure = None
        recoverable = (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError)
        try:
            self.start()
            if self.fixture is not None:
                self.fixture_flow()
            if self.editor is not None:
                self.editor_flow()
        except recoverable as error:
            failure = error
        finally:
            # Each cleanup stage runs independently, so verification still
            # happens when stop itself fails. Runs on KeyboardInterrupt too.
            if self.start_attempted:
                for stage in (self.stop, self.check_leaks, self.check_artifacts):
                    if stage != self.stop and self.generation is None:
                        failure = failure or SmokeFailure('cleanup', 'generation unknown; cannot verify cleanup')
                        break
                    try:
                        stage()
                    except recoverable as error:
                        failure = failure or error
        if failure is not None:
            raise failure
        return time.monotonic() - started


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''


def cgroup_members(marker):
    members = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit() and marker in read(entry / 'cgroup'):
            members.append(int(entry.name))
    return members


def png_size(path):
    with open(path, 'rb') as stream:
        header = stream.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise SmokeFailure('screenshot', f'{path} is not a PNG')
    return struct.unpack('>II', header[16:24])


def log_events(log, kinds):
    events = []
    for line in read(log).splitlines():
        if line.startswith('{'):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('event') in kinds:
                events.append(row)
    return events


def wait_for_keys(log, count, timeout=3):
    """Bounded wait until the fixture has logged at least COUNT key events."""
    deadline = time.monotonic() + timeout
    while len(log_events(log, {'key'})) < count and time.monotonic() < deadline:
        time.sleep(.05)


def check_button_log(log):
    rows = log_events(log, {'button'})
    observed = [(row['button'], row['state'], row['x'], row['y']) for row in rows]
    if observed != EXPECTED_BUTTONS or any(row['surface'] != 'primary' for row in rows):
        raise SmokeFailure('fixture: button acknowledgements', f'expected {EXPECTED_BUTTONS}, saw {observed}')


def check_key_log(log):
    """Validate the complete fixture log; return the measured hold of `w` in ms."""
    step = 'fixture: key acknowledgements'
    rows = log_events(log, {'key', 'modifiers'})
    keys = [row for row in rows if row['event'] == 'key']
    observed = [(row['key'], row['state']) for row in keys]
    if observed != EXPECTED_KEYS:
        raise SmokeFailure(step, f'expected {EXPECTED_KEYS}, saw {observed}')
    depressed = 0
    for row in rows:
        if row['event'] == 'modifiers':
            depressed = row['depressed']
        elif row['state'] == 1 and row['key'] not in MODIFIER_KEYS:
            if depressed != EXPECTED_MODIFIERS[row['key']]:
                raise SmokeFailure(step, f'key {row["key"]} arrived with modifiers {depressed}, '
                                         f'expected {EXPECTED_MODIFIERS[row["key"]]}')
    if depressed != 0:
        raise SmokeFailure(step, f'modifiers still active at the end: {depressed}')
    texts = ''.join(row['text'] for row in keys[8:] if row['state'] == 1 and row['key'] not in MODIFIER_KEYS)
    hold = keys[7]['time_ms'] - keys[6]['time_ms']
    if texts != 'aB!' or not 180 <= hold <= 1000:
        raise SmokeFailure(step, f'text {texts!r}, hold {hold}ms')
    return hold


def build_fixture():
    """Build the native fixture once into .local/smoke, rebuilding when its source changes."""
    source = hashlib.sha256((ROOT / 'tools' / 'wayland_fixture.c').read_bytes()).hexdigest()
    binary = FIXTURE_CACHE / 'wayland-fixture'
    receipt = FIXTURE_CACHE / 'build.json'
    try:
        if binary.is_file() and json.loads(receipt.read_text())['source_sha256'] == source:
            return binary
    except (OSError, ValueError, KeyError):
        pass
    if FIXTURE_CACHE.exists():
        shutil.rmtree(FIXTURE_CACHE)
    FIXTURE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / 'tools'))
    import private_harness
    return private_harness.build(FIXTURE_CACHE)[0]


def print_versions(cli, dependency_root):
    payload = json.loads(subprocess.run([cli, '--json', 'doctor', '--dependency-root', str(dependency_root)],
                                        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=150).stdout)
    if not payload['ok']:
        error = payload['error']
        raise SmokeFailure('doctor', f"{error['code']}: {error['message']}")
    observed = {item['name']: item['observed'] for item in payload['result']['dependencies']}
    runtime, libei, kdotool = observed['runtime_executables'], observed['libei'], observed['kdotool']
    print(f"versions: {runtime['kwin_version']} (tested {runtime['kwin_tested_version']}), "
          f"libei {libei['version']} (tested {libei['tested_version']}), kdotool {kdotool['revision'][:12]}, "
          f"python {observed['python_bindings']['python']}")
    for warning in payload['result']['warnings']:
        print(f"warning: {warning['code']}: {warning['message']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--cli', default=shutil.which('agent-desktop'), help='installed agent-desktop executable')
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=int, default=1, metavar='N', help='run N full cycles (default 1)')
    parser.add_argument('--keep-artifacts', action='store_true', help='keep artifacts after passing runs')
    parser.add_argument('--no-fixture', action='store_true', help='skip the native fixture (no gcc needed)')
    parser.add_argument('--no-editor', action='store_true', help=f'skip {EDITOR}')
    parser.add_argument('--verbose', action='store_true', help='print each result')
    args = parser.parse_args(argv)
    if not args.cli:
        parser.error('agent-desktop is not on PATH; install it (pip install .) or pass --cli')
    editor = None if args.no_editor else EDITOR
    if editor is not None and not Path(editor).is_file():
        parser.error(f'{EDITOR} is not installed; install it or pass --no-editor')
    try:
        print_versions(args.cli, args.dependency_root)
        fixture = None if args.no_fixture else build_fixture()
    except SmokeFailure as error:
        print(f'FAIL {error}')
        return 1
    except Exception as error:  # Fixture build: compiler or protocol files missing.
        print(f'FAIL fixture build: {type(error).__name__}: {error} (pass --no-fixture to skip)')
        return 1
    durations = []
    for iteration in range(1, args.loop + 1):
        artifacts = Path(tempfile.mkdtemp(prefix='agent-desktop-smoke-'))
        artifacts.chmod(0o700)
        print(f'run {iteration}/{args.loop} (artifacts {artifacts})')
        try:
            durations.append(Smoke(args.cli, args.dependency_root, artifacts, fixture, editor, args.verbose).run())
        except (SmokeFailure, subprocess.TimeoutExpired) as error:
            print(f'FAIL {error}')
            payload = getattr(error, 'payload', None)
            if payload is not None:
                print(json.dumps(payload, indent=2)[:3000])
            print(f'artifacts kept at {artifacts}')
            return 1
        if not args.keep_artifacts:
            shutil.rmtree(artifacts, ignore_errors=True)
        print(f'run {iteration} passed in {durations[-1]:.1f}s')
    print(f'PASS {len(durations)} run(s); slowest {max(durations):.1f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
