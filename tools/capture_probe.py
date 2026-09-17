#!/usr/bin/python3
"""M1 ScreenShot2 feasibility: killable capture child, one GLib input owner."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / 'tools' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


libei = module('libei_probe')
codec = module('capture_codec')
harness = libei.harness
Failure = harness.Failure
CAPTURE_SECONDS = 3
CLEANUP_SECONDS = 1
MAX_RAW = 8 * 1024 * 1024
MAX_STATUS = 128 * 1024


def pipe_inventory(inode):
    found = []
    for path in Path('/proc/self/fd').iterdir():
        try:
            if os.readlink(path) == inode:
                found.append({'fd': int(path.name), 'access': fcntl.fcntl(int(path.name), fcntl.F_GETFL) & os.O_ACCMODE})
        except FileNotFoundError:
            pass
    return found


def normalize(value):
    import dbus
    if isinstance(value, (bool, dbus.Boolean)):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    raise ValueError('Unexpected metadata type')


def capture_child(config_path):
    """All potentially blocking capture/codec/filesystem work lives here."""
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    from PIL import Image
    generation, runtime, artifacts, manifest, deadline = libei.windows.private_context(dict(os.environ))
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    folder = config_path.parent
    if folder.parent != artifacts or config['generation'] != generation or folder.name != config['request_id']:
        raise Failure('capture_identity', 'Capture request identity/path mismatch')
    if not manifest.get('screenshot', {}).get('enabled'):
        raise Failure('capture_prerequisite', 'Private screenshot capability was not enabled')
    end = min(config['deadline'], deadline)
    if not 0 < end - time.monotonic() <= CAPTURE_SECONDS:
        raise Failure('capture_deadline', 'Invalid capture deadline')
    def report(stage, **fields):
        print(json.dumps({'stage': stage, 'generation': generation, 'request_id': config['request_id'],
                          'monotonic_ns': time.monotonic_ns(), **fields}), flush=True)
    DBusGMainLoop(set_as_default=True)
    context = GLib.MainContext.default()
    bus = None
    read_fd = write_fd = None
    wrapper = None
    watch = None
    pending = None
    state = {'metadata': None, 'eof': False, 'error': None}
    raw = bytearray()
    fault = config.get('fault', 'none')
    temporary = folder / 'image.partial'
    final = folder / 'image.png'
    try:
        bus = dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(read_fd, False)
        inode = os.readlink(f'/proc/self/fd/{read_fd}')
        capacity = fcntl.fcntl(read_fd, fcntl.F_GETPIPE_SZ)
        report('pipe', inode=inode, capacity=capacity, python=sys.version.split()[0], dbus_python=dbus.__version__, pillow=Image.__version__)
        def read_ready(fd, condition):
            nonlocal watch
            try:
                for _ in range(4):
                    try:
                        chunk = os.read(fd, 4096 if fault in ('partial', 'stall-pipe') else 65536)
                    except BlockingIOError:
                        return True
                    if not chunk:
                        state['eof'] = True
                        report('eof', bytes=len(raw), local_pipe_fds=pipe_inventory(inode))
                        watch = None
                        return False
                    if not raw:
                        report('first-byte')
                    raw.extend(chunk)
                    if len(raw) > MAX_RAW:
                        raise Failure('capture_size', 'Raw image exceeds fixed output cap')
                    if fault in ('partial', 'stall-pipe'):
                        report('partial', bytes=len(raw), capacity=capacity, inode=inode)
                        if fault == 'stall-pipe':
                            os.kill(os.getpid(), signal.SIGSTOP)
                        raise Failure('injected_partial', 'Closed reader after positive partial image')
            except Exception as exc:
                state['error'] = exc
                watch = None
                return False
            return True
        watch = GLib.io_add_watch(read_fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, read_ready)
        def reply(metadata):
            try:
                state['metadata'] = normalize(metadata)
                expected = codec.validate_metadata(state['metadata'], config['screen'])
                fds = pipe_inventory(inode)
                if any(row['access'] != os.O_RDONLY for row in fds):
                    raise Failure('capture_fd', 'Local write duplicate remains after D-Bus reply')
                report('metadata', metadata=state['metadata'], expected_bytes=expected, local_pipe_fds=fds)
            except Exception as exc:
                state['error'] = exc
        def error(exc):
            state['error'] = Failure('capture_dbus', str(exc))
            state['dbus_error'] = exc.get_dbus_name()
        wrapper = dbus.types.UnixFd(write_fd)
        pending = bus.call_async('org.kde.KWin.ScreenShot2', '/org/kde/KWin/ScreenShot2',
                                 'org.kde.KWin.ScreenShot2', 'CaptureScreen', 'sa{sv}h',
                                 (config['screen'], dbus.Dictionary({'include-cursor': False, 'native-resolution': True,
                                                                    'hide-caller-windows': False}, signature='sv'), wrapper),
                                 reply, error, timeout=max(.001, end - time.monotonic()))
        # Message append duplicates the FD (covered by actual native-message test).
        # Queue owns that copy; neither this wrapper nor the original may hold EOF open.
        os.close(wrapper.take())
        wrapper = None
        os.close(write_fd)
        write_fd = None
        report('requested', reader_installed_before_request=True, screen=config['screen'])
        if fault == 'stall-reply':
            os.kill(os.getpid(), signal.SIGSTOP)
        timer = GLib.timeout_add(5, lambda: True)
        try:
            while not state['error'] and not (state['metadata'] is not None and state['eof']):
                if time.monotonic() >= end:
                    raise Failure('capture_timeout', 'Capture work deadline expired')
                context.iteration(True)
        finally:
            GLib.source_remove(timer)
        if state['error']:
            raise state['error']
        os.close(read_fd)
        read_fd = None
        report('raw-complete', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), local_pipe_fds=pipe_inventory(inode))
        image = codec.decode(raw, state['metadata'], config['screen'])
        checks = codec.pixel_checks(image, config['state'], config['geometry']) if 'state' in config else []
        report('encode', pixel_checks=checks)
        if fault == 'stall-encode':
            os.kill(os.getpid(), signal.SIGSTOP)
        with temporary.open('xb') as output:
            image.save(output, format='PNG')
        with Image.open(temporary) as reopened:
            reopened.load()
            if reopened.format != 'PNG' or reopened.size != (1280, 720):
                raise Failure('capture_png', 'PNG did not fully decode to expected dimensions')
        if time.monotonic() >= end:
            raise Failure('capture_timeout', 'Late PNG validation')
        temporary.rename(final)
        report('success', path=str(final), dimensions=[1280, 720], png_sha256=harness.digest(final),
               png_bytes=final.stat().st_size, capture_timestamp_kind='request-and-completion-monotonic',
               backend='org.kde.KWin.ScreenShot2', metadata=state['metadata'])
        return 0
    except Exception as exc:
        report('error', code=getattr(exc, 'code', 'capture_failed'), message=str(exc),
               dbus_error=state.get('dbus_error'), bytes=len(raw))
        return 1
    finally:
        if watch is not None:
            GLib.source_remove(watch)
        if pending is not None:
            pending.cancel()
        if wrapper is not None:
            os.close(wrapper.take())
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if bus is not None:
            bus.close()
        if temporary.exists():
            temporary.unlink()


class Capture:
    """Nonblocking child observer; no raw data, codec or waits in parent callbacks."""
    def __init__(self, owner, *, fault='none', state=None, geometry=None, screen=None):
        self.owner = owner
        self.started = time.monotonic()
        self.deadline = min(self.started + CAPTURE_SECONDS, owner.deadline - CLEANUP_SECONDS)
        if self.deadline <= self.started:
            raise Failure('capture_timeout', 'No capture budget remains')
        self.id = 'capture-' + uuid.uuid4().hex
        self.folder = owner.artifacts / self.id
        self.folder.mkdir(mode=0o700)
        self.config = {'generation': owner.generation, 'request_id': self.id, 'deadline': self.deadline,
                       'screen': screen or owner.manifest['initial_presented']['sole_output_name'], 'fault': fault}
        if state is not None:
            self.config.update(state=state, geometry=geometry)
        harness.atomic(self.folder / 'request.json', self.config)
        self.err = (self.folder / 'stderr.log').open('wb')
        self.process = subprocess.Popen(['/usr/bin/python', '-I', str(Path(__file__).resolve()), '_capture',
                                         str(self.folder / 'request.json')], env=owner.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=self.err)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.buffer = b''
        self.total_status = 0
        self.rows = []
        self.done = False
        self.aborting = False
        self.result = {'request_id': self.id, 'generation': owner.generation, 'fault': fault,
                       'process': harness.identity(self.process.pid), 'started': self.started,
                       'deadline': self.deadline, 'rows': self.rows, 'accepted': False}

    def stage(self, name):
        return next((row for row in self.rows if row['stage'] == name), None)

    def abort(self, code):
        if self.aborting:
            return
        self.aborting = True
        self.cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        self.result.update(error=code, abort_started=time.monotonic())
        inode = (self.stage('pipe') or {}).get('inode')
        try:
            kwin = self.owner.manifest['processes']['kwin']['pid']
            matching = []
            for path in Path(f'/proc/{kwin}/fd').iterdir():
                try:
                    if os.readlink(path) == inode:
                        matching.append(path.name)
                except FileNotFoundError:
                    pass
            self.result['server_fd_observation'] = {'observable': True, 'matching_before_abort': matching}
        except (PermissionError, FileNotFoundError) as exc:
            self.result['server_fd_observation'] = {'observable': False, 'reason': type(exc).__name__}
        # Even an empty FD snapshot cannot prove synchronous compositor work stopped.

        if self.process.poll() is None:
            self.process.kill()

    def tick(self):
        if self.done:
            return
        try:
            if not self.aborting:
                for _ in range(4):
                    try:
                        chunk = os.read(self.process.stdout.fileno(), 16384)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    self.total_status += len(chunk)
                    if self.total_status > MAX_STATUS:
                        raise Failure('capture_protocol', 'Capture status cap exceeded')
                    self.buffer += chunk
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    row = json.loads(line)
                    if row['generation'] != self.owner.generation or row['request_id'] != self.id:
                        raise Failure('capture_protocol', 'Wrong capture response identity')
                    self.rows.append(row)
            now = time.monotonic()
            code = self.process.poll()
            if not self.aborting and now >= self.deadline:
                self.abort('capture_timeout')
            elif not self.aborting and code is not None:
                success = self.stage('success')
                if code == 0 and success and success['path'] == str(self.folder / 'image.png'):
                    if not (self.folder / 'image.png').is_file():
                        raise Failure('capture_publication', 'Completed PNG absent')
                    # Process/publication checks can consume the remaining budget.
                    # Sample at acceptance, never reuse the pre-poll/pre-stat time.
                    accepted_at = time.monotonic()
                    if accepted_at >= self.deadline:
                        self.abort('capture_timeout')
                    else:
                        self.result.update(accepted=True, accepted_at=accepted_at,
                                           seconds=accepted_at - self.started,
                                           server_cleanup='exact_raw_bytes_and_eof', path=success['path'])
                        self.finish()
                else:
                    self.abort((self.stage('error') or {}).get('code', 'capture_child_failed'))
            if self.aborting:
                if self.process.poll() is not None:
                    for name in ('image.png', 'image.partial'):
                        (self.folder / name).unlink(missing_ok=True)
                    error = self.stage('error') or {}
                    # Exact InvalidScreen is rejected before KWin duplicates the writer.
                    safe_rejection = error.get('dbus_error') == 'org.kde.KWin.ScreenShot2.Error.InvalidScreen'
                    self.result.update(cleanup_seconds=time.monotonic() - self.result['abort_started'],
                                       local_cleanup=True, server_cleanup='rejected_before_capture' if safe_rejection else 'unconfirmed',
                                       session_stop_required=not safe_rejection, png_published=False)
                    self.finish()
                elif now >= self.cleanup_deadline:
                    raise Failure('capture_cleanup_timeout', 'Capture child failed to reap')
        except Exception as exc:
            if self.aborting:
                self.owner.fatal = exc
            else:
                self.abort(getattr(exc, 'code', 'capture_protocol'))

    def finish(self):
        self.done = True
        self.result.update(process_reaped=self.process.poll() is not None, returncode=self.process.returncode,
                           finished=time.monotonic())
        self.process.stdout.close()
        self.err.close()
        if self.result.get('cleanup_seconds', 0) > CLEANUP_SECONDS:
            self.owner.fatal = Failure('capture_cleanup_timeout', 'Local capture cleanup exceeded bound')
        harness.atomic(self.folder / 'receipt.json', self.result)


class Probe(libei.Probe):
    def __init__(self, binary, scenario):
        self.captures = []
        super().__init__(binary, scenario)
        self.capture_result = {'scope': 'M1 feasibility, not production support', 'scenario': scenario,
                               'generation': self.generation, 'probe_sha256': harness.digest(__file__),
                               'codec_sha256': harness.digest(codec.__file__), 'captures': [], 'checks': [], 'kwin_image_header_sha256': harness.digest('/usr/include/qt6/QtGui/qimage.h'),
                               'bounds_seconds': {'capture': CAPTURE_SECONDS, 'local_cleanup': CLEANUP_SECONDS,
                                                  'failed_session_stop': 15, 'cancel_dispatch': .1, 'fixture_release': .5}}

    def tick(self):
        keep = super().tick()
        for capture in self.captures:
            capture.tick()
        return keep

    def start_capture(self, **kwargs):
        if any(not capture.done for capture in self.captures):
            raise Failure('capture_busy', 'Only one capture admitted')
        if any(c.result.get('session_stop_required') for c in self.captures):
            raise Failure('session_unavailable', 'Unconfirmed capture cleanup requires service stop')
        value = Capture(self, **kwargs)
        self.captures.append(value)
        self.capture_result['captures'].append(value.result)
        return value

    def finish_capture(self, capture):
        self.wait(lambda: capture.done, CAPTURE_SECONDS + CLEANUP_SECONDS + .1, 'capture completion/cleanup')
        if capture.result.get('session_stop_required'):
            self.capture_result['session_unavailable_ns'] = time.monotonic_ns()
            raise Failure('capture_cleanup_unconfirmed', 'Owned private desktop must stop after unconfirmed server cleanup')
        if not capture.result['accepted']:
            raise Failure(capture.result['error'], 'Capture did not complete')
        return capture

    def fixture_state(self, state=None):
        # Independent CLI child; never block the input event loop on control recv.
        folder = self.artifacts / ('control-' + uuid.uuid4().hex)
        folder.mkdir(mode=0o700)
        with (folder / 'stdout.json').open('wb') as out, (folder / 'stderr.log').open('wb') as err:
            child = subprocess.Popen(['/usr/bin/python', '-I', str(harness.SCRIPT), 'control', '--socket',
                                      self.env['HARNESS_CONTROL'], '--generation', self.generation,
                                      *(['--state', str(state), 'set_state'] if state is not None else ['status'])], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                self.wait(lambda: child.poll() is not None, 3.5, 'fixture presentation')
            finally:
                if child.poll() is None:
                    child.kill()
                    self.wait(lambda: child.poll() is not None, 1, 'control child reap')
            if child.returncode:
                raise Failure('fixture_control', 'Fixture state CLI failed')
        receipt = json.loads((folder / 'stdout.json').read_text())
        if state is None:
            return receipt
        presented = receipt['presented']
        if presented['generation'] != self.generation or presented['state'] != state or presented['source'] != 'control':
            raise Failure('fixture_identity', 'Unexpected presentation receipt')
        return presented

    def geometry(self):
        snapshot = self.query()
        return next(row['client'] for row in snapshot['windows'] if row['uuid'] == self.target)

    def functional(self):
        started = time.monotonic()
        self.initial()
        for state in (0x13579BDF, 0x2468ACE0):
            presented = self.fixture_state(state)
            capture = self.finish_capture(self.start_capture(state=state, geometry=self.geometry()))
            requested = capture.stage('requested')['monotonic_ns']
            assert requested > presented['monotonic_ns']
            self.capture_result['checks'].append({'kind': 'fresh-state', 'presented': presented,
                                                  'request_id': capture.id, 'requested_after_presented': True})
        self.begin(['W'])
        self.release()
        self.wait(lambda: bool(self.events and any(e['event'] == 'presented' and e['source'] == 'input' for e in self.events)),
                  .5, 'rendered input')
        # Wait for the latest committed input state, not an earlier coalesced frame.
        self.wait(lambda: max(e['revision'] for e in self.events if e['event'] == 'committed') ==
                  max(e['revision'] for e in self.events if e['event'] == 'presented'), .5, 'latest input presentation')
        presented = next(e for e in reversed(self.events) if e['event'] == 'presented')
        capture = self.finish_capture(self.start_capture(state=presented['state'], geometry=self.geometry()))
        assert capture.stage('requested')['monotonic_ns'] > presented['monotonic_ns']
        self.capture_result['checks'].append({'kind': 'rendered-input', 'presented': presented, 'request_id': capture.id})
        for _ in range(7):
            self.finish_capture(self.start_capture(state=presented['state'], geometry=self.geometry()))
        self.capture_result['aggregate_ready_seconds'] = self.manifest['startup_seconds'] + time.monotonic() - started
        assert self.capture_result['aggregate_ready_seconds'] < 30

    def cancel_existing_action(self, capture):
        action = self.action
        left, right = socket.socketpair()
        left.setblocking(False)
        receipt = self.artifacts / 'capture-cancel-client.json'
        child = subprocess.Popen(['/usr/bin/python', '-I', str(Path(libei.__file__).resolve()), '_cancel',
                                  str(right.fileno()), 'send', str(receipt)], pass_fds=(right.fileno(),), env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        right.close()
        completed = False
        def arrived(fd, condition):
            nonlocal completed
            completed = True
            try:
                self.cancel('client_cancel', int(left.recv(128)))
            except Exception as exc:
                self.fatal = exc
            return False
        watch = self.GLib.io_add_watch(left.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP, arrived)
        try:
            self.wait(lambda: bool(action.get('cancel_reason')), .5, 'capture-time cancellation')
            self.wait(lambda: not self.received_held and self.modifiers in (0, None), .5, 'capture-time release')
            releases = [e for e in self.events[action['fixture_from']:] if e['event'] == 'key' and e['state'] == 0]
            dispatch = (action['finished_ns'] - action['cancel_requested_ns']) / 1e9
            acknowledged = (max(e['monotonic_ns'] for e in releases) - action['cancel_requested_ns']) / 1e9
            assert dispatch <= .1 and acknowledged <= .5
            assert not capture.done
            self.capture_result['checks'].append({'kind': 'cancel-during-capture', 'capture_stage': capture.rows[-1]['stage'],
                                                  'dispatch_seconds': dispatch, 'fixture_release_seconds': acknowledged,
                                                  'capture_still_running': True, 'action': action})
        finally:
            if not completed:
                self.GLib.source_remove(watch)
            left.close()
            self.action = None

    def run_capture_scenario(self, scenario):
        interface = self.dbus.Interface(self.bus.get_object('org.kde.KWin.ScreenShot2', '/org/kde/KWin/ScreenShot2', introspect=False),
                                        'org.freedesktop.DBus.Introspectable')
        (xml,) = self.call(interface.Introspect)
        (self.artifacts / 'screenshot-introspection.xml').write_text(str(xml))
        properties = self.dbus.Interface(self.bus.get_object('org.kde.KWin.ScreenShot2', '/org/kde/KWin/ScreenShot2', introspect=False),
                                         'org.freedesktop.DBus.Properties')
        (version,) = self.call(properties.Get, 'org.kde.KWin.ScreenShot2', 'Version')
        self.capture_result['interface_version'] = int(version)
        assert int(version) == 5
        if scenario == 'functional':
            self.functional()
        else:
            self.initial()
            if scenario == 'slow-query':
                self.slow_query_scenario()
                self.finish_capture(self.start_capture())
                self.capture_result['checks'].extend(self.checks)
            elif scenario == 'invalid-screen':
                capture = self.start_capture(screen='kde-agent-nonexistent-output')
                self.wait(lambda: capture.done, 4.1, 'invalid output rejection')
                assert not capture.result['accepted'] and not capture.result['session_stop_required']
                assert capture.stage('error')['dbus_error'].endswith('.InvalidScreen')
                self.finish_capture(self.start_capture())
            else:
                fault = {'responsive-pipe': 'stall-pipe', 'responsive-encode': 'stall-encode'}.get(scenario, scenario)
                if scenario.startswith('responsive-'):
                    self.begin(['SHIFT', 'W'])
                capture = self.start_capture(fault=fault)
                if scenario.startswith('responsive-'):
                    stage = 'partial' if fault == 'stall-pipe' else 'encode'
                    self.wait(lambda: capture.stage(stage) is not None or capture.done, 1.5, 'capture fault stage')
                    assert capture.stage(stage), capture.result
                    self.cancel_existing_action(capture)
                    control_start = time.monotonic()
                    status = self.fixture_state()
                    self.capture_result['checks'].append({'kind': 'control-during-capture', 'seconds': time.monotonic() - control_start, 'status': status, 'capture_still_running': not capture.done})
                    assert not capture.done
                self.finish_capture(capture)
        assert self.heartbeat_max <= .1
        self.capture_result['outcome'] = 'passed'

    def close_capture(self):
        for capture in self.captures:
            if not capture.done:
                capture.abort('probe_close')
                end = time.monotonic() + CLEANUP_SECONDS
                while not capture.done and time.monotonic() < end:
                    self.main_context.iteration(True)
        self.capture_result['heartbeat_max_seconds'] = self.heartbeat_max
        self.capture_result['input_uncertain'] = self.input.uncertain
        self.capture_result['final_held'] = sorted(self.received_held)
        harness.atomic(self.artifacts / 'capture-probe.json', self.capture_result)
        self.close()


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == '_capture':
        return capture_child(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('scenario', choices=('functional', 'invalid-screen', 'partial', 'stall-reply',
                                            'stall-pipe', 'stall-encode', 'responsive-pipe', 'responsive-encode', 'slow-query'))
    args = parser.parse_args()
    probe = Probe(args.binary, args.scenario)
    try:
        probe.run_capture_scenario(args.scenario)
        return 0
    except Exception as exc:
        probe.capture_result.update(outcome='failed', error={'code': getattr(exc, 'code', 'assertion'), 'message': str(exc)})
        print(json.dumps(probe.capture_result['error']), file=sys.stderr)
        return 1
    finally:
        probe.close_capture()


if __name__ == '__main__':
    raise SystemExit(main())
