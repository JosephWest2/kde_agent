"""Killable, provisional M1 ScreenShot2 child; replace with #35 before release.

Derived from tools/capture_probe.py. Retains real D-Bus UnixFd ownership, bounded
pipe draining, metadata plus EOF completion, and full PNG decode before publish.
No toolkit or codec work runs in the worker's GLib owner. The supervising parent
must reap the child and reject late receipts, and stop the generation on abort.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

CAPTURE_SECONDS = 3.0
MAX_RAW = 8 * 1024 * 1024
MAX_RECEIPT = 32 * 1024
CONFIG_KEYS = {'generation', 'request_id', 'deadline', 'screen', 'output_dir',
               'runtime_dir', 'bus_address'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


class Failure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _private(info, kind):
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure('capture_path', 'Capture path is not owner-private or has wrong type')


def _path(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\0' in value
            or os.path.normpath(value) != value or value.startswith('//')):
        raise Failure('capture_path', 'Capture paths must be normalized and absolute')
    return Path(value)


def _directory(path):
    """Anchor each path hop without following symlinks; caller owns final FD."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _private(os.fstat(fd), stat.S_ISDIR)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_json(fd, limit):
    _private(os.fstat(fd), stat.S_ISREG)
    with os.fdopen(os.dup(fd), 'rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Failure('capture_protocol', 'Capture JSON exceeds size limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Failure('capture_protocol', 'Duplicate capture JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def load_config(config_path, environment):
    """Return validated config and owned output directory FD (caller closes)."""
    path = _path(str(config_path))
    if path.name != 'request.json':
        raise Failure('capture_path', 'Capture configuration must be request.json')
    folder_fd = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
        try:
            config = _read_json(fd, MAX_RECEIPT)
        finally:
            os.close(fd)
        if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
            raise Failure('capture_protocol', 'Invalid capture configuration fields')
        for key in ('generation', 'request_id'):
            if not isinstance(config[key], str) or not IDENTIFIER.fullmatch(config[key]):
                raise Failure('capture_identity', 'Invalid capture request identity')
        if _path(config['output_dir']) != path.parent or path.parent.name != config['request_id']:
            raise Failure('capture_identity', 'Capture request path identity mismatch')
        if not isinstance(config['screen'], str) or not 0 < len(config['screen']) <= 256 or '\0' in config['screen']:
            raise Failure('capture_protocol', 'Invalid capture screen')
        end = config['deadline']
        if type(end) not in (int, float) or not math.isfinite(end) or not 0 < end - time.monotonic() <= CAPTURE_SECONDS:
            raise Failure('capture_timeout', 'Invalid or expired capture deadline')
        runtime = _path(config['runtime_dir'])
        runtime_fd = _directory(runtime)
        try:
            # The owned desktop constructs this single fixed bus endpoint.
            address = 'unix:path=' + str(runtime / 'bus')
            if (config['bus_address'] != address or environment.get('DBUS_SESSION_BUS_ADDRESS') != address
                    or environment.get('XDG_RUNTIME_DIR') != str(runtime)):
                raise Failure('capture_endpoint', 'Capture must use the explicit private bus')
            _private(os.stat('bus', dir_fd=runtime_fd, follow_symlinks=False), stat.S_ISSOCK)
        finally:
            os.close(runtime_fd)
        for name in ('image.png', 'image.partial', 'result.json', 'result.partial'):
            try:
                os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Failure('capture_path', 'Capture output already exists')
        return config, folder_fd
    except BaseException:
        os.close(folder_fd)
        raise


def pipe_inventory(inode):
    result = []
    for path in Path('/proc/self/fd').iterdir():
        try:
            if os.readlink(path) == inode:
                result.append({'fd': int(path.name), 'access': fcntl.fcntl(int(path.name), fcntl.F_GETFL) & os.O_ACCMODE})
        except FileNotFoundError:
            pass
    return result


def normalize(value, depth=0):
    import dbus
    if depth > 2:
        raise Failure('capture_metadata', 'Nested capture metadata')
    if isinstance(value, (bool, dbus.Boolean)):
        return bool(value)
    if isinstance(value, str) and len(value) <= 256:
        return str(value)
    if isinstance(value, int) and -(2**63) <= value < 2**64:
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, dict) and len(value) <= 16:
        if any(not isinstance(k, str) or len(k) > 64 for k in value):
            raise Failure('capture_metadata', 'Invalid capture metadata key')
        return {str(k): normalize(v, depth + 1) for k, v in value.items()}
    raise Failure('capture_metadata', 'Unexpected capture metadata type or size')


def _publish_receipt(folder_fd, value):
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_RECEIPT:
        raise Failure('capture_protocol', 'Capture receipt exceeds size limit')
    fd = os.open('result.partial', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    os.replace('result.partial', 'result.json', src_dir_fd=folder_fd, dst_dir_fd=folder_fd)


def capture_child(config_path):
    """Return 0 only after complete capture and publication within the deadline."""
    config, folder_fd = load_config(config_path, os.environ)
    bus = pending = wrapper = watch = None
    read_fd = write_fd = None
    GLib = None
    state = {'metadata': None, 'eof': False, 'error': None}
    raw = bytearray()
    stages = []
    end = config['deadline']
    receipt = {'schema': 1, 'provider': 'm1-provisional', 'generation': config['generation'],
               'request_id': config['request_id'], 'deadline': end, 'ok': False,
               'stages': stages, 'session_stop_required': True}
    def check_deadline():
        if time.monotonic() >= end:
            raise Failure('capture_timeout', 'Capture work deadline expired')
    def stage(name, **fields):
        stages.append({'stage': name, 'at': time.monotonic(), **fields})
    def cleanup():
        # Attempt every release even if one native object's disposal fails.
        nonlocal watch, pending, wrapper, read_fd, write_fd, bus
        actions = []
        if watch is not None:
            actions.append((GLib.source_remove, watch))
            watch = None
        if pending is not None:
            actions.append((lambda value: value.cancel(), pending))
            pending = None
        if wrapper is not None:
            actions.append((lambda value: os.close(value.take()), wrapper))
            wrapper = None
        for fd in (read_fd, write_fd):
            if fd is not None:
                actions.append((os.close, fd))
        read_fd = write_fd = None
        if bus is not None:
            actions.append((lambda value: value.close(), bus))
            bus = None
        failure = None
        for action, value in actions:
            try:
                action(value)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise Failure('capture_cleanup', str(failure)[:1024]) from failure
    try:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
        from PIL import Image
        from . import provisional_codec as codec
        check_deadline()
        DBusGMainLoop(set_as_default=True)
        context = GLib.MainContext.default()
        bus = dbus.bus.BusConnection(config['bus_address'])
        check_deadline()
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(read_fd, False)
        inode = os.readlink(f'/proc/self/fd/{read_fd}')
        stage('pipe', inode=inode, capacity=fcntl.fcntl(read_fd, fcntl.F_GETPIPE_SZ))
        def read_ready(fd, condition):
            nonlocal watch
            try:
                check_deadline()
                for _ in range(4):
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        return True
                    if not chunk:
                        state['eof'] = True
                        stage('eof', bytes=len(raw))
                        watch = None
                        return False
                    raw.extend(chunk)
                    if len(raw) > MAX_RAW:
                        raise Failure('capture_size', 'Raw capture exceeds cap')
            except Exception as exc:
                state['error'] = exc
                watch = None
                return False
            return True
        watch = GLib.io_add_watch(read_fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, read_ready)
        def reply(metadata):
            try:
                check_deadline()
                state['metadata'] = normalize(metadata)
                codec.validate_metadata(state['metadata'], config['screen'])
                if any(row['access'] != os.O_RDONLY for row in pipe_inventory(inode)):
                    raise Failure('capture_fd', 'Local writer remains after D-Bus reply')
                stage('metadata')
            except Exception as exc:
                state['error'] = exc
        def error(exc):
            state['error'] = Failure('capture_dbus', str(exc)[:1024])
        wrapper = dbus.types.UnixFd(write_fd)
        pending = bus.call_async('org.kde.KWin.ScreenShot2', '/org/kde/KWin/ScreenShot2',
                                'org.kde.KWin.ScreenShot2', 'CaptureScreen', 'sa{sv}h',
                                (config['screen'], dbus.Dictionary({'include-cursor': False,
                                 'native-resolution': True, 'hide-caller-windows': False}, signature='sv'), wrapper),
                                reply, error, timeout=max(.001, end - time.monotonic()))
        # libdbus message owns a duplicate; original AND UnixFd wrapper must close
        # immediately after queueing so only the compositor can keep EOF open.
        os.close(wrapper.take())
        wrapper = None
        os.close(write_fd)
        write_fd = None
        stage('requested', reader_installed_before_request=True)
        timer = GLib.timeout_add(5, lambda: True)
        try:
            while not state['error'] and not (state['metadata'] is not None and state['eof']):
                check_deadline()
                context.iteration(True)
        finally:
            GLib.source_remove(timer)
        if state['error']:
            raise state['error']
        check_deadline()
        cleanup()
        check_deadline()
        image = codec.decode(raw, state['metadata'], config['screen'])
        fd = os.open('image.partial', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=folder_fd)
        with os.fdopen(fd, 'w+b') as stream:
            image.save(stream, format='PNG')
            stream.flush()
            stream.seek(0)
            with Image.open(stream) as reopened:
                reopened.load()
                if reopened.format != 'PNG' or reopened.size != (1280, 720) or reopened.mode != 'RGBA':
                    raise Failure('capture_png', 'PNG did not fully decode to expected dimensions')
            stream.seek(0)
            encoded = stream.read(MAX_RAW + 1)
        if len(encoded) > MAX_RAW:
            raise Failure('capture_size', 'Encoded capture exceeds cap')
        check_deadline()
        os.rename('image.partial', 'image.png', src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        check_deadline()
        receipt.update(ok=True, path=str(Path(config['output_dir']) / 'image.png'),
                       png_sha256=hashlib.sha256(encoded).hexdigest(), png_bytes=len(encoded),
                       dimensions=[1280, 720], metadata=state['metadata'], eof=True, raw_bytes=len(raw),
                       completed_at=time.monotonic(), session_stop_required=False)
        _publish_receipt(folder_fd, receipt)
        check_deadline()  # Publication is part of the work budget, not just encode.
        return 0
    except Exception as exc:
        for key in ('path', 'png_sha256', 'png_bytes', 'dimensions', 'completed_at'):
            receipt.pop(key, None)
        receipt.update(ok=False, code=getattr(exc, 'code', 'capture_failed'), message=str(exc)[:1024],
                       raw_bytes=len(raw), eof=state['eof'], session_stop_required=True)
        _publish_receipt(folder_fd, receipt)
        return 1
    finally:
        try:
            cleanup()
        finally:
            for name in ('image.partial', 'result.partial') + (() if receipt['ok'] else ('image.png',)):
                try:
                    os.unlink(name, dir_fd=folder_fd)
                except FileNotFoundError:
                    pass
            os.close(folder_fd)


def main():
    try:
        if len(sys.argv) != 2:
            raise Failure('capture_protocol', 'Expected one absolute configuration path')
        return capture_child(sys.argv[1])
    except Exception as exc:
        # Bad/unsafe configuration has no trusted receipt destination.
        print(json.dumps({'code': getattr(exc, 'code', 'capture_failed'), 'message': str(exc)[:1024]}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
