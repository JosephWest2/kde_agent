"""Installed, read-only M1 prerequisite checks; production replacement is #35.

Policy and receipt validation derive from tools/dependencies.py (M1 #9).
The fixed libei ABI/FD-ownership boundary derives from tools/libei_binding.py
(M1 #12). Neither checkout tools nor historical evidence are runtime imports.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from .contracts import ContractError, OPERATIONS

KDOTool_REVISION = 'be03ce90c09350898556436bac74ed35fe928617'
KDOTool_LOCK_SHA256 = 'd6beea15d1a9254586d71ac1c5c55d088d9dc3c9a8e980c6af7c2d8ee8f25edc'
# M1 historical build plus the same pinned-source rebuild qualified by #20.
# Both have actual private query/cleanup evidence; see evidence/issue-20.
KDOTool_BINARY_SHA256S = frozenset({
    '62e7ee53096d933ec29e8e5d439b895590f851a40f0dcd87b87db9a6dc4749de',
    'b7a300d5a2f0b95a21d71dca5757328382bb6dd887e4ac975fffb59e2351bd21',
})
KDOTool_RELEASE = '0.3.0'
LIBEI_PATH = Path('/usr/lib/libei.so.1')
LIBEI_SHA256 = '93897fc311319920c1c25e9422db62ebe8324d54c5e0c3d4a9f15a0a6cac2501'
MAX_OUTPUT = 65536
MAX_RECEIPT = 2 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024
RUNTIME_EXECUTABLES = ('kwin_wayland', 'dbus-daemon', 'dbus-send', 'systemctl', 'systemd-run', 'env')
PROVISIONAL = {
    'provider': 'm1-provisional', 'production_readiness': False,
    'release_qualified': False, 'replacement_issue': 35,
}
BINDINGS_PROBE = r'''
import json, sys, gi, dbus, PIL
from PIL import Image
from gi.repository import Gio, GLib
Image.init()
print(json.dumps({
    'python': sys.version.split()[0],
    'versions': {'gi': gi.__version__, 'dbus': dbus.__version__, 'PIL': PIL.__version__,
                 'GLib': '.'.join(map(str, (GLib.MAJOR_VERSION, GLib.MINOR_VERSION, GLib.MICRO_VERSION)))},
    'origins': {name: module.__file__ for name, module in [('gi', gi), ('dbus', dbus), ('PIL', PIL)]},
    'gio_unix_fd': hasattr(Gio, 'UnixFDList') and hasattr(Gio.DBusConnection, 'call_with_unix_fd_list'),
    'png': 'PNG' in Image.OPEN,
}))
'''


class _Failure(Exception):
    def __init__(self, code, reason, repair, observed=None):
        self.code, self.reason, self.repair, self.observed = code, reason, repair, observed
        super().__init__(reason)


def _time_left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Failure('prerequisite_incompatible', 'deadline_expired',
                       'Check system load and retry within the documented timeout.')
    return remaining


def _read(path, deadline, maximum):
    """Bound bytes and reject FIFOs/devices before reading dependency state."""
    _time_left(deadline)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise _Failure('prerequisite_incompatible', 'invalid_file',
                           'Restore a regular dependency file within the documented size limit.', str(path))
        chunks, count = [], 0
        while True:
            _time_left(deadline)
            chunk = os.read(fd, min(65536, maximum + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > maximum:
                raise _Failure('prerequisite_incompatible', 'file_limit',
                               'Restore the verified dependency artifact.', str(path))
        _time_left(deadline)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _run(argv, deadline, *, manager=False):
    """Killable import/version probes; cap output while the child is running.

    All descendants are killed even when the direct child exits successfully.
    A short reap reserve is included in the caller's absolute deadline.
    """
    available = _time_left(deadline)
    reserve = min(.25, available / 4)
    work_end = min(time.monotonic() + 5, deadline - reserve)
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'KDE_SESSION_VERSION': '6'}
    if manager:
        runtime = '/run/user/' + str(os.getuid())
        env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS='unix:path=' + runtime + '/bus')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, cwd='/', start_new_session=True)
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for stream in output:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                left = work_end - time.monotonic()
                if left <= 0:
                    raise _Failure('prerequisite_incompatible', 'helper_timeout',
                                   'Check the dependency and system load, then retry.', Path(argv[0]).name)
                for key, _ in selector.select(min(left, .02)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.fileobj].extend(chunk)
                        if len(output[key.fileobj]) > MAX_OUTPUT:
                            raise _Failure('prerequisite_incompatible', 'output_limit',
                                           'Inspect the dependency version/import outside doctor.', Path(argv[0]).name)
            if process.returncode:
                raise _Failure('prerequisite_incompatible', 'helper_failed',
                               'Restore the documented runtime package or binding.',
                               {'executable': argv[0], 'exit_status': process.returncode,
                                'diagnostic': output[process.stderr].decode(errors='replace')[-2000:]})
            _time_left(deadline)
            return output[process.stdout].decode('utf-8').strip()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise _Failure('prerequisite_incompatible', 'cleanup_unconfirmed',
                           'Inspect the owned prerequisite helper before retrying.', {'pid': process.pid}) from None
        finally:
            for stream in output:
                stream.close()


def _validate_receipt(receipt):
    """Retain M1's complete resolved-build receipt shape and pinned policy."""
    def fail():
        raise _Failure('prerequisite_incompatible', 'provenance_mismatch',
                       'Restore the audited kdotool build and complete setup receipt; a new build needs renewed evidence.')

    def text(value):
        return isinstance(value, str) and bool(value)

    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    if not isinstance(receipt, dict):
        fail()
    required = {'revision': KDOTool_REVISION, 'cargo_lock_sha256': KDOTool_LOCK_SHA256,
                'release': KDOTool_RELEASE,
                'patches': [], 'clean_checkout': True}
    if any(receipt.get(key) != value for key, value in required.items()) or receipt.get('clean_checkout') is not True:
        fail()
    if receipt.get('binary_sha256') not in KDOTool_BINARY_SHA256S:
        fail()
    if receipt.get('build_command') != 'cargo build --release --locked --manifest-path <pinned-source>/Cargo.toml':
        fail()
    toolchain = receipt.get('toolchain')
    if not isinstance(toolchain, dict) or not all(text(toolchain.get(key)) for key in ('rustc', 'cargo')):
        fail()
    resolved = receipt.get('resolved_cargo')
    if not isinstance(resolved, dict):
        fail()
    for key in ('lock_packages', 'resolved_nodes'):
        items = resolved.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
            fail()
    for package in resolved['lock_packages']:
        if not all(text(package.get(key)) for key in ('name', 'version')):
            fail()
        if any(not text(package[key]) for key in ('source', 'checksum') if key in package):
            fail()
        if 'dependencies' in package and not strings(package['dependencies']):
            fail()
    for node in resolved['resolved_nodes']:
        if not text(node.get('id')) or not strings(node.get('dependencies')) or not strings(node.get('features')):
            fail()
        dependencies = node.get('deps')
        if not isinstance(dependencies, list):
            fail()
        for dependency in dependencies:
            if not isinstance(dependency, dict) or not all(text(dependency.get(key)) for key in ('name', 'pkg')):
                fail()
            kinds = dependency.get('dep_kinds')
            if not isinstance(kinds, list) or any(
                    not isinstance(kind, dict) or any(
                        key not in kind or (kind[key] is not None and not isinstance(kind[key], str))
                        for key in ('kind', 'target')) for kind in kinds):
                fail()


def _kdotool(root, deadline):
    receipt = json.loads(_read(root / 'build.json', deadline, MAX_RECEIPT))
    _validate_receipt(receipt)
    executable = root / 'bin/kdotool'
    observed_hash = hashlib.sha256(_read(executable, deadline, MAX_BINARY)).hexdigest()
    if observed_hash != receipt['binary_sha256']:
        raise _Failure('prerequisite_incompatible', 'binary_digest_mismatch',
                       'Restore the audited kdotool artifact; requalify changed builds before updating policy.', observed_hash)
    if not os.access(executable, os.X_OK):
        raise _Failure('prerequisite_missing', 'not_executable', 'Restore executable permissions on the audited kdotool artifact.')
    version = _run([str(executable), '--version'], deadline)
    if version != 'kdotool v' + KDOTool_RELEASE:
        raise _Failure('prerequisite_incompatible', 'release_mismatch', 'Restore the pinned kdotool release.', version)
    return {'executable': str(executable), 'binary_sha256': observed_hash,
            'revision': receipt['revision'], 'cargo_lock_sha256': receipt['cargo_lock_sha256'],
            'patches': [], 'version': version, 'provenance_verified': True,
            'source_checkout_required': False}


def _libei(deadline):
    observed = hashlib.sha256(_read(LIBEI_PATH, deadline, MAX_BINARY)).hexdigest()
    if platform.machine() != 'x86_64' or observed != LIBEI_SHA256:
        raise _Failure('prerequisite_incompatible', 'unsupported_libei_abi',
                       'Restore the audited x86_64 libei build, or renew the ABI and failed-setup FD ownership audit.',
                       {'architecture': platform.machine(), 'sha256': observed})
    return {'path': str(LIBEI_PATH), 'sha256': observed, 'architecture': 'x86_64',
            'audit_issue': 12, 'compiler_required_at_runtime': False}


def _bindings(deadline):
    result = json.loads(_run([sys.executable, '-I', '-c', BINDINGS_PROBE], deadline))
    origins = result.get('origins', {})
    if (set(origins) != {'gi', 'dbus', 'PIL'} or any(
            not isinstance(path, str) or not path.startswith('/usr/lib/') for path in origins.values())
            or result.get('gio_unix_fd') is not True or result.get('png') is not True):
        raise _Failure('prerequisite_incompatible', 'unsupported_bindings',
                       'Use distribution python-gobject, python-dbus and python-pillow with system-site-packages and no pip overrides.', result)
    return result | {'interpreter': sys.executable, 'isolated': True}


def _executables(deadline):
    result = {}
    for name in RUNTIME_EXECUTABLES:
        _time_left(deadline)
        path = Path('/usr/bin') / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise _Failure('prerequisite_missing', 'missing_executable',
                           'Install the documented KWin 6, D-Bus and systemd runtime packages.', str(path))
        result[name] = str(path)
    kwin = _run(['/usr/bin/kwin_wayland', '--version'], deadline)
    if not re.search(r'\bkwin\s+6\.', kwin, re.IGNORECASE):
        raise _Failure('prerequisite_incompatible', 'unsupported_kwin', 'Use the recorded KDE 6 target.', kwin)
    result['kwin_version'] = kwin
    result['systemd_version'] = _run(['/usr/bin/systemctl', '--version'], deadline).splitlines()[0]
    return result


def _manager(deadline):
    value = _run(['/usr/bin/systemctl', '--user', '--no-ask-password', 'show',
                  'app.slice', '-p', 'ControlGroup', '--value'], deadline, manager=True)
    if not value.startswith('/') or not value.endswith('/app.slice') or '..' in Path(value).parts:
        raise _Failure('prerequisite_incompatible', 'user_cgroup_unavailable',
                       'Start a systemd user session with a unified cgroup hierarchy.', value)
    base = Path('/sys/fs/cgroup' + value)
    events = _read(base / 'cgroup.events', deadline, 4096).decode()
    if not re.search(r'^populated [01]$', events, re.MULTILINE):
        raise _Failure('prerequisite_incompatible', 'invalid_cgroup_events',
                       'Restore readable cgroup v2 state for the systemd user session.')
    return {'cgroup': value, 'observation': 'read_only', 'service_creation': 'not_tested',
            'events_readable': True}


def check(root, deadline):
    """Return report or raise ContractError containing the same full report.

    ``deadline`` is an absolute monotonic deadline shared with startup/doctor.
    Success verifies prerequisites only; it never certifies a desktop capability.
    """
    root = Path(root)
    if not root.is_absolute():
        raise ContractError('invalid_arguments', 'Dependency root must be absolute.', context={'field': 'dependency-root'})
    started = time.monotonic()
    report = {'schema_version': 1, 'scope': 'prerequisites', 'dependency_root': str(root),
              'desktop_ready': False, 'desktop_launched': False, **PROVISIONAL,
              'capabilities': {key: 'not_tested' for key in ('control', 'window_query', 'input_resumed', 'screenshot')},
              'unsupported_operations': [key for key in OPERATIONS if key not in
                                         {'doctor', 'session.start', 'session.status', 'session.stop'}],
              'dependencies': []}
    checks = [('kdotool', lambda: _kdotool(root, deadline)), ('libei', lambda: _libei(deadline)),
              ('python_bindings', lambda: _bindings(deadline)), ('runtime_executables', lambda: _executables(deadline)),
              ('user_manager', lambda: _manager(deadline))]
    for name, probe in checks:
        try:
            _time_left(deadline)
            observed = probe()
            _time_left(deadline)
            item = {'name': name, 'status': 'passed', 'observed': observed, 'code': None, 'reason': None, 'repair': None}
        except _Failure as failure:
            item = {'name': name, 'status': 'failed', 'observed': failure.observed,
                    'code': failure.code, 'reason': failure.reason, 'repair': failure.repair}
        except (OSError, ValueError, TypeError, KeyError) as error:
            missing = isinstance(error, FileNotFoundError)
            item = {'name': name, 'status': 'failed', 'observed': None,
                    'code': 'prerequisite_missing' if missing else 'prerequisite_incompatible',
                    'reason': 'missing_dependency' if missing else 'invalid_dependency_state',
                    'repair': ('Install the documented runtime packages and restore the audited dependency root. '
                               'Build/setup repair is a separate operation; doctor never installs dependencies.')}
        report['dependencies'].append(item)
    report['elapsed_seconds'] = round(time.monotonic() - started, 6)
    failures = [item for item in report['dependencies'] if item['status'] == 'failed']
    report['ok'] = not failures
    if failures:
        first = failures[0]
        raise ContractError(first['code'], 'Runtime prerequisites are unavailable or incompatible.',
                            context={'component': first['name'], 'reason': first['reason'],
                                     'repair': first['repair'], 'prerequisite_report': report})
    report['kdotool'] = report['dependencies'][0]['observed']
    return report


def doctor(request):
    root = os.path.normpath(os.path.join(request.caller_cwd, request.arguments['dependency_root']))
    return check(root, time.monotonic() + request.timeout_seconds)
