#!/usr/bin/env python3
"""End-to-end smoke test for an installed agent-desktop.

Drives the real CLI through separate invocations against the native Wayland
fixture (exact key acknowledgements) and gnome-text-editor (a real GTK app):

    doctor, session start, launch, windows, focus, key, type, click, move,
    scroll, drag, modifier click/scroll/drag, screenshot, wait, close, kill,
    session stop, input while KWin's window menu is open, and non-ASCII type
    through the input method into the fixture's text-input field

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
# After the clicks: move --x 200 --y 100; scroll --x 210 --y 110 --dx -1 --dy 3;
# a screen-coordinate scroll --dy -2 at client (50, 60). Motion is ('motion', x, y)
# and each wheel frame is ('wheel', x, y, [(axis, value120), ...]) with axis 0
# vertical, 1 horizontal and positive meaning down/right (KWin sends horizontal first).
EXPECTED_POINTER = ([('motion', 200, 100), ('motion', 210, 110), ('wheel', 210, 110, [(1, -120), (0, 120)])]
                    + [('wheel', 210, 110, [(0, 120)])] * 2
                    + [('motion', 50, 60)] + [('wheel', 50, 60, [(0, -120)])] * 2)
# A second fixture: drag --from 100,50 --to 300,150 --duration 200 --modifiers ctrl,shift
# (20 motions of (10, 5)); click --x 10 --y 20 --count 2 --modifiers alt; scroll --x 10
# --y 20 --dy 2 --modifiers ctrl (the pointer is already there, so no motion); then
# drag --from 400,300 --to 380,290 --button middle --duration 0 (one motion). Rows are
# ('key', code, state), ('button', code, state, x, y, mask), ('motion', x, y, mask) and
# ('wheel', x, y, value120s, mask); MASK is the XKB modifier mask in effect (Shift 1,
# Ctrl 4, Alt 8), so every pointer event proves which modifiers it arrived under.
DRAG_STEPS, DRAG_MS = 20, 200
EXPECTED_LAYERED = ([('motion', 100, 50, 0), ('key', 29, 1), ('key', 42, 1), ('button', 272, 1, 100, 50, 5)]
                    + [('motion', 100 + 10 * i, 50 + 5 * i, 5) for i in range(1, DRAG_STEPS + 1)]
                    + [('button', 272, 0, 300, 150, 5), ('key', 42, 0), ('key', 29, 0)]
                    + [('motion', 10, 20, 0), ('key', 56, 1)]
                    + [('button', 272, 1, 10, 20, 8), ('button', 272, 0, 10, 20, 8)] * 2 + [('key', 56, 0)]
                    + [('key', 29, 1)] + [('wheel', 10, 20, [120], 4)] * 2 + [('key', 29, 0)]
                    + [('motion', 400, 300, 0), ('button', 274, 1, 400, 300, 0), ('motion', 380, 290, 0),
                       ('button', 274, 0, 380, 290, 0)])
# Non-ASCII type: a precomposed é, a symbol, CJK, an emoji and e + U+0301 (combining acute).
UNICODE_TEXT = 'héllo ✓ 中文 🎉 e\u0301'
# gnome-text-editor's "New Tab" header-bar button, in client coordinates.
EDITOR_NEW_TAB = (107, 23)
# Empty header-bar space; a right-click there opens KWin's window menu.
EDITOR_HEADER_GAP = (350, 23)
# Window-screenshot rows below the header bar, and columns left of the overlay scrollbar.
EDITOR_TEXT_TOP, EDITOR_SCROLLBAR = 48, 30


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
        self.launched = 0
        self.private_logs = set()  # Application logs that may hold typed text; nothing else may.

    def run_cli(self, step, *args, timeout=40, expect_ok=True, quiet=False):
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
        if not quiet:
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
        missing = {'launch', 'windows', 'focus', 'key', 'type', 'click', 'move', 'scroll', 'drag', 'screenshot',
                   'close', 'kill'} - supported
        if missing:
            raise SmokeFailure('session start', f'operations not supported: {sorted(missing)}')

    def launch(self, label, *argv, windows=1):
        payload = self.desktop(f'{label}: launch', 'launch', '--wait-window', '--', *argv)
        result = payload['result']
        self.launched += 1
        self.pids.append(result['process']['pid'])
        app = result['application']['ref']
        expected, windows = windows, []
        deadline = time.monotonic() + 5
        while len(windows) < expected and time.monotonic() < deadline:
            windows = self.desktop(f'{label}: windows', 'windows', '--app', app)['result']['windows']
        if len(windows) != expected:
            raise SmokeFailure(f'{label}: windows', f'expected {expected} window(s), saw {len(windows)}')
        if expected > 1:  # The largest window is the primary one.
            windows.sort(key=lambda row: -row['client']['width'])
        self.windows = windows
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
        typed = self.desktop("fixture: type 'aB!'", 'type', '--window', window, 'aB!')['result']
        if typed.get('method') != 'keys':
            raise SmokeFailure("fixture: type 'aB!'", f'expected method keys, saw {typed.get("method")}')
        self.desktop('fixture: click 100,50', 'click', '--window', window, '--x', '100', '--y', '50')
        self.desktop('fixture: double right-click', 'click', '--window', window, '--x', '20', '--y', '30',
                     '--button', 'right', '--count', '2')
        moved = self.desktop('fixture: move 200,100', 'move', '--window', window, '--x', '200', '--y', '100')['result']
        client = moved['client']
        if (moved['screen_x'], moved['screen_y']) != (client['x'] + 200, client['y'] + 100):
            raise SmokeFailure('fixture: move 200,100', f'unexpected result {json.dumps(moved)[:300]}')
        scrolled = self.desktop('fixture: scroll --dx -1 --dy 3', 'scroll', '--window', window, '--x', '210', '--y', '110',
                                '--dx', '-1', '--dy', '3')['result']
        if (scrolled['dx'], scrolled['dy'], scrolled['steps']) != (-1, 3, 3):
            raise SmokeFailure('fixture: scroll', f'unexpected result {json.dumps(scrolled)[:300]}')
        self.desktop('fixture: screen scroll --dy -2', 'scroll', '--x', str(client['x'] + 50),
                     '--y', str(client['y'] + 60), '--dy', '-2')
        wait_for_keys(Path(logs['stdout']), len(EXPECTED_KEYS))
        wait_for_keys(Path(logs['stdout']), sum(len(row[3]) for row in EXPECTED_POINTER if row[0] == 'wheel'),
                      kinds={'axis_value120'})
        self.screenshot('fixture', '--window', window)
        result = self.desktop('fixture: close', 'close', '--app', app)['result']
        if result['exited'] is not True:
            raise SmokeFailure('fixture: close', 'application did not exit')
        # The fixture has exited, so this is its complete log: no late extras.
        hold = check_key_log(Path(logs['stdout']))
        print(f'  ok  {"fixture: key acknowledgements":<34}       order, modifiers, text, hold {hold}ms')
        check_button_log(Path(logs['stdout']))
        print(f'  ok  {"fixture: button acknowledgements":<34}       position, button, count')
        check_pointer_log(Path(logs['stdout']))
        print(f'  ok  {"fixture: move/scroll acknowledgements":<34}       motion, wheel steps, signs, order')
        self.check_logs(app, logs)
        self.layered_flow()
        self.unicode_flow()

    def unicode_flow(self):
        """Non-ASCII type is one input-method commit: the field holds exactly the text, confirmed, no keys."""
        health = self.desktop('session status: input_method', 'session status')['result']['health']
        if health.get('input_method', {}).get('state') != 'passed':
            raise SmokeFailure('session status: input_method', json.dumps(health.get('input_method')))
        app, window, logs = self.launch('fixture 3', str(self.fixture), '--autonomous', '--text-input',
                                        '--exit-after-ms', '60000')
        self.private_logs.update(str(Path(path).resolve()) for path in logs.values())
        self.desktop('fixture 3: focus', 'focus', '--window', window)
        step = 'fixture 3: type (input method)'
        result = self.desktop(step, 'type', '--window', window, UNICODE_TEXT)['result']
        expected = {'method': 'input_method', 'confirmed': True, 'confirmation_reason': None,
                    'characters': len(UNICODE_TEXT), 'bytes': len(UNICODE_TEXT.encode())}
        if {key: result.get(key) for key in expected} != expected:
            raise SmokeFailure(step, f'unexpected result {json.dumps(result)[:400]}')
        wait_for_keys(Path(logs['stdout']), 1, kinds={'text_input_commit_string'})
        result = self.desktop('fixture 3: close', 'close', '--app', app)['result']
        if result['exited'] is not True:
            raise SmokeFailure('fixture 3: close', 'application did not exit')
        commits = [row['text'] for row in log_events(Path(logs['stdout']), {'text_input_commit_string'})]
        applied = [row['text'] for row in log_events(Path(logs['stdout']), {'text_input_done'}) if row['applied']]
        keys = log_events(Path(logs['stdout']), {'key'})
        if commits != [UNICODE_TEXT] or applied != [UNICODE_TEXT] or keys:
            raise SmokeFailure('fixture 3: commit receipts', f'commits {commits!r}, field {applied!r}, '
                               f'{len(keys)} key events')
        print(f'  ok  {"fixture 3: commit receipts":<34}       one exact commit, no key events')

    def layered_flow(self):
        """drag, and click and scroll with --modifiers, on a fresh fixture: exact receipts in order."""
        app, window, logs = self.launch('fixture 2', str(self.fixture), '--autonomous', '--exit-after-ms', '60000')
        self.desktop('fixture 2: focus', 'focus', '--window', window)
        dragged = self.desktop('fixture 2: drag --modifiers ctrl,shift', 'drag', '--window', window, '--from', '100,50',
                               '--to', '300,150', '--duration', str(DRAG_MS), '--modifiers', 'ctrl,shift')['result']
        client = dragged['client']
        if ((dragged['steps'], dragged['modifiers'], dragged['screen_x'], dragged['screen_y'])
                != (DRAG_STEPS, ['ctrl', 'shift'], client['x'] + 300, client['y'] + 150)):
            raise SmokeFailure('fixture 2: drag', f'unexpected result {json.dumps(dragged)[:400]}')
        self.desktop('fixture 2: click --count 2 --modifiers alt', 'click', '--window', window, '--x', '10', '--y', '20',
                     '--count', '2', '--modifiers', 'alt')
        self.desktop('fixture 2: scroll --dy 2 --modifiers ctrl', 'scroll', '--window', window, '--x', '10', '--y', '20',
                     '--dy', '2', '--modifiers', 'ctrl')
        self.desktop('fixture 2: drag --button middle --duration 0', 'drag', '--window', window, '--from', '400,300',
                     '--to', '380,290', '--button', 'middle', '--duration', '0')
        wait_for_keys(Path(logs['stdout']), 4, kinds={'button'})
        result = self.desktop('fixture 2: close', 'close', '--app', app)['result']
        if result['exited'] is not True:
            raise SmokeFailure('fixture 2: close', 'application did not exit')
        took = check_layered_log(Path(logs['stdout']))
        print(f'  ok  {"fixture 2: drag/modifier receipts":<34}       order, positions, modifier masks; '
              f'{DRAG_STEPS} motions in {took}ms')

    def check_logs(self, app, launched):
        """`logs` finds the exited fixture's complete logs at the paths launch returned."""
        entries = self.desktop('fixture: logs --tail 3', 'logs', '--app', app, '--tail', '3')['result']['logs']
        streams = {entry['stream']: entry for entry in entries}
        stdout = streams.get('stdout', {})
        if (set(streams) != {'stdout', 'stderr'} or any(streams[s]['path'] != launched[s] for s in streams)
                or not all(entry['complete'] for entry in entries) or len(stdout.get('tail', [])) != 3
                or any(path.endswith('.partial') for path in launched.values())):
            raise SmokeFailure('fixture: logs', json.dumps(entries)[:600])
        content = Path(launched['stdout']).read_text()
        if stdout['tail'] != content.splitlines()[-3:] or stdout['bytes'] != len(content.encode()):
            raise SmokeFailure('fixture: logs', f'tail does not match the log\'s last lines: {stdout["tail"]}')
        session = self.desktop('logs --source compositor', 'logs', '--source', 'compositor', '--tail', '0')
        if [entry['source'] for entry in session['result']['logs']] != ['compositor']:
            raise SmokeFailure('logs', json.dumps(session['result'])[:300])
        print(f'  ok  {"fixture: log artifacts":<34}       final names, complete, tail matches')

    def editor_flow(self):
        app, window, _ = self.launch('editor', self.editor)
        self.desktop('editor: focus', 'focus', '--window', window)
        self.desktop('editor: wait --for focus', 'wait', '--for', 'focus', '--window', window)
        text = 'agent desktop smoke ' + uuid.uuid4().hex[:6]
        self.desktop('editor: type', 'type', '--window', window, text)
        self.wait_title('editor: wait --for title (typed)', window, text)
        x, y = EDITOR_NEW_TAB
        self.desktop('editor: click New Tab', 'click', '--window', window, '--x', str(x), '--y', str(y))
        # The new empty document becomes current: same window, "New Document" title.
        title = self.wait_title('editor: wait --for title (new tab)', window, '^New Document', '--regex')
        if text in title:
            raise SmokeFailure('editor: new tab check', f'title still shows the typed text: {title}')
        self.window_menu(window)
        # Switching back to the first tab must show the typed document again,
        # which also proves input works once the window menu is dismissed.
        self.desktop('editor: key ctrl+page_up', 'key', '--window', window, 'ctrl+page_up')
        self.wait_title('editor: wait --for title (first tab)', window, text)
        self.file_dialog(app, window)
        self.editor_scroll(window)
        self.desktop('editor: key ctrl+a', 'key', '--window', window, 'ctrl+a')
        self.screenshot('editor')
        self.screenshot('editor', '--window', window)
        result = self.desktop('editor: kill', 'kill', '--app', app)['result']
        if result.get('exited') is not True:
            raise SmokeFailure('editor: kill', f'application did not exit: {json.dumps(result)[:300]}')

    def editor_scroll(self, window):
        """The wheel scrolls a real GTK text view: down moves the text up, and up brings it back.

        The document gets a marker line 12 lines below the first and enough
        blank lines to overflow. Ink rows (at least 3 pixels unlike the
        background) ignore the 1px caret, and the overlay scrollbar is cropped.
        """
        self.desktop('editor: type overflow lines', 'type', '--window', window,
                     '\n' * 12 + 'smoke scroll marker' + '\n' * 30)
        self.desktop('editor: key ctrl+home', 'key', '--window', window, 'ctrl+home')
        top, path = self.text_bands('editor: screenshot (top)', window, lambda bands: True)
        if not top:
            raise SmokeFailure('editor: scroll', f'no text visible at the top: {path}')
        width, height = png_size(path)
        x, y = str(width // 2), str((EDITOR_TEXT_TOP + height) // 2)
        self.desktop('editor: scroll --dy 3', 'scroll', '--window', window, '--x', x, '--y', y, '--dy', '3')
        # Down moves every line up: the last band rises or leaves the view, and nothing new appears.
        down, path = self.text_bands('editor: screenshot (scrolled down)', window,
                                     lambda bands: bands != top and (not bands or bands[-1] < top[-1] - 10))
        self.desktop('editor: scroll --dy -3', 'scroll', '--window', window, '--x', x, '--y', y, '--dy', '-3')
        back, path = self.text_bands('editor: screenshot (scrolled up)', window,
                                     lambda bands: len(bands) == len(top)
                                     and all(abs(a - b) <= 3 for a, b in zip(bands, top)))
        print(f'  ok  {"editor: scroll":<34}       text bands {top} -> {down} -> {back}')

    def text_bands(self, step, window, done, timeout=3):
        """Poll window screenshots until the text bands (tops of ink-row runs) satisfy DONE twice in a row."""
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            result = self.desktop(step, 'screenshot', '--window', window, quiet=True)['result']
            path = Path(result['path'])
            bands = ink_bands(path)
            if done(bands) and bands == previous:
                self.screenshots.append(path)
                print(f'  ok  {step:<34}       bands {bands}')
                return bands, path
            if time.monotonic() >= deadline:
                raise SmokeFailure(step, f'text bands {bands} (previous {previous}); see {path}')
            previous = bands
            time.sleep(.05)

    def wait_title(self, step, window, match, *flags):
        result = self.desktop(step, 'wait', '--for', 'title', '--window', window, '--match', match, *flags,
                              '--timeout', '5')['result']
        if result['window']['ref'] != window or result['row']['title'] != result['title']:
            raise SmokeFailure(step, f'unexpected result {json.dumps(result)[:300]}')
        return result['title']

    def file_dialog(self, app, window):
        """A modal dialog closes while the editor keeps running: `wait --for gone` confirms it."""
        self.desktop('editor: key ctrl+o', 'key', '--window', window, 'ctrl+o')
        deadline = time.monotonic() + 5
        while True:
            rows = self.desktop('editor: windows (dialog open)', 'windows', '--app', app, quiet=True)['result']['windows']
            dialogs = [row for row in rows if row['kind'] == 'window' and row['window']['ref'] != window]
            if dialogs:
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('editor: dialog', f'no dialog window: {[r["title"] for r in rows]}')
            time.sleep(.1)
        dialog = dialogs[0]['window']['ref']
        print(f'  ok  {"editor: windows (dialog open)":<34}       {dialogs[0]["title"]!r}')
        self.desktop('editor: focus dialog', 'focus', '--window', dialog)
        self.desktop('editor: key escape (dialog)', 'key', '--window', dialog, 'escape')
        result = self.desktop('editor: wait --for gone (dialog)', 'wait', '--for', 'gone', '--window', dialog,
                              '--timeout', '5')['result']
        rows = self.desktop('editor: windows (dialog closed)', 'windows', '--app', app)['result']['windows']
        if result['window']['ref'] != dialog or [r['window']['ref'] for r in rows if r['kind'] == 'window'] != [window]:
            raise SmokeFailure('editor: dialog closed', f'{json.dumps(result)[:200]}; rows {[r["title"] for r in rows]}')
        self.desktop('editor: focus', 'focus', '--window', window)

    def compositor_rows(self, step, *, present, timeout=3):
        """Poll the full `windows` (it must keep succeeding) until a compositor row is (not) listed."""
        deadline = time.monotonic() + timeout
        while True:
            rows = self.desktop(step, 'windows', quiet=True)['result']['windows']
            menus = [row for row in rows if row['kind'] == 'compositor']
            if bool(menus) == present:
                print(f'  ok  {step:<34}       {len(menus)} compositor row(s)')
                return rows, menus
            if time.monotonic() >= deadline:
                raise SmokeFailure(step, f'kinds {[row["kind"] for row in rows]}')
            time.sleep(.1)

    def window_menu(self, window):
        """KWin's window menu is listed, blocks window input, and a screen click dismisses it."""
        x, y = EDITOR_HEADER_GAP
        self.desktop('editor: right-click header bar', 'click', '--window', window, '--button', 'right',
                     '--x', str(x), '--y', str(y))
        rows, menus = self.compositor_rows('editor: windows (menu open)', present=True)
        menu = menus[0]
        if menu['pid'] is not None or menu['app'] is not None or not menu['client']:
            raise SmokeFailure('editor: menu row', f'unexpected compositor row {json.dumps(menu)[:300]}')
        editor = next(row for row in rows if row['window']['ref'] == window)
        if not editor['active'] or editor['kind'] != 'window':
            raise SmokeFailure('editor: menu row', 'the editor should stay the active window')
        # The menu has keyboard accelerators ("c" is Close), so input must not be sent.
        error = self.desktop('editor: key while menu open', 'key', '--window', window, 'escape',
                             expect_ok=False)['error']
        if (error is None or error['code'] != 'target_lost' or error['outcome'] != 'not_started'
                or error['context'].get('reason') != 'compositor_surface_open'
                or error['context'].get('blocking_windows') != [menu['window']]):
            raise SmokeFailure('editor: key while menu open', f'expected compositor_surface_open, got {error}')
        # Dismiss with a screen click outside the menu and the editor; the menu consumes it.
        rects = [r['frame'] or r['client'] for r in rows if r['frame'] or r['client']]
        point = next(p for p in ((5, 715), (1275, 715), (5, 5), (1275, 5))
                     if not any(b['x'] <= p[0] < b['x'] + b['width'] and b['y'] <= p[1] < b['y'] + b['height']
                                for b in rects))
        self.desktop('editor: click outside the menu', 'click', '--x', str(point[0]), '--y', str(point[1]))
        self.compositor_rows('editor: windows (menu closed)', present=False)

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
        components = {entry['component'] for entry in manifest['dependencies'].get('inventory', [])}
        if not {'kwin', 'libei', 'kdotool'} <= components or manifest['output']['state'] != 'collected':
            raise SmokeFailure('artifacts', f'manifest lacks versions or output: {sorted(components)}')
        applications = manifest.get('applications', [])
        if len(applications) != self.launched or manifest.get('failure') is not None:
            raise SmokeFailure('artifacts', f'manifest lists {len(applications)} of {self.launched} applications')
        if self.private_logs:
            # Typed text is never recorded: only the application's own logs may hold it.
            needles = [UNICODE_TEXT.encode(), json.dumps(UNICODE_TEXT).encode()[1:-1], '中文'.encode()]
            leaks = [str(path) for path in generation.rglob('*') if path.is_file()
                     and str(path.resolve()) not in self.private_logs
                     and any(needle in path.read_bytes() for needle in needles)]
            if leaks:
                raise SmokeFailure('artifacts', f'typed text recorded in {leaks}')
        print(f'  ok  {"artifacts":<34}       manifest (versions, {len(applications)} apps), shutdown record, '
              f'{len(self.screenshots)} screenshots')

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


def wait_for_keys(log, count, timeout=3, kinds=frozenset({'key'})):
    """Bounded wait until the fixture has logged at least COUNT key (or KINDS) events."""
    deadline = time.monotonic() + timeout
    while len(log_events(log, kinds)) < count and time.monotonic() < deadline:
        time.sleep(.05)


def check_button_log(log):
    rows = log_events(log, {'button'})
    observed = [(row['button'], row['state'], row['x'], row['y']) for row in rows]
    if observed != EXPECTED_BUTTONS or any(row['surface'] != 'primary' for row in rows):
        raise SmokeFailure('fixture: button acknowledgements', f'expected {EXPECTED_BUTTONS}, saw {observed}')


def check_pointer_log(log):
    """Motion and wheel receipts after the last button: exact positions, steps, signs and order."""
    step = 'fixture: move/scroll acknowledgements'
    rows = log_events(log, {'button', 'motion', 'axis', 'axis_value120', 'axis_discrete', 'axis_stop', 'pointer_frame'})
    buttons = [index for index, row in enumerate(rows) if row['event'] == 'button']
    observed, wheel, continuous = [], [], []
    for row in rows[buttons[-1] + 1 if buttons else 0:]:
        if row['surface'] != 'primary' or row['event'] in ('axis_stop', 'axis_discrete'):
            # axis_discrete is only sent below wl_seat v8; axis_stop is never expected
            # because the toolkit never sends a scroll stop, as a physical wheel would not.
            raise SmokeFailure(step, f'unexpected receipt {row}')
        if row['event'] == 'motion':
            observed.append(('motion', row['x'], row['y']))
        elif row['event'] == 'axis_value120':
            wheel.append((row['axis'], row['value120']))
        elif row['event'] == 'axis':
            continuous.append((row['axis'], row['value'] > 0))
        elif row['event'] == 'pointer_frame' and (wheel or continuous):
            if continuous != [(axis, value > 0) for axis, value in wheel]:
                raise SmokeFailure(step, f'axis values {continuous} do not match value120 {wheel}')
            observed.append(('wheel', row['x'], row['y'], wheel))
            wheel, continuous = [], []
    if observed != EXPECTED_POINTER or wheel or continuous:
        raise SmokeFailure(step, f'expected {EXPECTED_POINTER}, saw {observed} (unframed {wheel})')


def check_layered_log(log):
    """The second fixture's complete log against EXPECTED_LAYERED; returns the first drag's press-to-last-motion ms."""
    step = 'fixture 2: drag/modifier receipts'
    rows = log_events(log, {'key', 'modifiers', 'button', 'motion', 'axis_value120', 'pointer_frame', 'axis_stop'})
    observed, wheel, mask, times = [], [], 0, []
    for row in rows:
        if row['event'] != 'modifiers' and row['surface'] != 'primary' or row['event'] == 'axis_stop':
            raise SmokeFailure(step, f'unexpected receipt {row}')
        if row['event'] == 'modifiers':
            mask = row['depressed']
        elif row['event'] == 'key':
            observed.append(('key', row['key'], row['state']))
        elif row['event'] == 'button':
            observed.append(('button', row['button'], row['state'], row['x'], row['y'], mask))
            times.append(row['time_ms'])
        elif row['event'] == 'motion':
            observed.append(('motion', row['x'], row['y'], mask))
            times.append(row['time_ms'])
        elif row['event'] == 'axis_value120':
            wheel.append(row['value120'])
        elif row['event'] == 'pointer_frame' and wheel:
            observed.append(('wheel', row['x'], row['y'], wheel, mask))
            wheel = []
    if observed != EXPECTED_LAYERED or mask != 0:
        raise SmokeFailure(step, f'expected {EXPECTED_LAYERED}, saw {observed} (final modifiers {mask})')
    # times: the positioning motion, the press, DRAG_STEPS motions, the release, ...
    took = times[DRAG_STEPS + 1] - times[1]
    if not DRAG_MS - 10 <= took <= DRAG_MS + 400:
        raise SmokeFailure(step, f'press to last motion took {took}ms, expected about {DRAG_MS}ms')
    return took


def ink_bands(path):
    """Tops of runs of text rows (>= 3 pixels unlike the background) in an editor window screenshot."""
    from PIL import Image  # A prerequisite (python-pillow); only the editor check needs it.
    with Image.open(path) as image:
        gray = image.convert('L')
    area = gray.crop((0, EDITOR_TEXT_TOP, gray.width - EDITOR_SCROLLBAR, gray.height - 8))
    histogram = area.histogram()
    background = histogram.index(max(histogram))
    data = area.point(lambda value: 255 if abs(value - background) > 64 else 0).tobytes()
    rows = [y for y in range(area.height) if data[y * area.width:(y + 1) * area.width].count(255) >= 3]
    return [EDITOR_TEXT_TOP + y for index, y in enumerate(rows) if index == 0 or y - rows[index - 1] > 3]


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


def positive_int(text):
    """argparse type for --loop: zero or fewer runs would pass without testing anything."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{text!r} is not an integer') from None
    if value < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, not {value}')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--cli', default=shutil.which('agent-desktop'), help='installed agent-desktop executable')
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=positive_int, default=1, metavar='N', help='run N full cycles (default 1)')
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
