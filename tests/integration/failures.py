#!/usr/bin/env python3
"""Failure-path integration tests for an installed agent-desktop.

Each scenario starts its own session, breaks something on purpose, checks the
reported outcome, then requires that `session stop` leaves no process in the
generation's cgroup and no systemd unit:

    focus-loss       focus moves to another window during `key --hold 2`
    cancel-hold      Ctrl-C on the client during `key --hold 2`
    cancel-type      Ctrl-C on the client during a long `type`
    generation       stale and mismatched generations are refused
    compositor-death SIGKILL kwin_wayland
    bus-death        SIGKILL the private dbus-daemon
    worker-sigkill   SIGKILL the worker during a hold
    worker-stopped   SIGSTOP the worker, then `session stop`
    title-gone       `wait --for title|gone`: retitle, timeout, a window lost
                     mid-wait, and a dialog closing while the app keeps running

    python tests/integration/failures.py [SCENARIO ...] [--loop N]

Not part of the unit-test suite; it needs a KDE Plasma 6 host. See docs/TESTING.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke  # noqa: E402
from smoke import ROOT, SmokeFailure, cgroup_members, log_events, read  # noqa: E402

KDOTOOL = 'bin/kdotool'  # Relative to the dependency root.
W = 17  # evdev KEY_W


class Scenario(smoke.Smoke):
    def __init__(self, cli, dependency_root, artifacts, fixture, verbose):
        super().__init__(cli, dependency_root, artifacts, fixture, None, verbose)

    # Helpers ---------------------------------------------------------------

    def fixture_window(self, *extra, windows=1):
        app, window, logs = self.launch('fixture', str(self.fixture), '--autonomous', '--exit-after-ms', '60000', *extra,
                                        windows=windows)
        self.desktop('fixture: focus', 'focus', '--window', window)
        return app, window, Path(logs['stdout'])

    def session_process(self, name):
        """PID of NAME (comm) in this generation's cgroup."""
        for pid in cgroup_members(f'agent-desktop-{self.generation}'):
            if read(f'/proc/{pid}/comm').strip() == name:
                return pid
        raise SmokeFailure('setup', f'no {name} process in the session')

    def private_env(self):
        root = f'/run/user/{os.getuid()}/agent-desktop/g/{self.generation}/desktop'
        return os.environ | {'DBUS_SESSION_BUS_ADDRESS': f'unix:path={root}/bus', 'XDG_RUNTIME_DIR': root}

    def background_cli(self, *args):
        argv = [self.cli, '--json', *args]
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def interrupt(self, process, after):
        time.sleep(after)
        process.send_signal(signal.SIGINT)
        out, _ = process.communicate(timeout=10)
        return process.returncode, json.loads(out)

    def keys(self, log):
        return [(row['key'], row['state'], row['monotonic_ns']) for row in log_events(log, {'key'})]

    def expect_released(self, step, log, *, within=None, start_ns=None):
        rows = smoke_wait(lambda: self.keys(log), lambda rows: rows and rows[-1][1] == 0)
        if not rows or rows[-1][1] != 0:
            raise SmokeFailure(step, f'key still held in the application: {rows}')
        presses = [r for r in rows if r[1] == 1]
        held_for = (rows[-1][2] - presses[-1][2]) / 1e9 if presses else 0
        if within is not None and held_for > within:
            raise SmokeFailure(step, f'released after {held_for:.2f}s, expected within {within}s')
        ok(step, f'application saw the release after {held_for:.2f}s')
        return held_for

    def wait_state(self, step, state, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            payload = self.run_cli(f'{step} (status)', 'session', 'status', '--session', self.session,
                                   quiet=True, expect_ok=False)
            # A failed session reports through the error, with its state in the context.
            current = (payload['result'] or (payload['error'] or {}).get('context') or {}).get('state')
            if current == state:
                ok(step, f'session {state}: {(payload["error"] or {}).get("message")}')
                return payload
            if time.monotonic() >= deadline:
                raise SmokeFailure(step, f'session is {current}, expected {state}', payload)
            time.sleep(.2)

    # Scenarios -------------------------------------------------------------

    def focus_loss(self):
        app, window, log = self.fixture_window('--sibling', windows=2)
        sibling = self.windows[1]['window']
        kdotool = str(Path(self.dependency_root) / KDOTOOL)
        def steal():
            time.sleep(.8)
            subprocess.run([kdotool, 'windowactivate', '{' + sibling['window_id'] + '}'], env=self.private_env(),
                           capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
        thief = threading.Thread(target=steal)
        thief.start()
        started = time.monotonic()
        payload = self.desktop('key --hold 2 w (focus stolen at 0.8s)', 'key', '--window', window, '--hold', '2', 'w',
                               expect_ok=False)
        elapsed = time.monotonic() - started
        thief.join()
        error = payload['error'] or {}
        if (error.get('code'), error.get('context', {}).get('reason')) != ('target_lost', 'focus_lost'):
            raise SmokeFailure('focus-loss', f'expected target_lost/focus_lost, got {json.dumps(payload)[:400]}')
        if elapsed > 1.6:
            raise SmokeFailure('focus-loss', f'hold ran {elapsed:.2f}s; focus loss should end it near 1s')
        ok('focus-loss', f'target_lost/focus_lost after {elapsed:.2f}s, '
                         f'{error["context"].get("focus_rechecks")} rechecks passed first')
        self.expect_released('focus-loss: release', log, within=1.6)

    def cancel_hold(self):
        _, window, log = self.fixture_window()
        process = self.background_cli('key', '--session', self.session, '--window', window, '--hold', '2', 'w')
        code, payload = self.interrupt(process, .6)
        if payload['ok'] or payload['error']['code'] != 'cancelled' or code != 130:
            raise SmokeFailure('cancel-hold', f'exit {code}: {json.dumps(payload)[:400]}')
        ok('cancel-hold', 'client got cancelled (exit 130)')
        self.expect_released('cancel-hold: release', log, within=1.0)
        self.desktop('cancel-hold: key afterwards', 'key', '--window', window, 'a')

    def cancel_type(self):
        _, window, log = self.fixture_window()
        process = self.background_cli('type', '--session', self.session, '--window', window, '--timeout', '30', 'a' * 1500)
        code, payload = self.interrupt(process, .8)
        if payload['ok'] or payload['error']['code'] != 'cancelled':
            raise SmokeFailure('cancel-type', f'exit {code}: {json.dumps(payload)[:400]}')
        time.sleep(.3)
        typed = sum(1 for key, state, _ in self.keys(log) if state == 1)
        time.sleep(.5)
        later = sum(1 for key, state, _ in self.keys(log) if state == 1)
        if not 0 < typed < 1500 or later != typed:
            raise SmokeFailure('cancel-type', f'{typed} characters, then {later}; typing must stop at cancel')
        ok('cancel-type', f'typing stopped after {typed} of 1500 characters')
        self.expect_released('cancel-type: release', log)

    def generation_refusal(self):
        _, window, _ = self.fixture_window()
        other = uuid.uuid4().hex
        payload = self.desktop('windows --generation OTHER', 'windows', '--generation', other, expect_ok=False)
        if (payload['error'] or {}).get('code') != 'generation_mismatch':
            raise SmokeFailure('generation', f'expected generation_mismatch: {json.dumps(payload)[:300]}')
        ok('generation', 'mismatched --generation refused')
        stale = other + ':' + window.split(':', 1)[1]
        payload = self.desktop('key with a stale window ref', 'key', '--window', stale, 'a', expect_ok=False)
        if (payload['error'] or {}).get('code') != 'generation_mismatch':
            raise SmokeFailure('generation', f'expected generation_mismatch: {json.dumps(payload)[:300]}')
        ok('generation', 'window ref from another generation refused')

    def kill_component(self, name, component):
        _, window, _ = self.fixture_window()
        os.kill(self.session_process(name), signal.SIGKILL)
        ok(f'{name} killed', '')
        self.wait_state(f'{name} death', 'failed')
        payload = self.desktop(f'key after {name} death', 'key', '--window', window, 'a', expect_ok=False)
        if (payload['error'] or {}).get('code') != 'session_unavailable':
            raise SmokeFailure(f'{name} death', f'expected session_unavailable: {json.dumps(payload)[:300]}')
        status = self.run_cli(f'{name} death (status)', 'session', 'status', '--session', self.session,
                              expect_ok=False, quiet=True)
        failure = (status['result'] or (status['error'] or {}).get('context') or {}).get('failure') or {}
        if failure.get('context', {}).get('component') != component:
            raise SmokeFailure(f'{name} death', f'status does not name the cause: {json.dumps(status)[:400]}')
        ok(f'{name} death', f'status: {(status["error"] or {}).get("message") or failure.get("message")}')
        self.stop_session()
        manifest = json.loads(read(self.artifacts / 'generations' / self.generation / 'manifest.json'))
        cause = (manifest.get('failure') or {}).get('context', {})
        if (cause.get('component'), cause.get('returncode')) != (component, -signal.SIGKILL):
            raise SmokeFailure(f'{name} death', f'manifest does not name the cause: {json.dumps(manifest)[:400]}')
        ended = [app['ended_by_session_stop'] for app in manifest.get('applications', [])]
        if ended != [True]:
            raise SmokeFailure(f'{name} death', f'manifest applications: {manifest.get("applications")}')
        ok(f'{name} death', f'manifest failure: {component} exited with SIGKILL')

    def compositor_death(self):
        # Also drops the EIS connection; the compositor's exit must still be the recorded cause.
        self.kill_component('kwin_wayland', 'compositor')

    def bus_death(self):
        self.kill_component('dbus-daemon', 'bus')

    def worker_sigkill(self):
        _, window, log = self.fixture_window()
        process = self.background_cli('key', '--session', self.session, '--window', window, '--hold', '2', 'w')
        rows = smoke_wait(lambda: self.keys(log), lambda rows: any(r[:2] == (W, 1) for r in rows))
        if not any(r[:2] == (W, 1) for r in rows) or process.poll() is not None:
            raise SmokeFailure('worker-sigkill', f'the hold never started: {rows}')
        os.kill(self.pids[0], signal.SIGKILL)
        out, _ = process.communicate(timeout=10)
        payload = json.loads(out)
        if (payload['error'] or {}).get('code') != 'completion_unknown':
            raise SmokeFailure('worker-sigkill', f'expected completion_unknown: {json.dumps(payload)[:300]}')
        ok('worker-sigkill', f'client got {payload["error"]["code"]}')
        status = self.run_cli('worker-sigkill (status)', 'session', 'status', '--session', self.session, expect_ok=False)
        ok('worker-sigkill', f'status: {json.dumps(status["result"] or status["error"])[:160]}')

    def worker_stopped(self):
        self.fixture_window()
        os.kill(self.pids[0], signal.SIGSTOP)
        ok('worker SIGSTOP', '')
        started = time.monotonic()
        payload = self.run_cli('session stop (worker stopped)', 'session', 'stop', '--session', self.session,
                               expect_ok=False, timeout=60)
        elapsed = time.monotonic() - started
        self.stopped = True
        if not payload['ok'] or payload['result'].get('cleanup') != 'complete':
            raise SmokeFailure('worker-stopped', f'{elapsed:.1f}s: {json.dumps(payload)[:400]}')
        ok('worker-stopped', f'stop cleaned up in {elapsed:.1f}s')

    def expect_error(self, step, code, *args):
        payload = self.desktop(step, *args, expect_ok=False)
        if payload['ok'] or payload['error']['code'] != code:
            raise SmokeFailure(step, f'expected {code}: {json.dumps(payload)[:400]}', payload)
        return payload['error']

    def title_gone_waits(self):
        # Timers count from fixture start: retitle the primary at 3s, close the
        # sibling at 4.5s and the dialog at 6s. The fixture keeps running.
        app, primary, logs = self.launch('fixture', str(self.fixture), '--autonomous', '--exit-after-ms', '60000',
                                         '--sibling', '--dialog', '--retitle-after-ms', 'primary:3000',
                                         '--destroy-after-ms', 'sibling:4500', '--destroy-after-ms', 'dialog:6000',
                                         windows=3)
        sibling, dialog = (row['window']['ref'] for row in self.windows[1:])
        original = self.windows[0]['title']
        if original != 'KDE Agent Native Fixture':
            raise SmokeFailure('title-gone', f'primary already retitled ({original!r}); launch was too slow')
        error = self.expect_error('wait --for title (timeout)', 'timeout', 'wait', '--for', 'title',
                                  '--window', primary, '--match', 'never this title', '--timeout', '1')
        if error['context'].get('phase') != 'title_wait':
            raise SmokeFailure('title-gone', f'timeout without title_wait phase: {error}')
        ok('wait --for title (timeout)', 'timeout, phase title_wait')
        result = self.desktop('wait --for title (retitled)', 'wait', '--for', 'title', '--window', primary,
                              '--match', 'retitled')['result']
        if result['title'] != 'KDE Agent Native Fixture retitled' or result['polls'] < 2:
            raise SmokeFailure('title-gone', f'unexpected title result {json.dumps(result)[:300]}')
        if not log_events(Path(logs['stdout']), {'scheduled_retitle'}):
            raise SmokeFailure('title-gone', 'fixture never logged its retitle')
        ok('wait --for title (retitled)', f'{result["title"]!r} after {result["polls"]} polls')
        result = self.desktop('wait --for title --regex (initial)', 'wait', '--for', 'title', '--window', primary,
                              '--regex', '--match', r'^KDE \w+ Native Fixture retitled$')['result']
        if result['polls'] != 1:
            raise SmokeFailure('title-gone', f'an initial match must return on the first poll: {result["polls"]}')
        error = self.expect_error('wait --for title (window lost)', 'target_lost', 'wait', '--for', 'title',
                                  '--window', sibling, '--match', 'never this title')
        ok('wait --for title (window lost)', f'target_lost in phase {error["context"].get("phase")}')
        result = self.desktop('wait --for gone (dialog)', 'wait', '--for', 'gone', '--window', dialog)['result']
        if result['already_gone'] or result['last_seen']['window']['ref'] != dialog:
            raise SmokeFailure('title-gone', f'unexpected gone result {json.dumps(result)[:300]}')
        rows = self.desktop('windows (app still running)', 'windows', '--app', app)['result']['windows']
        if [row['window']['ref'] for row in rows] != [primary]:
            raise SmokeFailure('title-gone', f'expected only the primary window: {[r["title"] for r in rows]}')
        ok('wait --for gone (dialog)', f'after {result["polls"]} polls; primary still listed')
        result = self.desktop('wait --for gone (already gone)', 'wait', '--for', 'gone', '--window', dialog)['result']
        if not result['already_gone'] or result['polls'] != 1:
            raise SmokeFailure('title-gone', f'expected already_gone: {json.dumps(result)[:300]}')
        self.expect_error('wait --for title (gone window)', 'target_not_found', 'wait', '--for', 'title',
                          '--window', dialog, '--match', 'x')
        stale = uuid.uuid4().hex + ':' + dialog.split(':', 1)[1]
        self.expect_error('wait --for gone (stale ref)', 'generation_mismatch', 'wait', '--for', 'gone', '--window', stale)

    # Driver ----------------------------------------------------------------

    def stop_session(self):
        self.stopped = True
        payload = self.run_cli('session stop', 'session', 'stop', '--session', self.session, expect_ok=False, timeout=60)
        if self.generation is None and payload.get('session'):
            self.generation = payload['session'].get('generation')
        if not payload['ok'] or payload['result'].get('cleanup') != 'complete':
            raise SmokeFailure('session stop', json.dumps(payload)[:400], payload)

    def run_scenario(self, name):
        started = time.monotonic()
        failure = None
        self.stopped = False
        try:
            self.start()
            getattr(self, SCENARIOS[name])()
        except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
            failure = error
        finally:
            if self.start_attempted:
                try:
                    if not self.stopped:
                        self.stop_session()
                    if self.generation is not None:
                        self.pids = [self.pids[0]] if self.pids else []
                        self.check_leaks()
                except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                    failure = failure or error
        if failure is not None:
            raise failure
        return time.monotonic() - started


def smoke_wait(fetch, done, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if done(value) or time.monotonic() >= deadline:
            return value
        time.sleep(.05)


def ok(step, detail):
    print(f'  ok  {step:<34}       {detail}')


SCENARIOS = {'focus-loss': 'focus_loss', 'cancel-hold': 'cancel_hold', 'cancel-type': 'cancel_type',
             'generation': 'generation_refusal', 'compositor-death': 'compositor_death', 'bus-death': 'bus_death',
             'worker-sigkill': 'worker_sigkill', 'worker-stopped': 'worker_stopped', 'title-gone': 'title_gone_waits'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scenarios', nargs='*', metavar='SCENARIO', help='default: all of ' + ', '.join(SCENARIOS))
    parser.add_argument('--cli', default=shutil.which('agent-desktop'))
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=int, default=1, metavar='N')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    if not args.cli:
        parser.error('agent-desktop is not on PATH; install it (pip install .) or pass --cli')
    unknown = set(args.scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f'unknown scenario(s) {sorted(unknown)}; choose from {list(SCENARIOS)}')
    fixture = smoke.build_fixture()
    names = args.scenarios or list(SCENARIOS)
    results = []
    for iteration in range(1, args.loop + 1):
        for name in names:
            artifacts = Path(tempfile.mkdtemp(prefix=f'agent-desktop-{name}-'))
            artifacts.chmod(0o700)
            print(f'{name} ({iteration}/{args.loop})')
            try:
                elapsed = Scenario(args.cli, args.dependency_root, artifacts, fixture, args.verbose).run_scenario(name)
            except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                print(f'FAIL {name}: {error}')
                payload = getattr(error, 'payload', None)
                if payload is not None:
                    print(json.dumps(payload, indent=2)[:3000])
                print(f'artifacts kept at {artifacts}')
                return 1
            shutil.rmtree(artifacts, ignore_errors=True)
            results.append(elapsed)
            print(f'{name} passed in {elapsed:.1f}s')
    print(f'PASS {len(results)} scenario run(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
