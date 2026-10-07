#!/usr/bin/env python3
"""Optional target-application tests for an installed agent-desktop.

Each scenario starts its own session, drives a real application through the CLI
the way an agent would, and checks the application's own state on disk, not
just that input was dispatched:

    text-editor  gnome-text-editor: type, save through the "Save a File" dialog
                 with keyboard shortcuts, compare the file; edit and save again
    gimp         GIMP 3: a new 320x240 image, one paintbrush click, export to PNG
                 through the export dialogs; the clicked pixel is dark, the rest white
    blender      Blender: --factory-startup on a prepared scene, duplicate the
                 cube from the keyboard, Save As through Blender's file view;
                 `blender -b` counts the objects in both files

    python tests/integration/apps.py [SCENARIO ...] [--loop N]

An application that isn't installed (or is too old) is skipped with the reason.
AGENT_DESKTOP_REQUIRE_HOST_TESTS=1 turns that skip into a failure. Each
application's settings, caches and recent files stay in the session's private
HOME/XDG directories plus a temporary work directory, which is removed after a
passing scenario. Not part of the unit-test suite; see docs/TESTING.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke  # noqa: E402
from smoke import ROOT, SmokeFailure, positive_int  # noqa: E402

# The same switch as tests/host_facilities.py: a missing application fails instead of skipping.
REQUIRE = 'AGENT_DESKTOP_REQUIRE_HOST_TESTS'
EDITOR = '/usr/bin/gnome-text-editor'
GIMP = '/usr/bin/gimp'
BLENDER = '/usr/bin/blender'
# Version probes and `blender -b` run outside the session, so they get no display at all.
PROBE_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}

# Typed into gnome-text-editor. No leading indentation (it auto-indents the next line)
# and only characters the private US layout can type.
EDITOR_TEXT = 'agent desktop save check {tag}\nbraces {{x}} [y] $HOME ~/p "q" \'s\'\ttab\nlast line'
EDITOR_MORE = '\nappended after the first save'

GIMP_SIZE = (320, 240)
GIMP_DOT = (80, 60)  # Image pixel to paint: off-centre, so a shifted or mirrored mapping fails.
# GIMP shows its welcome dialog on first run and after an upgrade, which it detects
# from config-version, whatever show-welcome-dialog says.
GIMPRC = '(config-version "{version}")\n(show-welcome-dialog no)\n(check-updates no)\n'

# Prints the scene's objects as JSON once the .blend given before it has loaded.
BLENDER_INSPECT = ('import bpy, json, sys; sys.stdout.write("OBJECTS " + json.dumps({"file": bpy.data.filepath, '
                   '"objects": sorted([o.name, o.type] for o in bpy.data.objects)}) + "\\n"); sys.stdout.flush()')
# Blender shows its quick-setup splash, even over a file given on the command line,
# until the user config has a userpref.blend; --factory-startup still ignores its contents.
BLENDER_PREPARE = ('import bpy, sys; bpy.ops.wm.save_userpref(); '
                   'bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])')
BLENDER_DEFAULT = [['Camera', 'CAMERA'], ['Cube', 'MESH'], ['Light', 'LIGHT']]
BLENDER_DUPLICATED = sorted(BLENDER_DEFAULT + [['Cube.001', 'MESH']])


class AppScenario(smoke.Smoke):
    def __init__(self, cli, dependency_root, artifacts, work, verbose):
        super().__init__(cli, dependency_root, artifacts, None, None, verbose)
        self.work = work

    # Helpers ---------------------------------------------------------------

    def rows(self, app):
        return self.desktop('windows', 'windows', '--app', app, quiet=True)['result']['windows']

    def wait_row(self, step, app, title, timeout=10):
        """Poll `windows --app` until a kind "window" row whose title matches TITLE (a regex) appears."""
        deadline = time.monotonic() + timeout
        while True:
            rows = self.rows(app)
            found = [row for row in rows if row['kind'] == 'window' and re.search(title, row['title'] or '')]
            if found:
                row = found[0]
                ok(step, f'{row["title"]!r} client {geometry(row["client"])} frame {geometry(row["frame"])}')
                return row
            if time.monotonic() >= deadline:
                raise SmokeFailure(step, f'no window titled /{title}/: {[r["title"] for r in rows]}')
            time.sleep(.1)

    def gone(self, step, window):
        self.desktop(step, 'wait', '--for', 'gone', '--window', window, '--timeout', '10')

    def title(self, step, window, match, *flags, timeout=10):
        return self.desktop(step, 'wait', '--for', 'title', '--window', window, '--match', match, *flags,
                            '--timeout', str(timeout))['result']['title']

    def key(self, label, window, chord):
        self.desktop(f'{label}: key {chord}', 'key', '--window', window, chord)

    def type(self, label, window, text, what):
        self.desktop(f'{label}: type {what}', 'type', '--window', window, text)

    def quit_app(self, label, app):
        result = self.desktop(f'{label}: wait --for exit', 'wait', '--for', 'exit', '--app', app,
                              '--timeout', '20')['result']
        if result.get('exited') is not True:
            raise SmokeFailure(f'{label}: exit', json.dumps(result)[:300])

    # Scenarios -------------------------------------------------------------

    def text_editor(self):
        """Type, save through the dialog by keyboard, compare the file, then edit and save in place."""
        label = 'editor'
        target = self.work / 'saved.txt'
        text = EDITOR_TEXT.format(tag=uuid.uuid4().hex[:6])
        app, window, _ = self.launch(label, EDITOR, cwd=self.work)
        self.desktop(f'{label}: wait --for focus', 'wait', '--for', 'focus', '--window', window)
        self.type(label, window, text, 'three lines')
        # The draft's title is the start of its first line (a few words): the text arrived.
        self.title(f'{label}: wait --for title (typed)', window, '^agent desktop save', '--regex')
        self.key(label, window, 'ctrl+s')
        dialog = self.wait_row(f'{label}: save dialog', app, '^Save a File$')['window']['ref']
        # The name field has focus with the stem selected; a full path replaces it.
        self.key(label, dialog, 'ctrl+a')
        self.type(label, dialog, str(target), 'the path')
        self.key(label, dialog, 'return')
        self.gone(f'{label}: wait --for gone (dialog)', dialog)
        self.title(f'{label}: wait --for title (saved)', window, '^saved\\.txt ', '--regex')
        # GtkSourceView ends the file with a newline it doesn't show.
        expect_file(f'{label}: saved file', target, (text + '\n').encode())
        # A second save goes straight to the same file: no dialog.
        self.focus_window(label, window)
        self.key(label, window, 'ctrl+end')
        self.type(label, window, EDITOR_MORE, 'one more line')
        self.key(label, window, 'ctrl+s')
        expect_file(f'{label}: saved again', target, (text + EDITOR_MORE + '\n').encode())
        if any(row['kind'] == 'window' and row['window']['ref'] != window for row in self.rows(app)):
            raise SmokeFailure(f'{label}: second save', 'a dialog opened for a file that already has a path')
        result = self.desktop(f'{label}: close', 'close', '--app', app)['result']
        if result.get('exited') is not True:
            raise SmokeFailure(f'{label}: close', f'application did not exit: {json.dumps(result)[:300]}')

    def gimp(self):
        """A new image, one paintbrush click, export to PNG; the clicked pixel changed and nothing else did."""
        label = 'gimp'
        profile, gimprc, target = self.work / 'gimp-profile', self.work / 'gimprc', self.work / 'export.png'
        # A user gimprc of our own: no first-run welcome dialog and no update check.
        # GIMP3_DIRECTORY is the whole profile (brushes, sessionrc, recent files).
        version = re.search(r'version (\d+\.\d+\.\d+)', version_output(GIMP))
        if version is None:
            raise SmokeFailure(f'{label}: version', 'gimp --version printed no x.y.z version')
        gimprc.write_text(GIMPRC.format(version=version.group(1)))
        app, window, _ = self.launch(label, GIMP, '-n', '-s', '-c', '-g', str(gimprc), cwd=self.work,
                                     env={'GIMP3_DIRECTORY': str(profile)}, timeout=60)
        self.desktop(f'{label}: wait --for focus', 'wait', '--for', 'focus', '--window', window)
        self.key(label, window, 'ctrl+n')
        dialog = self.wait_row(f'{label}: new image dialog', app, '^Create a New Image$')['window']['ref']
        # Width has focus. Tab moves to height; return commits it and presses OK.
        width, height = GIMP_SIZE
        self.key(label, dialog, 'ctrl+a')
        self.type(label, dialog, str(width), 'width')
        self.key(label, dialog, 'tab')
        self.key(label, dialog, 'ctrl+a')
        self.type(label, dialog, str(height), 'height')
        self.key(label, dialog, 'return')
        self.gone(f'{label}: wait --for gone (new image)', dialog)
        self.title(f'{label}: wait --for title (image)', window, f' {width}x{height} ')
        self.focus_window(label, window)
        canvas = self.gimp_canvas(label, window)
        scale = canvas[2] / width
        x, y = round(canvas[0] + GIMP_DOT[0] * scale), round(canvas[1] + GIMP_DOT[1] * scale)
        # The default tool is the paintbrush with black foreground: one click paints one dab.
        self.desktop(f'{label}: click image {GIMP_DOT}', 'click', '--window', window, '--x', str(x), '--y', str(y))
        self.title(f'{label}: wait --for title (dirty)', window, '^\\*', '--regex')
        self.key(label, window, 'ctrl+shift+e')
        export = self.wait_row(f'{label}: export dialog', app, '^Export Image$')['window']['ref']
        self.key(label, export, 'ctrl+a')
        self.type(label, export, str(target), 'the path')
        self.key(label, export, 'return')
        options = self.wait_row(f'{label}: PNG options dialog', app, '^Export Image as PNG$')['window']['ref']
        self.key(label, options, 'return')
        self.gone(f'{label}: wait --for gone (PNG options)', options)
        self.title(f'{label}: wait --for title (exported)', window, '(exported)')
        detail = check_dot(wait_png(f'{label}: exported file', target), GIMP_SIZE, GIMP_DOT)
        ok(f'{label}: exported pixels', detail)
        # Quit: the image is exported but unsaved, so GIMP asks; ctrl+d discards.
        self.focus_window(label, window)
        self.key(label, window, 'ctrl+q')
        ask = self.wait_row(f'{label}: quit dialog', app, '^Quit GIMP$')['window']['ref']
        self.key(label, ask, 'ctrl+d')
        self.quit_app(label, app)

    def gimp_canvas(self, label, window):
        """The image's [x, y, width, height] in client pixels, once two window screenshots agree."""
        deadline = time.monotonic() + 10
        previous = None
        while True:
            result = self.desktop(f'{label}: screenshot (canvas)', 'screenshot', '--window', window,
                                  quiet=True)['result']
            canvas = find_canvas(Path(result['path']), GIMP_SIZE)
            if canvas is not None and canvas == previous:
                self.screenshots.append(Path(result['path']))
                ok(f'{label}: image on screen', f'client {canvas}')
                return canvas
            if time.monotonic() >= deadline:
                raise SmokeFailure(f'{label}: canvas', f'no stable {GIMP_SIZE} image in {result["path"]}')
            previous = canvas
            time.sleep(.1)

    def blender(self):
        """Duplicate the default cube from the keyboard and Save As; both files are checked with blender -b."""
        label = 'blender'
        scene, edited = self.work / 'scene.blend', self.work / 'edited.blend'
        resources = self.work / 'blender-user'
        env = {'BLENDER_USER_RESOURCES': str(resources)}
        # A user preferences file and an opened scene skip both splash screens, and Save As
        # then starts in the scene's folder.
        blender_batch(self.work, resources, '--python-expr', BLENDER_PREPARE, '--', str(scene))
        if inspect_blend(self.work, resources, scene) != BLENDER_DEFAULT:
            raise SmokeFailure(f'{label}: prepare', 'the prepared scene is not the default scene')
        app, window, _ = self.launch(label, BLENDER, '--factory-startup', '--offline-mode', str(scene),
                                     cwd=self.work, env=env, timeout=60)
        self.desktop(f'{label}: wait --for focus', 'wait', '--for', 'focus', '--window', window)
        self.title(f'{label}: wait --for title (scene)', window, f'^scene \\[{re.escape(str(scene))}\\]', '--regex')
        client = self.rows(app)[0]['client']
        # Blender sends keys to the area under the pointer: park it in the 3D viewport.
        self.desktop(f'{label}: move into the viewport', 'move', '--window', window,
                     '--x', str(int(client['width'] * .3)), '--y', str(int(client['height'] * .6)))
        self.key(label, window, 'shift+d')    # Duplicate Objects: the selected cube, then a modal move
        self.key(label, window, 'return')     # confirm the move without moving
        self.title(f'{label}: wait --for title (modified)', window, '^\\* ', '--regex')
        self.key(label, window, 'ctrl+shift+s')
        view = self.wait_row(f'{label}: file view', app, '^Blender File View$')
        ref, size = view['window']['ref'], view['client']
        # The filename field is a text button along the bottom; a double click edits it.
        self.desktop(f'{label}: double-click filename', 'click', '--window', ref, '--count', '2',
                     '--x', str(int(size['width'] * .4)), '--y', str(int(size['height']) - 21))
        self.key(label, ref, 'ctrl+a')
        self.type(label, ref, edited.name, 'the name')
        self.key(label, ref, 'return')        # ends the edit
        # The return that runs Save As closes the file view, and the main window then
        # acts on it too, on whatever button is under the pointer (the timeline's
        # jump-to-end, here). Park the pointer over the file list, above the viewport.
        self.desktop(f'{label}: move over the file list', 'move', '--window', ref,
                     '--x', str(int(size['width'] * .55)), '--y', str(int(size['height'] * .5)))
        self.key(label, ref, 'return')        # runs Save As
        self.gone(f'{label}: wait --for gone (file view)', ref)
        self.title(f'{label}: wait --for title (saved)', window, f'^edited \\[{re.escape(str(edited))}\\]', '--regex')
        wait_file(f'{label}: saved file', edited)
        objects = inspect_blend(self.work, resources, edited)
        if objects != BLENDER_DUPLICATED:
            raise SmokeFailure(f'{label}: saved objects', f'expected {BLENDER_DUPLICATED}, saw {objects}')
        if inspect_blend(self.work, resources, scene) != BLENDER_DEFAULT:
            raise SmokeFailure(f'{label}: original scene', 'Save As changed the original file')
        ok(f'{label}: blender -b', f'{edited.name}: {[name for name, _ in objects]}; {scene.name} unchanged')
        # Saved, so a normal close exits without asking.
        result = self.desktop(f'{label}: close', 'close', '--app', app, '--timeout', '20')['result']
        if result.get('exited') is not True:
            raise SmokeFailure(f'{label}: close', f'application did not exit: {json.dumps(result)[:300]}')

    def focus_window(self, label, window):
        self.desktop(f'{label}: focus', 'focus', '--window', window)

    def launch(self, label, *argv, cwd=None, env=None, timeout=None):
        options = ['--cwd', str(cwd)] if cwd else []
        for key, value in (env or {}).items():
            options += ['--env', f'{key}={value}']
        if timeout:
            options += ['--timeout', str(timeout)]
        started = time.monotonic()
        payload = self.desktop(f'{label}: launch', 'launch', '--wait-window', *options, '--', *argv,
                               timeout=(timeout or 10) + 30)
        result = payload['result']
        self.launched += 1
        self.pids.append(result['process']['pid'])
        windows = [row for row in result['window_wait']['windows'] if row['kind'] == 'window']
        if len(windows) != 1:
            raise SmokeFailure(f'{label}: launch', f'expected one window, saw {[r["title"] for r in windows]}')
        ok(f'{label}: first window', f'{time.monotonic() - started:.1f}s, {windows[0]["title"]!r}')
        return result['application']['ref'], windows[0]['window']['ref'], result['logs']

    # Driver ----------------------------------------------------------------

    def run_scenario(self, name):
        started = time.monotonic()
        failure = None
        try:
            self.start()
            getattr(self, SCENARIOS[name][0])()
        except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as error:
            failure = error
        finally:
            if self.start_attempted:
                for stage in (self.stop, self.check_leaks, self.check_artifacts):
                    if stage != self.stop and self.generation is None:
                        failure = failure or SmokeFailure('cleanup', 'generation unknown; cannot verify cleanup')
                        break
                    try:
                        stage()
                    except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                        failure = failure or error
        if failure is not None:
            raise failure
        return time.monotonic() - started


# Checks that need no desktop (unit-tested in tests/test_app_checks.py) -------------


def geometry(rect):
    return None if rect is None else f'{rect["width"]}x{rect["height"]}+{rect["x"]}+{rect["y"]}'


def ok(step, detail):
    print(f'  ok  {step:<34}       {detail}')


def find_canvas(path, size):
    """[x, y, width, height] of the blank white image in a GIMP window screenshot, or None.

    At 25% zoom or more, the image is the only white area at least a quarter of its
    width wide (brush previews are smaller): rows with such a run of pure white bound
    it. GIMP draws the layer boundary over the image's edge pixels, so the white run
    is one pixel short of the image on each side.
    """
    from PIL import Image  # A prerequisite (python-pillow); only these checks need it.
    with Image.open(path) as image:
        rgb = image.convert('RGB')
    data, width = rgb.tobytes(), rgb.width
    white = b'\xff\xff\xff'
    runs = []
    for y in range(rgb.height):
        row = data[y * width * 3:(y + 1) * width * 3]
        best, start, x = (0, 0), None, 0
        for x in range(width + 1):
            if x < width and row[x * 3:x * 3 + 3] == white:
                start = x if start is None else start
            elif start is not None:
                best, start = max(best, (x - start, start)), None
        if best[0] >= size[0] // 4:
            runs.append((y, best[1], best[0]))
    if not runs:
        return None
    top, bottom = runs[0][0], runs[-1][0]
    left, length = runs[0][1], runs[0][2]
    if (bottom - top + 1 != len(runs) or any((start, run) != (left, length) for _, start, run in runs)):
        return None  # Not one solid rectangle (still drawing, or covered).
    canvas = [left - 1, top - 1, length + 2, bottom - top + 3]
    if abs(canvas[2] * size[1] - canvas[3] * size[0]) > max(size):
        return None  # Not the image's aspect ratio.
    return canvas


# Half the side of the square around the clicked pixel that may differ from white:
# the default 51px brush's soft edge (radius about 26) plus antialiasing.
DAB_REACH = 34


def check_dot(path, size, dot):
    """The exported image is SIZE, dark at DOT and centred on it, and white (>= 250) everywhere else.

    Every pixel more than DAB_REACH from DOT in x or y must be white, so paint
    anywhere else fails, however light.
    """
    from PIL import Image
    with Image.open(path) as image:
        if image.size != size:
            raise SmokeFailure('gimp: exported pixels', f'image is {image.size}, expected {size}')
        gray = image.convert('L')
    pixels = gray.load()
    width, height = size
    dark = [(x, y) for y in range(height) for x in range(width) if pixels[x, y] < 128]
    if pixels[dot] >= 64 or not dark:
        raise SmokeFailure('gimp: exported pixels', f'pixel {dot} is {pixels[dot]}, not painted')
    stray = [(x, y) for y in range(height) for x in range(width)
             if pixels[x, y] < 250 and (abs(x - dot[0]) > DAB_REACH or abs(y - dot[1]) > DAB_REACH)]
    if stray:
        raise SmokeFailure('gimp: exported pixels', f'{len(stray)} non-white pixels more than {DAB_REACH}px from '
                                                    f'{dot}, first {stray[0]} = {pixels[stray[0]]}')
    xs, ys = [x for x, _ in dark], [y for _, y in dark]
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    extent = (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    if abs(centre[0] - dot[0]) > 3 or abs(centre[1] - dot[1]) > 3:
        raise SmokeFailure('gimp: exported pixels', f'dark area centred at {centre}, {extent}, expected at {dot}')
    painted = [(x, y) for y in range(height) for x in range(width) if pixels[x, y] < 250]
    reach = max(max(abs(x - dot[0]), abs(y - dot[1])) for x, y in painted)
    return (f'{size[0]}x{size[1]}, pixel {dot} = {pixels[dot]}, dark core {extent[0]}x{extent[1]} centred at '
            f'{centre}, paint within {reach}px, white beyond {DAB_REACH}px')


def wait_file(step, path, timeout=10):
    deadline = time.monotonic() + timeout
    while not (path.is_file() and path.stat().st_size):
        if time.monotonic() >= deadline:
            raise SmokeFailure(step, f'{path} was not written')
        time.sleep(.1)
    return path


def wait_png(step, path, timeout=10):
    """Wait until PATH is a complete PNG (ends with its IEND chunk)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if path.read_bytes().endswith(b'IEND\xaeB`\x82'):
                return path
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise SmokeFailure(step, f'{path} is not a complete PNG')
        time.sleep(.1)


def expect_file(step, path, content, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            seen = path.read_bytes()
        except OSError as error:
            seen = error
        if seen == content:
            ok(step, f'{len(content)} bytes match')
            return
        if time.monotonic() >= deadline:
            raise SmokeFailure(step, f'expected {content!r}, found {seen!r}')
        time.sleep(.1)


def probe_env(work, resources=None):
    """Outside the session: no display, and HOME/XDG in the work directory."""
    env = PROBE_ENV | {'HOME': str(work), 'XDG_CONFIG_HOME': str(work / '.config'),
                       'XDG_CACHE_HOME': str(work / '.cache'), 'XDG_DATA_HOME': str(work / '.local' / 'share')}
    if resources is not None:
        env['BLENDER_USER_RESOURCES'] = str(resources)
    return env


def blender_batch(work, resources, *args):
    argv = [BLENDER, '-b', '--factory-startup', '--offline-mode', '--python-exit-code', '1', *args]
    result = subprocess.run(argv, env=probe_env(work, resources), cwd=work, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise SmokeFailure('blender -b', f'exit {result.returncode}: {(result.stdout + result.stderr)[-600:]}')
    return result.stdout


def inspect_blend(work, resources, path):
    """[[name, type], ...] of the objects in PATH, read by `blender -b`."""
    output = blender_batch(work, resources, str(path), '--python-expr', BLENDER_INSPECT)
    return parse_objects(output, path)


def parse_objects(output, path):
    lines = [line for line in output.splitlines() if line.startswith('OBJECTS ')]
    if len(lines) != 1:
        raise SmokeFailure('blender -b', f'no object listing for {path}: {output[-400:]}')
    found = json.loads(lines[0][len('OBJECTS '):])
    if found['file'] != str(path):
        raise SmokeFailure('blender -b', f'loaded {found["file"]!r}, not {path}')  # A missing file loads nothing.
    return found['objects']


def version_output(executable):
    """`EXECUTABLE --version` run outside the session (no display), or an OSError/TimeoutExpired."""
    with tempfile.TemporaryDirectory(prefix='agent-desktop-probe-') as raw:
        return subprocess.run([executable, '--version'], env=probe_env(Path(raw)), cwd=raw,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30).stdout


def availability(name):
    """None when the scenario's application can run here, otherwise why it can't."""
    executable = {'text-editor': EDITOR, 'gimp': GIMP, 'blender': BLENDER}[name]
    if not (os.path.isfile(executable) and os.access(executable, os.X_OK)):
        return f'{executable} is not installed'
    if name == 'text-editor':
        return None
    try:
        return version_problem(name, version_output(executable))
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'{executable} --version failed: {error}'


def version_problem(name, output):
    pattern, minimum = {'gimp': (r'version (\d+)\.(\d+)', (3, 0)), 'blender': (r'^Blender (\d+)\.(\d+)', (4, 2))}[name]
    match = re.search(pattern, output, re.MULTILINE)
    if match is None:
        return f'{name} --version printed no version: {output.strip()[:200]!r}'
    found = tuple(int(part) for part in match.groups())
    if found < minimum:
        return f'{name} {found[0]}.{found[1]} is older than the {minimum[0]}.{minimum[1]} this scenario needs'
    return None


SCENARIOS = {'text-editor': ('text_editor', 'gnome-text-editor'), 'gimp': ('gimp', 'GIMP 3'),
             'blender': ('blender', 'Blender')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scenarios', nargs='*', metavar='SCENARIO', help='default: all of ' + ', '.join(SCENARIOS))
    parser.add_argument('--cli', default=shutil.which('agent-desktop'))
    parser.add_argument('--dependency-root', default=str(ROOT / '.local' / 'dependencies'))
    parser.add_argument('--loop', type=positive_int, default=1, metavar='N', help='run N cycles (default 1)')
    parser.add_argument('--keep-artifacts', action='store_true', help='keep artifacts and work directories after passing')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    if not args.cli:
        parser.error('agent-desktop is not on PATH; install it (pip install .) or pass --cli')
    unknown = set(args.scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f'unknown scenario(s) {sorted(unknown)}; choose from {list(SCENARIOS)}')
    names, skipped = [], []
    for name in args.scenarios or list(SCENARIOS):
        reason = availability(name)
        if reason is None:
            names.append(name)
        elif os.environ.get(REQUIRE) == '1':
            print(f'FAIL {name}: {reason} ({REQUIRE}=1 forbids skipping)')
            return 1
        else:
            print(f'skip {name}: {reason}')
            skipped.append(name)
    results = []
    for iteration in range(1, args.loop + 1):
        for name in names:
            artifacts = Path(tempfile.mkdtemp(prefix=f'agent-desktop-{name}-'))
            work = Path(tempfile.mkdtemp(prefix=f'agent-desktop-{name}-work-'))
            artifacts.chmod(0o700)
            work.chmod(0o700)
            print(f'{name} ({iteration}/{args.loop})')
            try:
                elapsed = AppScenario(args.cli, args.dependency_root, artifacts, work, args.verbose).run_scenario(name)
            except (SmokeFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as error:
                print(f'FAIL {name}: {error}')
                payload = getattr(error, 'payload', None)
                if payload is not None:
                    print(json.dumps(payload, indent=2)[:3000])
                print(f'artifacts kept at {artifacts}, work directory at {work}')
                return 1
            if args.keep_artifacts:
                print(f'artifacts kept at {artifacts}, work directory at {work}')
            else:
                shutil.rmtree(artifacts, ignore_errors=True)
                shutil.rmtree(work, ignore_errors=True)
            results.append(elapsed)
            print(f'{name} passed in {elapsed:.1f}s')
    print(f'PASS {len(results)} scenario run(s), skipped {len(skipped)}' + (f' ({", ".join(skipped)})' if skipped else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
