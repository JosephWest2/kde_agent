#!/usr/bin/env python3
"""Failure-path integration tests for an installed agent-desktop.

Each scenario starts its own session, breaks something on purpose, checks the
reported outcome, then requires that `session stop` leaves no process in the
generation's cgroup and no systemd unit:

    focus-loss       focus moves to another window during `key --hold 2`
    cancel-hold      Ctrl-C on the client during `key --hold 2`
    cancel-type      Ctrl-C on the client during a long `type`
    scroll-interrupted  Ctrl-C, then focus loss, during `scroll --dy 50`: the
                     wheel stops and steps_sent matches what the fixture got
    generation       stale and mismatched generations are refused
    compositor-death SIGKILL kwin_wayland
    bus-death        SIGKILL the private dbus-daemon
    worker-sigkill   SIGKILL the worker during a hold
    worker-stopped   SIGSTOP the worker, then `session stop`
    title-gone       `wait --for title|gone`: retitle, timeout, a window lost
                     mid-wait, a dialog closing while the app keeps running, and
                     a bad --regex sent straight over the transport
                     (AGENT_DESKTOP_TEST_SLOW=SECONDS adds setup delay)

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
# Extra seconds of setup delay in title-gone, to show its checks don't depend on timing.
SLOW = float(os.environ.get('AGENT_DESKTOP_TEST_SLOW', '0'))
# A client other than the CLI: builds a request from this checkout's sources and sends
# it with the transport directly, skipping the CLI's own checks. Spec on stdin.
RAW_CLIENT = (
    'import json,sys\n'
    'from agent_desktop.contracts import make_request\n'
    'from agent_desktop.transport import exchange\n'
    'spec=json.load(sys.stdin)\n'
    'print(json.dumps(exchange(make_request(spec.pop("operation"),caller_cwd="/",**spec))))\n'
)


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

    def scroll_interrupted(self):
        """A long scroll stops between wheel steps, and its progress counts exactly the steps sent."""
        _, window, log = self.fixture_window('--sibling', windows=2)
        sibling = self.windows[1]['window']
        wheel = lambda: log_events(log, {'axis_value120', 'axis_stop'})
        scroll = ('scroll', '--session', self.session, '--window', window, '--x', '100', '--y', '100', '--dy', '50')
        process = self.background_cli(*scroll)
        smoke_wait(wheel, lambda rows: len(rows) >= 3)
        process.send_signal(signal.SIGINT)
        out, _ = process.communicate(timeout=10)
        payload = json.loads(out)
        if payload['ok'] or payload['error']['code'] != 'cancelled' or process.returncode != 130:
            raise SmokeFailure('scroll-interrupted: cancel', f'exit {process.returncode}: {json.dumps(payload)[:400]}')
        # The interrupted CLI reports a local cancellation, so it has no worker progress to compare.
        self.expect_wheel_stopped('scroll-interrupted: cancel', wheel, 0, payload['error'], progress=False)
        cancelled = len(wheel())
        self.desktop('scroll-interrupted: scroll afterwards', *scroll[:1], *scroll[3:-1], '1')
        before = len(smoke_wait(wheel, lambda rows: len(rows) > cancelled))
        if before != cancelled + 1:
            raise SmokeFailure('scroll-interrupted: scroll afterwards', f'{before - cancelled} wheel steps received')

        kdotool = str(Path(self.dependency_root) / KDOTOOL)
        def steal():
            smoke_wait(wheel, lambda rows: len(rows) >= before + 3)
            subprocess.run([kdotool, 'windowactivate', '{' + sibling['window_id'] + '}'], env=self.private_env(),
                           capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
        thief = threading.Thread(target=steal)
        thief.start()
        payload = self.desktop('scroll --dy 50 (focus stolen)', *scroll[:1], *scroll[3:], expect_ok=False)
        thief.join()
        error = payload['error'] or {}
        if (error.get('code'), error.get('context', {}).get('reason')) != ('target_lost', 'focus_lost'):
            raise SmokeFailure('scroll-interrupted: focus', f'expected target_lost/focus_lost, got {json.dumps(payload)[:400]}')
        self.expect_wheel_stopped('scroll-interrupted: focus', wheel, before, error)

    def expect_wheel_stopped(self, step, wheel, before, error, progress=True):
        """The wheel stopped part way, with no scroll stop, and the error reports exactly the steps received."""
        time.sleep(.3)
        rows = wheel()[before:]
        time.sleep(.3)
        later = wheel()[before:]
        context = error.get('context', {})
        if (any(row['event'] == 'axis_stop' for row in rows) or len(later) != len(rows) or not 3 <= len(rows) < 50
                or any(row['value120'] != 120 for row in rows) or error.get('outcome') != 'unknown'
                or progress and (context.get('steps_sent'), context.get('dy_sent'), context.get('steps_total'))
                != (len(rows), len(rows), 50)):
            raise SmokeFailure(step, f'{len(rows)} then {len(later)} wheel steps received; error {json.dumps(error)[:400]}')
        surfaces = sorted({str(row['surface']) for row in rows})
        ok(step, f'{error["code"]} after {len(rows)} of 50 steps (on {", ".join(surfaces)}); '
                 f'{"steps_sent matches, " * progress}no axis_stop')

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

    def raw_request(self, step, operation, arguments):
        spec = {'operation': operation, 'session': self.session, 'arguments': arguments, 'timeout_seconds': 10}
        process = subprocess.run([sys.executable, '-c', RAW_CLIENT], input=json.dumps(spec), capture_output=True,
                                 text=True, timeout=40, env=os.environ | {'PYTHONPATH': str(ROOT / 'src')})
        if process.returncode:
            raise SmokeFailure(step, f'raw client failed: {process.stderr[-300:]}')
        return json.loads(process.stdout)

    def observations(self):
        directory = self.artifacts / 'generations' / self.generation / 'window-observations'
        return set(directory.iterdir()) if directory.is_dir() else set()

    def wait_with_step(self, step, fixture_pid, *args):
        """Run a wait and apply the fixture's next SIGUSR1 step after the wait's first observation.

        Waits hold the session's only ordinary slot, so nothing else queries meanwhile:
        the first new observation artifact is this wait's first poll, taken before the
        change. However slow setup was, the change always happens mid-wait.
        """
        before = self.observations()
        started = time.monotonic()
        process = self.background_cli(*args, '--session', self.session)
        fresh = smoke_wait(lambda: self.observations() - before, bool, timeout=5)
        if not fresh or process.poll() is not None:
            process.kill()
            out, _ = process.communicate(timeout=10)
            raise SmokeFailure(step, f'no first observation while waiting: {out[:300]}')
        os.kill(fixture_pid, signal.SIGUSR1)
        out, _ = process.communicate(timeout=30)
        print(f'  ok  {step:<34} {time.monotonic() - started:5.2f}s')
        return json.loads(out)

    def title_gone_waits(self):
        # Each SIGUSR1 applies the fixture's next step: retitle the primary, close
        # the sibling, close the dialog. The fixture keeps running throughout.
        app, primary, logs = self.launch('fixture', str(self.fixture), '--autonomous', '--exit-after-ms', '60000',
                                         '--sibling', '--dialog',
                                         '--on-sigusr1', 'retitle:primary,close:sibling,close:dialog', windows=3)
        fixture_pid = self.pids[-1]
        sibling, dialog = (row['window']['ref'] for row in self.windows[1:])
        if self.windows[0]['title'] != 'KDE Agent Native Fixture':
            raise SmokeFailure('title-gone', f'unexpected primary title {self.windows[0]["title"]!r}')
        if SLOW:
            time.sleep(SLOW)  # Robustness check: nothing below depends on launch timing.
        error = self.expect_error('wait --for title (timeout)', 'timeout', 'wait', '--for', 'title',
                                  '--window', primary, '--match', 'never this title', '--timeout', '1')
        if (error['context'].get('phase') != 'title_wait' or error['context'].get('window', {}).get('ref') != primary
                or not error['context'].get('last_query_artifact')):
            raise SmokeFailure('title-gone', f'timeout context lacks phase/window/last query: {error}')
        ok('wait --for title (timeout)', 'timeout; context has phase, window and last_query_artifact')
        payload = self.wait_with_step('wait --for title (retitled)', fixture_pid, 'wait', '--for', 'title',
                                      '--window', primary, '--match', 'retitled')
        result = payload['result'] or {}
        if not payload['ok'] or result.get('title') != 'KDE Agent Native Fixture retitled' or result['polls'] < 2:
            raise SmokeFailure('title-gone', f'unexpected title result {json.dumps(payload)[:300]}')
        ok('wait --for title (retitled)', f'{result["title"]!r} after {result["polls"]} polls')
        result = self.desktop('wait --for title --regex (initial)', 'wait', '--for', 'title', '--window', primary,
                              '--regex', '--match', r'^KDE \w+ Native Fixture retitled$')['result']
        if result['polls'] != 1:
            raise SmokeFailure('title-gone', f'an initial match must return on the first poll: {result["polls"]}')
        error = self.expect_error('wait --for title --regex (runaway)', 'invalid_arguments', 'wait', '--for', 'title',
                                  '--window', primary, '--regex', '--match', '(.*.*)*!')
        if error['context'].get('reason') != 'pattern_too_slow':
            raise SmokeFailure('title-gone', f'expected pattern_too_slow: {error}')
        ok('wait --for title --regex (runaway)', 'pattern_too_slow; helper killed by its CPU timer')
        step = 'wait --for title --regex (raw)'
        payload = self.raw_request(step, 'wait', {'condition': 'title', 'window': primary,
                                                  'match': 'unclosed(group', 'regex': True})
        context = {} if payload['ok'] else payload['error']['context']
        if (payload['ok'] or payload['error']['code'] != 'invalid_arguments' or context.get('reason') != 'invalid_regex'
                or context.get('phase') != 'title_wait' or 'unclosed(group' in json.dumps(payload)):
            raise SmokeFailure('title-gone', f'expected invalid_regex from the helper: {json.dumps(payload)[:300]}')
        ok(step, 'invalid_regex from the helper child; the worker never compiled it')
        payload = self.wait_with_step('wait --for title (window lost)', fixture_pid, 'wait', '--for', 'title',
                                      '--window', sibling, '--match', 'never this title')
        if payload['ok'] or payload['error']['code'] != 'target_lost':
            raise SmokeFailure('title-gone', f'expected target_lost: {json.dumps(payload)[:300]}')
        ok('wait --for title (window lost)', f'target_lost in phase {payload["error"]["context"].get("phase")}')
        payload = self.wait_with_step('wait --for gone (dialog)', fixture_pid, 'wait', '--for', 'gone',
                                      '--window', dialog)
        result = payload['result'] or {}
        if not payload['ok'] or result['already_gone'] or result['last_seen']['window']['ref'] != dialog:
            raise SmokeFailure('title-gone', f'unexpected gone result {json.dumps(payload)[:300]}')
        rows = self.desktop('windows (app still running)', 'windows', '--app', app)['result']['windows']
        if [row['window']['ref'] for row in rows] != [primary]:
            raise SmokeFailure('title-gone', f'expected only the primary window: {[r["title"] for r in rows]}')
        ok('wait --for gone (dialog)', f'after {result["polls"]} polls; primary still listed')
        steps = log_events(Path(logs['stdout']), {'signal_step'})
        if [(e.get('action'), e.get('applied')) for e in steps] != [('retitle', True), ('close', True), ('close', True)]:
            raise SmokeFailure('title-gone', f'fixture signal steps: {steps}')
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
             'scroll-interrupted': 'scroll_interrupted',
             'generation': 'generation_refusal', 'compositor-death': 'compositor_death', 'bus-death': 'bus_death',
             'worker-sigkill': 'worker_sigkill', 'worker-stopped': 'worker_stopped', 'title-gone': 'title_gone_waits'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scenarios', nargs='*', metavar='SCENARIO', help='default: all of ' + ', '.join(SCENARIOS))
    parser.add_argument('--cli', default=shutil.which('agent-desktop'))
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=int, default=1, metavar='N')
    parser.add_argument('--keep-artifacts', action='store_true', help='keep artifacts after passing scenarios')
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
