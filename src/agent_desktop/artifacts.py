"""Owner-private, bounded generation records independent of disposable runtime.

One nonblocking advisory lock protects read/modify/replace. Request history lives
in separate attempt records, never an ever-growing generation snapshot. Each
call is synchronous on the thread that makes it; in the worker that is the
writer thread (writer.Journal), never the GLib owner. Not a hard latency
guarantee on a hung filesystem.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import time
import uuid

from . import __version__
from .contracts import ContractError, GENERATION, NAME, APP_ID, OPERATIONS, EXIT_CODES, handle

LIMIT = 65536
EVENT_LIMIT = 4096
LAUNCH_LIMIT = 1048576
SUMMARY_APPLICATIONS = 16   # Manifest keeps the most recent; every app keeps its own record.
SUMMARY_ARGV = 1024         # Characters of argv per app in the manifest; launch.json has all of it.
PHASES = frozenset({'admitted', 'started', 'effects', 'cancelling', 'control_admitted',
                    'finalizing', 'terminal', 'startup', 'cleanup', 'transport', 'queue_full'})


def fail(phase='storage'):
    raise ContractError('artifact_failed', 'Durable record could not be preserved.', context={'phase': phase})


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not GENERATION.fullmatch(value):
        fail('identity')
    return value


def packed(value, limit=LIMIT):
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    except (TypeError, ValueError, RecursionError):
        fail('schema')
    if len(raw) > limit:
        fail('size')
    return raw


def check_fd(fd, directory=False):
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail('permissions')



def mkdir_durable(parent, name, *, exclusive=False, durable=False):
    """Persist the parent entry before any descendant can authorize effects.

    durable: this process already made the existing entry durable, so only a new
    link needs the parent fsync.
    """
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        if exclusive:
            raise
        if durable:
            return
    os.fsync(parent)


@contextmanager
def root_directory(path, *, create=False):
    """Anchor each component before opening the next; O_NOFOLLOW on every hop."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ('', '.', '..'):
                fail('root')
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            fail('root')
        yield fd
    finally:
        os.close(fd)


class CaptureRefused(Exception):
    """A capture file that may not be read; the reason is a stable word."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def generation_root(session, generation):
    """The artifact root of SESSION's GENERATION, from its lifecycle record (as session status reads it)."""
    from .lifecycle import read_metadata
    from .runtime import Runtime
    try:
        return Path(read_metadata(Runtime(), session, generation)['configuration']['artifacts'])
    except (OSError, ContractError, KeyError, TypeError, ValueError):
        raise CaptureRefused('unreadable') from None


def open_capture(session, generation, path):
    """A read-only fd for a file of SESSION's GENERATION, inside ROOT/generations/GENERATION.

    For the screenshot a worker reported: `screenshot --output` copies it and the MCP
    server sends it. Opened one component at a time below the root, never following
    a symlink, every directory and the file owner-private (check_fd), the file
    regular and opened O_NONBLOCK so a FIFO cannot block. CaptureRefused otherwise:
    outside_artifacts, not_regular_file or unreadable.
    """
    if (not isinstance(session, str) or not NAME.fullmatch(session) or not isinstance(generation, str)
            or not GENERATION.fullmatch(generation) or not isinstance(path, str) or not os.path.isabs(path)
            or os.path.normpath(path) != path):
        raise CaptureRefused('outside_artifacts')
    root = generation_root(session, generation)
    try:
        parts = Path(path).relative_to(root / 'generations' / generation).parts
    except ValueError:
        raise CaptureRefused('outside_artifacts') from None
    if not parts:
        raise CaptureRefused('outside_artifacts')
    fd = final = None
    try:
        with root_directory(root) as root_fd:
            fd = os.dup(root_fd)
        for part in ('generations', generation, *parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            check_fd(fd, True)
        final = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(final).st_mode):
            raise CaptureRefused('not_regular_file')
        check_fd(final)
        opened, final = final, None
        return opened
    except (OSError, ContractError):
        raise CaptureRefused('unreadable') from None
    finally:
        for descriptor in (fd, final):
            if descriptor is not None:
                os.close(descriptor)


def read_bounded(fd, limit):
    """Up to LIMIT + 1 bytes of FD: more than LIMIT means the file is over the limit."""
    chunks, total = [], 0
    while total <= limit:
        chunk = os.read(fd, limit + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b''.join(chunks)


def actual_path(fd):
    # Linux is the worker's supported platform. procfs reflects renamed/unlinked
    # directory identities, unlike a prior Path.resolve check.
    return Path(os.readlink(f'/proc/self/fd/{fd}'))


def failure_detail(value):
    """Bounded copy of a failure for the manifest and status: code, message, scalar context."""
    try:
        code, message, context = value['code'], value['message'], value.get('context') or {}
    except (TypeError, KeyError):
        fail('schema')
    if not isinstance(code, str) or code not in EXIT_CODES or not isinstance(message, str) or not isinstance(context, dict):
        fail('schema')
    kept = {key: item for key, item in list(context.items())[:16]
            if isinstance(key, str) and len(key) <= 64
            and (item is None or type(item) in (bool, int, float) or isinstance(item, str) and len(item) <= 256)}
    return {'code': code, 'message': message[:512] or code, 'context': kept}


def record_layout():
    return {'requests': 'requests', 'applications': 'applications', 'events': 'events.jsonl',
            'logs': {source: f'logs/{source}.log' for source in ('worker', 'compositor', 'bus')}}


def safe_projection(value, generation):
    """No generic error/result dictionary is a safe diagnostic record."""
    out = {}
    if not isinstance(value, dict):
        return out
    for key, kind in (('app', 'app'), ('application', 'app'), ('window', 'window')):
        candidate = value.get(key)
        # Bound strings before regex/copy/encoding; do not walk arbitrary objects.
        if isinstance(candidate, str) and len(candidate) > 4096:
            continue
        if isinstance(candidate, dict) and (len(candidate) > 2 or any(not isinstance(v, str) or len(v) > 4096 for v in candidate.values())):
            continue
        try:
            parsed = handle(candidate, kind)
            if parsed['generation'] == generation:
                out[key] = parsed
        except ContractError:
            pass
    process = value.get('process')
    if isinstance(process, dict):
        identity = {key: process[key] for key in ('pid', 'start_time_ticks')
                    if type(process.get(key)) is int and 0 < process[key] < 2 ** 63}
        if len(identity) == 2:
            out['process'] = identity
    windows = value.get('windows')
    if isinstance(windows, list) and len(windows) <= 256:
        out['windows'] = [ref['window'] for window in windows[:64]
                          if (ref := safe_projection({'window': window.get('window', window) if isinstance(window, dict) else window}, generation)).get('window')]
        if len(windows) > 64:
            out['windows_omitted'] = len(windows) - 64
    query = value.get('query_artifact')
    if isinstance(query, str) and re.fullmatch(r'window-observations/[0-9a-f]{32}\.json', query):
        out['query_artifact'] = query
    phase = value.get('phase')
    if isinstance(phase, str) and phase in PHASES:
        out['phase'] = phase
    close = value.get('close_state')
    if isinstance(close, dict):
        typed = {}
        for key, choices in (('phase', ('resolve', 'close_request', 'exit_wait', 'cleanup')),
                             ('dispatch', ('not_started', 'uncertain', 'transport_completed'))):
            if type(close.get(key)) is str and close[key] in choices:
                typed[key] = close[key]
        native = close.get('native_operation_id')
        if native is None or (type(native) is str and re.fullmatch(r'[0-9a-f]{32}', native)):
            typed['native_operation_id'] = native
        for key in ('transport_completed_at', 'exit_observed_at'):
            item = close.get(key)
            if item is None or (type(item) in (int, float) and 0 <= item < 2 ** 53 and math.isfinite(item)):
                typed[key] = item
        out['close_state'] = typed
    state = value.get('process_state')
    if isinstance(state, dict):
        typed = {}
        for key in ('root_reaped', 'subtree_populated', 'all_exited'):
            if state.get(key) is None or type(state[key]) is bool:
                typed[key] = state.get(key)
        code = state.get('root_returncode')
        if code is None or (type(code) is int and -(2 ** 31) <= code < 2 ** 31):
            typed['root_returncode'] = code
        observed = state.get('observed_at')
        if observed is None or (type(observed) in (int, float) and 0 <= observed < 2 ** 53 and math.isfinite(observed)):
            typed['observed_at'] = observed
        if state.get('remaining_processes') is None:
            typed['remaining_processes'] = None
        if state.get('enumeration') == 'unavailable':
            typed['enumeration'] = 'unavailable'
        out['process_state'] = typed
    kill = value.get('kill_state')
    if isinstance(kill, dict):
        typed = {}
        if type(kill.get('phase')) is str and kill['phase'] in ('resolve', 'term', 'kill', 'observe'):
            typed['phase'] = kill['phase']
        for key in ('intent', 'dispatch_revoked', 'enumeration_incomplete', 'sample_truncated'):
            if type(kill.get(key)) is bool:
                typed[key] = kill[key]
        for key in ('started_at', 'term_cutoff', 'signal_cutoff', 'deadline', 'phase_started_at',
                    'last_signal_at', 'exit_observed_at'):
            item = kill.get(key)
            if item is None or (type(item) in (int, float) and 0 <= item < 2 ** 53 and math.isfinite(item)):
                typed[key] = item
        counts = kill.get('counts')
        if isinstance(counts, dict):
            typed['counts'] = {key: counts[key] for key in ('term_attempted', 'term_submitted',
                'kill_attempted', 'kill_submitted', 'raced_exit')
                if type(counts.get(key)) is int and 0 <= counts[key] <= 8192}
        def sample(item):
            if not isinstance(item, dict):
                return None
            ident = {key: item[key] for key in ('pid', 'start_time_ticks')
                     if type(item.get(key)) is int and 0 < item[key] < 2 ** 63}
            when = item.get('observed_at')
            if len(ident) != 2 or type(when) not in (int, float) or not 0 <= when < 2 ** 53 or not math.isfinite(when):
                return None
            return ident | {'observed_at': when}
        remaining = kill.get('remaining_processes')
        if remaining is None:
            typed['remaining_processes'] = None
        elif isinstance(remaining, list) and len(remaining) <= 64:
            typed['remaining_processes'] = [row for item in remaining if (row := sample(item)) is not None]
        if kill.get('enumeration') in ('complete', 'sampled', 'unavailable'):
            typed['enumeration'] = kill['enumeration']
        foreign = sample(kill.get('ownership_uncertain'))
        typed['ownership_uncertain'] = foreign
        out['kill_state'] = typed
        for key in ('exited', 'already_exited'):
            if value.get(key) is None or type(value[key]) is bool:
                out[key] = value.get(key)
        for key in ('exit_status', 'root_returncode'):
            code = value.get(key)
            if code is None or (type(code) is int and -(2 ** 31) <= code < 2 ** 31):
                out[key] = code
        snapshot = value.get('application_snapshot')
        if isinstance(snapshot, dict):
            # Fixed-shape references only; never persist the arbitrary snapshot
            # or recursively copy its window payloads/environment/path values.
            retained = safe_projection({key: snapshot.get(key) for key in ('application', 'process')}, generation)
            if snapshot.get('state') in ('prepared', 'execution-authorized', 'running', 'root-exited',
                                          'all-exited', 'launch-failed'):
                retained['state'] = snapshot['state']
            code = snapshot.get('exit_code')
            if code is None or (type(code) is int and -(2 ** 31) <= code < 2 ** 31):
                retained['exit_code'] = code
            for name in ('window_observation', 'previous_observation', 'pending_observation'):
                observation = snapshot.get(name)
                if isinstance(observation, dict):
                    retained[name] = safe_projection({'query_artifact': observation.get('query_artifact')}, generation)
            out['application_snapshot'] = retained
    return out


class Store:
    def __init__(self, root, session, generation, *, create=False, disposable=()):
        if not isinstance(session, str) or not NAME.fullmatch(session):
            fail('identity')
        identifier(generation)
        root = Path(root)
        if not root.is_absolute() or os.path.normpath(str(root)) != str(root):
            fail('root')
        canonical = root.resolve()
        for raw in disposable:
            other = Path(raw).resolve()
            if canonical.is_relative_to(other) or other.is_relative_to(canonical):
                fail('disposable_root')
        self.root, self.session, self.generation = root, session, generation
        self.path = root / 'generations' / generation
        self.fd = None
        self.lock_identity = None
        self.observations_durable = False
        self.disposable = tuple(Path(raw).resolve() for raw in disposable)
        try:
            with root_directory(root, create=create) as root_fd:
                if actual_path(root_fd) != root:
                    fail('root_moved')
                if create:
                    mkdir_durable(root_fd, 'generations')
                parent = os.open('generations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    check_fd(parent, True)
                    # Recheck the opened identity after a possible ancestor swap.
                    if actual_path(root_fd) != root or actual_path(parent) != root / 'generations':
                        fail('root_moved')
                    if create:
                        mkdir_durable(parent, generation, exclusive=True)
                    self.fd = os.open(generation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    check_fd(self.fd, True)
                    self._location()
                finally:
                    os.close(parent)
            if create:
                for name in ('requests', 'applications', 'logs'):
                    mkdir_durable(self.fd, name, exclusive=True)
                self._initialize_file(self.fd, 'record.lock')
                self._initialize_file(self.fd, 'events.jsonl')
                self._attach_lock()
                with self.directory('logs') as logs:
                    for name in ('worker', 'compositor', 'bus'):
                        self._initialize_file(logs, name + '.log')
                # Manifest publication is the final initialization step.
                with self.lock():
                    self._write(self.fd, 'manifest.json', {
                        'schema_version': 1, 'revision': 0, 'session': session, 'generation': generation,
                        'created_at': timestamp(), 'updated_at': timestamp(), 'mode': 'headless',
                        'state': 'starting', 'outcome': 'pending', 'first_failure': None,
                        'cleanup': {'state': 'not_started', 'failure': None},
                        'output': {'state': 'not_collected', 'requested': {'width': 1280, 'height': 720, 'scale': 1},
                                   'observed': None, 'producer': 'M3 startup'},
                        'dependencies': {'state': 'partial', 'observed': {'agent_desktop': __version__,
                            'python': platform.python_version()}, 'native': 'not_collected', 'producer': 'M3/doctor'},
                        'process': {'state': 'not_collected', 'producer': 'supervisor'},
                        'records': record_layout(),
                    })
            else:
                self._attach_lock()
                self._validate_layout()
                self.read()
        except (OSError, ContractError):
            self.close()
            fail('open')

    def _location(self):
        actual = actual_path(self.fd)
        if actual != self.path:
            fail('root_moved')
        for other in self.disposable:
            if actual.is_relative_to(other) or other.is_relative_to(self.root):
                fail('disposable_root')

    def _initialize_file(self, directory, name):
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            check_fd(fd)
            os.fsync(fd)
            os.fsync(directory)
        finally:
            os.close(fd)

    def _attach_lock(self):
        fd = os.open('record.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            check_fd(fd)
            info = os.fstat(fd)
            self.lock_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)

    def _validate_layout(self):
        # Attachment never repairs a partially initialized/damaged generation.
        with self.lock():
            for name in ('requests', 'applications', 'logs'):
                with self.directory(name):
                    pass
            for directory_parts, names in (((), ('events.jsonl',)), (('logs',), ('worker.log', 'compositor.log', 'bus.log'))):
                with self.directory(*directory_parts) as directory:
                    for name in names:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        try:
                            check_fd(fd)
                        finally:
                            os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def directory(self, *parts):
        fd = os.dup(self.fd)
        try:
            for part in parts:
                if '/' in part or part in ('', '.', '..'):
                    fail('path')
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
                check_fd(fd, True)
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def lock(self):
        fd = None
        try:
            self._location()
            fd = os.open('record.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=self.fd)
            check_fd(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.lock_identity:
                fail('lock_replaced')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except OSError:
            fail('storage')
        finally:
            if fd is not None:
                os.close(fd)

    def _read(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            check_fd(fd)
            raw = os.read(fd, LIMIT + 1)
            if len(raw) > LIMIT:
                fail('size')
            try:
                from .protocol import decode
                value = decode(raw)
            except (ValueError, RecursionError):
                fail('schema')
            if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1
                    or value.get('generation') != self.generation or value.get('session') != self.session
                    or type(value.get('revision')) is not int or value['revision'] < 0):
                fail('identity')
            if name == 'manifest.json':
                self._validate_manifest(value)
            return value
        finally:
            os.close(fd)

    def _validate_manifest(self, value):
        required = {'schema_version', 'revision', 'session', 'generation', 'created_at', 'updated_at',
                    'mode', 'state', 'outcome', 'first_failure', 'cleanup', 'output',
                    'dependencies', 'process', 'records'}
        optional = {'failure', 'applications', 'applications_omitted'}
        if (not required <= set(value) or set(value) - required - optional
                or value['mode'] != 'headless' or value['records'] != record_layout()):
            fail('schema')
        failure = value.get('failure')
        if failure is not None and (not isinstance(failure, dict) or set(failure) != {'code', 'message', 'context'}
                or not isinstance(failure['code'], str) or failure['code'] not in EXIT_CODES
                or not isinstance(failure['message'], str)
                or not 0 < len(failure['message']) <= 512 or not isinstance(failure['context'], dict)):
            fail('schema')
        applications = value.get('applications', [])
        if (not isinstance(applications, list) or len(applications) > SUMMARY_APPLICATIONS
                or any(not isinstance(app, dict) for app in applications)
                or type(value.get('applications_omitted', 0)) is not int):
            fail('schema')
        def member(candidate, allowed):
            return isinstance(candidate, str) and candidate in allowed
        def code(candidate):
            return candidate is None or member(candidate, EXIT_CODES)
        def date(candidate):
            try:
                return isinstance(candidate, str) and datetime.fromisoformat(candidate).utcoffset().total_seconds() == 0
            except (ValueError, AttributeError):
                return False
        if (not all(date(value[key]) for key in ('created_at', 'updated_at'))
                or not member(value['state'], {'starting', 'running', 'ready', 'stopping', 'stopped', 'failed'})
                or not member(value['outcome'], {'pending', 'stopped', 'failed'}) or not code(value['first_failure'])):
            fail('schema')
        cleanup = value['cleanup']
        if (not isinstance(cleanup, dict) or set(cleanup) != {'state', 'failure'}
                or not member(cleanup['state'], {'not_started', 'in_progress', 'complete', 'uncertain'})
                or not code(cleanup['failure'])):
            fail('schema')
        def geometry(candidate):
            return (isinstance(candidate, dict) and set(candidate) == {'width', 'height', 'scale'}
                    and all(type(v) is int and 0 < v <= 65536 for v in candidate.values()))
        output = value['output']
        if (not isinstance(output, dict) or set(output) != {'state', 'requested', 'observed', 'producer'}
                or not member(output['state'], {'not_collected', 'collected'}) or output['producer'] != 'M3 startup'
                or not geometry(output['requested'])
                or (output['state'] == 'collected' and not geometry(output['observed']))
                or (output['state'] == 'not_collected' and output['observed'] is not None)):
            fail('schema')
        dependencies = value['dependencies']
        if (not isinstance(dependencies, dict)
                or not {'state', 'observed', 'native', 'producer'} <= dependencies.keys()
                or set(dependencies) - {'state', 'observed', 'native', 'producer', 'inventory'}
                or dependencies['state'] != 'partial' or dependencies['producer'] != 'M3/doctor'
                or not member(dependencies['native'], {'not_collected', 'supplied_inventory'})
                or not isinstance(dependencies['observed'], dict)
                or set(dependencies['observed']) != {'agent_desktop', 'python'}
                or any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in dependencies['observed'].values())):
            fail('schema')
        inventory = dependencies.get('inventory', [])
        if not isinstance(inventory, list) or len(inventory) > 128:
            fail('schema')
        for entry in inventory:
            if (not isinstance(entry, dict) or not {'component', 'version'} <= entry.keys()
                    or set(entry) - {'component', 'version', 'source_revision', 'sha256', 'patches'}
                    or any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in entry.values())):
                fail('schema')
        process = value['process']
        if not isinstance(process, dict):
            fail('schema')
        if process.get('state') == 'not_collected':
            if process != {'state': 'not_collected', 'producer': 'supervisor'}:
                fail('schema')
        elif (set(process) != {'state', 'producer', 'pid', 'start_ticks', 'service'}
                or process.get('state') != 'collected' or process.get('producer') != 'worker'
                or type(process.get('pid')) is not int or process['pid'] <= 0
                or not isinstance(process.get('start_ticks'), str) or not process['start_ticks'].isdigit()
                or not (process['service'] == 'not_collected' or process['service'] ==
                        'agent-desktop-' + self.generation + '.service')):
            fail('schema')

    def _write(self, directory, name, value, limit=LIMIT):
        raw = packed(value, limit)
        temp = '.' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Replacing a symlink cannot follow it, but reject it instead of deleting it.
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    fail('permissions')
            except FileNotFoundError:
                pass
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass

    def read(self):
        with self.lock():
            return self._read(self.fd, 'manifest.json')

    def generation_update(self, *, state=None, failure=None, cleanup=None, detail=None, replace_detail=False):
        """DETAIL: the first failure's {code, message, context}; only the first is kept
        unless REPLACE_DETAIL (a symptom recorded first, then its root cause)."""
        if state not in (None, 'running', 'ready', 'stopping', 'stopped', 'failed') or failure not in (None, *EXIT_CODES):
            fail('schema')
        if detail is not None:
            detail = failure_detail(detail)
        if cleanup not in (None, 'in_progress', 'complete', 'uncertain'):
            fail('schema')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            if failure:
                value['first_failure'] = value['first_failure'] or failure
            if detail is not None and (replace_detail or value.get('failure') is None):
                value['failure'] = detail
            terminal = value['state'] in ('stopped', 'failed')
            if state and not (terminal and state in ('running', 'ready', 'stopping')):
                value['state'] = 'failed' if value['first_failure'] else state
            if cleanup:
                value['cleanup'] = {'state': cleanup, 'failure': failure or value['cleanup']['failure']}
            if value['first_failure']:
                value['outcome'] = 'failed'
            elif value['state'] == 'stopped':
                value['outcome'] = 'stopped'
            value['revision'] += 1
            value['updated_at'] = timestamp()
            self._write(self.fd, 'manifest.json', value)

    def request(self, request, admitted_at, deadline):
        identifier(request.request_id)
        if (request.operation not in OPERATIONS or type(admitted_at) not in (int, float)
                or type(deadline) not in (int, float) or deadline < admitted_at):
            fail('schema')
        if request.session != self.session or request.expected_generation != self.generation:
            fail('identity')
        with self.lock(), self.directory('requests') as parent:
            mkdir_durable(parent, request.request_id)
            with self.directory('requests', request.request_id) as group:
                for _ in range(8):
                    attempt = uuid.uuid4().hex
                    try:
                        mkdir_durable(group, attempt, exclusive=True)
                        break
                    except FileExistsError:
                        continue
                else:
                    fail('collision')
            with self.directory('requests', request.request_id, attempt) as directory:
                self._write(directory, 'record.json', {'schema_version': 1, 'revision': 0,
                    'session': self.session, 'generation': self.generation,
                    'request_id': request.request_id, 'attempt': attempt, 'operation': request.operation,
                    'admitted_at': admitted_at, 'deadline': deadline, 'created_at': timestamp(),
                    'phase': 'admitted', 'outcome': 'pending', 'references': {}, 'error_code': None,
                    'started_at': None, 'started_monotonic': None,
                    'terminal_observed_at': None, 'terminal_observed_monotonic': None})
        return (request.request_id, attempt)

    def transition(self, token, event, *, outcome='pending', error_code=None, references=None,
                   observed_at=None, observed_monotonic=None):
        observed_at = timestamp() if observed_at is None else observed_at
        observed_monotonic = time.monotonic() if observed_monotonic is None else observed_monotonic
        if event not in PHASES or outcome not in ('pending', 'success', 'not_started', 'partial', 'unknown'):
            fail('schema')
        if error_code not in (None, *EXIT_CODES):
            fail('schema')
        if outcome == 'success' and event != 'terminal':
            fail('premature_success')
        request_id, attempt = map(identifier, token)
        with self.lock(), self.directory('requests', request_id, attempt) as directory:
            value = self._read(directory, 'record.json')
            if value['request_id'] != request_id or value['attempt'] != attempt:
                fail('identity')
            if value['phase'] == 'terminal':
                return
            if event == 'started' and value['started_at'] is None:
                value['started_at'], value['started_monotonic'] = observed_at, observed_monotonic
            if event == 'terminal':
                value['terminal_observed_at'], value['terminal_observed_monotonic'] = observed_at, observed_monotonic
            value.update(phase=event, outcome=outcome, error_code=error_code,
                         references=value["references"] | self.references(references), updated_at=timestamp())
            value['revision'] += 1
            self._write(directory, 'record.json', value)

    def references(self, value):
        result = safe_projection(value, self.generation)
        if not isinstance(value, dict):
            return result
        def owned_path(raw):
            if not isinstance(raw, str) or len(raw) > 4096:
                return None
            path = Path(raw)
            try:
                parts = path.relative_to(self.path).parts
                if not parts or any(part in ('.', '..') for part in parts):
                    return None
                with self.directory(*parts[:-1]) as directory:
                    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        check_fd(fd)
                    finally:
                        os.close(fd)
                return str(path)
            except (ValueError, OSError, ContractError):
                return None
        if 'query_artifact' in result:
            if owned_path(str(self.path / result['query_artifact'])) is None:
                del result['query_artifact']
        artifacts = value.get('artifacts')
        if isinstance(artifacts, list) and len(artifacts) <= 64:
            result['artifacts'] = [path for item in artifacts if (path := owned_path(item)) is not None]
        for name in ('log_paths', 'logs'):
            logs = value.get(name)
            if isinstance(logs, dict):
                result[name] = {key: path for key in ('stdout', 'stderr')
                                if (path := owned_path(logs.get(key))) is not None}
        return result

    def event(self, token, phase):
        if phase not in PHASES:
            fail('schema')
        raw = packed({'generation': self.generation, 'request_id': identifier(token[0]),
                      'attempt': identifier(token[1]), 'phase': phase, 'at': timestamp()}, EVENT_LIMIT) + b'\n'
        with self.lock():
            fd = os.open('events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=self.fd)
            try:
                check_fd(fd)
                if os.write(fd, raw) != len(raw):
                    fail('event_write')
            finally:
                os.close(fd)

    def open_log(self, source):
        if source not in ('worker', 'compositor', 'bus'):
            fail('schema')
        # Opening an existing append-only log is not an update, so it takes no
        # record lock: the owner opens logs while the writer thread holds it.
        self._location()
        with self.directory('logs') as directory:
            fd = os.open(source + '.log', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
            try:
                check_fd(fd)
                return os.fdopen(fd, 'a', buffering=1)
            except BaseException:
                os.close(fd)
                raise

    def log_path(self, source):
        if source not in ('worker', 'compositor', 'bus'):
            fail('schema')
        self._location()
        with self.directory('logs') as directory:
            fd = os.open(source + '.log', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
            try:
                check_fd(fd)
            finally:
                os.close(fd)
        return self.path / 'logs' / (source + '.log')

    def allocate(self, token, kind):
        if kind not in ('capture', 'stdout', 'stderr', 'diagnostic'):
            fail('schema')
        request_id, attempt = map(identifier, token)
        with self.lock(), self.directory('requests', request_id, attempt) as directory:
            for _ in range(8):
                allocation = uuid.uuid4().hex
                # Logs keep one name for their whole life, so paths returned by
                # launch never go stale; the allocation record says when they're complete.
                name = allocation + '.' + kind + ('.log' if kind in ('stdout', 'stderr') else '.partial')
                try:
                    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    os.close(fd)
                    os.fsync(directory)
                    self._write(directory, allocation + '.json', {'schema_version': 1, 'revision': 0,
                        'session': self.session, 'generation': self.generation, 'request_id': request_id,
                        'attempt': attempt, 'allocation': allocation, 'kind': kind, 'filename': name,
                        'state': 'allocated', 'metadata': {}})
                    return self.path / 'requests' / request_id / attempt / name
                except FileExistsError:
                    continue
            fail('collision')

    def launch(self, token, argv, cwd):
        """One write, exact argv; credentials belong in env/files, never in argv."""
        if (not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\0' in a for a in argv)
                or not isinstance(cwd, str) or not os.path.isabs(cwd)):
            fail('schema')
        request_id, attempt = map(identifier, token)
        value = {'argv': argv, 'cwd': cwd, 'generation': self.generation,
                 'request_id': request_id, 'attempt': attempt}
        packed(value, LAUNCH_LIMIT)
        with self.lock(), self.directory('requests', request_id, attempt) as directory:
            try:
                os.stat('launch.json', dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                self._write(directory, 'launch.json', value, LAUNCH_LIMIT)
            else:
                fail('already_recorded')

    def artifact_state(self, path, state, *, width=None, height=None):
        """An adapter marks complete only after its own full publication checks."""
        if state not in ('partial', 'complete', 'failed'):
            fail('schema')
        try:
            parts = Path(path).relative_to(self.path / 'requests').parts
            request_id, attempt, name = parts
            allocation = identifier(name.split('.')[0])
            identifier(request_id)
            identifier(attempt)
        except (ValueError, TypeError):
            fail('path')
        metadata = {}
        for key, number in (('width', width), ('height', height)):
            if number is not None:
                if type(number) is not int or not 0 < number <= 65536:
                    fail('schema')
                metadata[key] = number
        with self.lock(), self.directory('requests', request_id, attempt) as directory:
            value = self._read(directory, allocation + '.json')
            if value['filename'] != name:
                fail('identity')
            if value['state'] in ('complete', 'failed'):
                return
            value.update(state=state, metadata=metadata, revision=value['revision'] + 1)
            self._write(directory, allocation + '.json', value)

    def application(self, token, application_id, *, pid, birth_identity, executable, logs, windows=()):
        """M4 supplies verified process birth identity; a PID alone is not authority."""
        request_id, attempt = map(identifier, token)
        if (not isinstance(application_id, str) or len(application_id) > 128 or not APP_ID.fullmatch(application_id)
                or type(pid) is not int or pid <= 0 or not isinstance(birth_identity, str)
                or not 0 < len(birth_identity) <= 256 or not isinstance(executable, str)
                or not os.path.isabs(executable) or len(executable) > 4096 or len(windows) > 64):
            fail('schema')
        validated_windows = [handle(window, 'window') for window in windows]
        if any(window['generation'] != self.generation for window in validated_windows):
            fail('identity')
        if not isinstance(logs, dict) or set(logs) - {'stdout', 'stderr'}:
            fail('schema')
        if self.references({'log_paths': logs}).get('log_paths') != logs:
            fail('path')
        value = {'schema_version': 1, 'revision': 0, 'session': self.session, 'generation': self.generation,
                 'application_id': application_id, 'request_id': request_id, 'attempt': attempt,
                 'launch_record': f'requests/{request_id}/{attempt}/launch.json',
                 'process': {'pid': pid, 'birth_identity': birth_identity}, 'executable': executable,
                 'logs': logs, 'windows': validated_windows, 'exit': {'state': 'not_observed'}}
        with self.lock(), self.directory('applications') as parent:
            mkdir_durable(parent, application_id, exclusive=True)
            with self.directory('applications', application_id) as directory:
                self._write(directory, 'record.json', value)
            os.fsync(parent)

    def application_prepare(self, token, application_id, *, executable, logs, cgroup):
        request_id, attempt = map(identifier, token)
        identifier(application_id)
        if (not isinstance(executable, str) or not os.path.isabs(executable)
                or self.references({'logs': logs}).get('logs') != logs
                or not isinstance(cgroup, str) or not cgroup.endswith('/applications/' + application_id)):
            fail('schema')
        value = {'schema_version': 1, 'revision': 0, 'session': self.session, 'generation': self.generation,
                 'application_id': application_id, 'request_id': request_id, 'attempt': attempt,
                 'launch_record': f'requests/{request_id}/{attempt}/launch.json', 'process': None,
                 'executable': executable, 'logs': logs, 'windows': [], 'owner': 'cgroup-v2',
                 'cgroup': cgroup, 'state': 'prepared', 'authorized': False, 'uncertain': False,
                 'exit_code': None}
        with self.lock(), self.directory('applications') as parent:
            mkdir_durable(parent, application_id, exclusive=True)
            with self.directory('applications', application_id) as directory:
                mkdir_durable(directory, 'processes', exclusive=True)
                self._write(directory, 'record.json', value)

    def application_update(self, application_id, *, state, process, exit_code, authorized, uncertain):
        identifier(application_id)
        if (state not in ('prepared', 'execution-authorized', 'running', 'root-exited', 'all-exited', 'launch-failed')
                or type(authorized) is not bool or type(uncertain) is not bool
                or (exit_code is not None and type(exit_code) is not int)):
            fail('schema')
        if process is not None and (safe_projection({'process': process}, self.generation).get('process') !=
                {k: process.get(k) for k in ('pid', 'start_time_ticks')}):
            fail('identity')
        with self.lock(), self.directory('applications', application_id) as directory:
            value = self._read(directory, 'record.json')
            value.update(state=state, process=process, exit_code=exit_code, authorized=authorized,
                         uncertain=uncertain, revision=value['revision'] + 1, updated_at=timestamp())
            self._write(directory, 'record.json', value)

    def application_read(self, application_id):
        identifier(application_id)
        try:
            with self.directory('applications', application_id) as directory:
                value = self._read(directory, 'record.json')
        except FileNotFoundError:
            raise ContractError('target_not_found', 'Application was not found.') from None
        except OSError:
            fail('application_read')
        if (value.get('application_id') != application_id or value.get('owner') != 'cgroup-v2'
                or value.get('state') not in ('prepared', 'execution-authorized', 'running', 'root-exited', 'all-exited', 'launch-failed')
                or type(value.get('authorized')) is not bool or type(value.get('uncertain')) is not bool
                or not isinstance(value.get('executable'), str) or not os.path.isabs(value['executable'])
                or not isinstance(value.get('logs'), dict) or set(value['logs']) != {'stdout', 'stderr'}
                or self.references({'logs': value.get('logs')}).get('logs') != value.get('logs')
                or not isinstance(value.get('cgroup'), str) or not value['cgroup'].endswith('/applications/' + application_id)
                or not isinstance(value.get('windows'), list) or len(value['windows']) > 256):
            fail('application_schema')
        process = value.get('process')
        if process is not None and (not isinstance(process, dict) or
                safe_projection({'process': process}, self.generation).get('process') !=
                {k: process.get(k) for k in ('pid', 'start_time_ticks')}):
            fail('application_schema')
        for ref in value['windows']:
            self._window_handle(ref)
        publication = value.get('window_publication')
        if publication not in (None, 'pending', 'confirmed'):
            fail('window_reference')
        for key in ('window_observation', 'previous_observation', 'candidate_observation'):
            observation = value.get(key)
            if observation is not None:
                if not isinstance(observation, dict) or publication is None:
                    fail('window_reference')
                # Exactly two sets of references are ever stored: the confirmed
                # top-level list and either previous or proposed references.
                compact = (key == 'window_observation' or
                           key == ('previous_observation' if publication == 'pending' else 'candidate_observation'))
                if compact:
                    if 'windows' in observation:
                        fail('window_reference')
                    observation = observation | {'windows': value['windows']}
                self._window_reference(observation)
                value[key] = observation
        return {'application': {'generation': self.generation, 'application_id': application_id},
                **{key: value.get(key) for key in ('process', 'executable', 'logs', 'state', 'exit_code', 'windows',
                   'window_observation', 'previous_observation')},
                'pending_observation': value.get('candidate_observation') if value.get('window_publication') != 'confirmed' else None}

    def _window_handle(self, ref):
        try:
            parsed = handle(ref, 'window')
        except ContractError:
            fail('window_reference')
        if parsed['generation'] != self.generation:
            fail('identity')
        return parsed

    def _window_reference(self, observation):
        if (not isinstance(observation, dict) or set(observation) != {'query_artifact', 'query_id', 'observed_at', 'accepted_at', 'windows'}
                or not isinstance(observation['query_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', observation['query_id'])
                or observation['query_artifact'] != 'window-observations/' + observation['query_id'] + '.json'
                or any(type(observation[k]) not in (int, float) or not 0 < observation[k] < float('inf') for k in ('observed_at', 'accepted_at'))
                or not isinstance(observation['windows'], list) or len(observation['windows']) > 256):
            fail('window_reference')
        for ref in observation['windows']:
            self._window_handle(ref)

    def _application_window_value(self, value, previous, candidate, state):
        def pointer(observation):
            return None if observation is None else {k: v for k, v in observation.items() if k != 'windows'}
        confirmed = candidate if state == 'confirmed' else previous
        return value | {
            'previous_observation': previous if state == 'confirmed' else pointer(previous),
            'candidate_observation': pointer(candidate) if state == 'confirmed' else candidate,
            'window_observation': pointer(confirmed), 'window_publication': state,
            'windows': [] if confirmed is None else confirmed['windows'],
            'revision': value['revision'] + 1, 'updated_at': timestamp()}

    def application_windows(self, application_id, previous, candidate, state):
        identifier(application_id)
        if state not in ('pending', 'confirmed'):
            fail('schema')
        for observation in (previous, candidate):
            if observation is not None:
                self._window_reference(observation)
        with self.lock(), self.directory('applications', application_id) as directory:
            value = self._read(directory, 'record.json')
            # A pending success must not authorize an oversized deferred checkpoint.
            pending = self._application_window_value(value, previous, candidate, 'pending')
            confirmed = self._application_window_value(value, previous, candidate, 'confirmed')
            packed(pending)
            packed(confirmed)
            self._write(directory, 'record.json', pending if state == 'pending' else confirmed)

    def window_observation(self, query_id, value):
        identifier(query_id)
        # Exclusive immutable publication; an existing name is never overwritten.
        raw = packed(value, 1024 * 1024)
        with self.lock():
            # The first publication fsyncs the generation directory; later ones only a recreated link.
            # Cleared first: a recreation whose fsync fails leaves a link that is not yet durable.
            durable, self.observations_durable = self.observations_durable, False
            mkdir_durable(self.fd, 'window-observations', durable=durable)
            self.observations_durable = True
            with self.directory('window-observations') as directory:
                fd = os.open(query_id + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
        return 'window-observations/' + query_id + '.json'

    def application_processes(self, application_id, batch):
        identifier(application_id)
        if len(batch) > 16:
            fail('size')
        with self.lock(), self.directory('applications', application_id, 'processes') as directory:
            for process in batch:
                projected = safe_projection({'process': process}, self.generation).get('process')
                if projected is None or not isinstance(process.get('boot_id'), str):
                    fail('identity')
                name = str(projected['pid']) + '-' + str(projected['start_time_ticks']) + '.json'
                self._write(directory, name, {'schema_version': 1, 'revision': 0,
                    'generation': self.generation, 'session': self.session,
                    'process': process, 'exit_status': 'not_observed'})

    def provenance(self, *, output=None, dependencies=()):
        """Trusted real collectors provide observations, never copied baseline claims."""
        if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 128:
            fail('schema')
        entries = []
        for entry in dependencies:
            allowed = {'component', 'version', 'source_revision', 'sha256', 'patches'}
            if (not isinstance(entry, dict) or set(entry) - allowed or not {'component', 'version'} <= entry.keys()
                    or any(not isinstance(v, str) or not v or len(v) > 256 for v in entry.values())):
                fail('schema')
            entries.append(dict(entry))
        if output is not None and (not isinstance(output, dict) or set(output) != {'width', 'height', 'scale'}
                or any(type(v) is not int or not 0 < v <= 65536 for v in output.values())):
            fail('schema')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            if output is not None:
                value['output']['observed'] = dict(output)
                value['output']['state'] = 'collected'
            if entries:
                value['dependencies']['inventory'] = entries
                # An inventory does not certify completeness/native support.
                value['dependencies']['native'] = 'supplied_inventory'
            value['revision'] += 1
            value['updated_at'] = timestamp()
            self._write(self.fd, 'manifest.json', value)

    def open_owned(self, path):
        """Read-only fd for a file under this generation, opened component by component."""
        try:
            parts = Path(path).relative_to(self.path).parts
        except (TypeError, ValueError):
            fail('path')
        if not parts:
            fail('path')
        with self.directory(*parts[:-1]) as directory:
            if '/' in parts[-1] or parts[-1] in ('.', '..'):
                fail('path')
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)

    def _read_launch(self, directory, request_id, attempt):
        fd = os.open('launch.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            check_fd(fd)
            raw = os.read(fd, LAUNCH_LIMIT + 1)
        finally:
            os.close(fd)
        from .protocol import decode
        try:
            value = decode(raw) if len(raw) <= LAUNCH_LIMIT else None
        except (ValueError, RecursionError):
            value = None
        if (not isinstance(value, dict) or value.get('generation') != self.generation
                or value.get('request_id') != request_id or value.get('attempt') != attempt
                or not isinstance(value.get('argv'), list) or not all(isinstance(a, str) for a in value['argv'])):
            fail('launch_record')
        return value

    def applications_by_launch(self, limit=SUMMARY_APPLICATIONS):
        """Summary rows of the LIMIT most recently updated applications, oldest launch first,
        and how many others were left out. Reads at most LIMIT records."""
        rows = []
        try:
            with self.directory('applications') as parent:
                found = []
                for entry in os.scandir(parent):
                    if APP_ID.fullmatch(entry.name):
                        try:
                            found.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
                        except OSError:
                            continue
        except FileNotFoundError:
            found = []
        found.sort(reverse=True)
        names = [name for _, name in found[:limit]]
        self.applications_skipped = max(0, len(found) - limit)
        for name in names:
            try:
                record = self.application_read(name)  # Validates; the raw record has the request.
                with self.directory('applications', name) as directory:
                    raw = self._read(directory, 'record.json')
                request_id, attempt = identifier(raw['request_id']), identifier(raw['attempt'])
                with self.directory('requests', request_id, attempt) as directory:
                    launch = self._read_launch(directory, request_id, attempt)
                    launched_at = self._read(directory, 'record.json').get('created_at')
            except (ContractError, OSError, KeyError):
                continue
            argv, shown, size = launch.get('argv') or [], [], 0
            for arg in argv:
                size += len(json.dumps(arg))  # Serialized size: non-ASCII becomes \uXXXX.
                if size > SUMMARY_ARGV:
                    break
                shown.append(arg)
            rows.append({'application': {'generation': self.generation, 'application_id': name},
                         'argv': shown, 'argv_truncated': len(shown) < len(argv), 'cwd': launch.get('cwd'),
                         'state': record['state'], 'exit_code': record.get('exit_code'),
                         'uncertain': raw.get('uncertain') is True, 'logs': record['logs'],
                         'record': f'applications/{name}/record.json', 'launch_record': raw.get('launch_record'),
                         'launched_at': launched_at if isinstance(launched_at, str) else None})
        rows.sort(key=lambda row: (row['launched_at'] or '', row['application']['application_id']))
        return rows

    def summarize_applications(self, *, emptied=False):
        """Record the most recent applications in the manifest.

        EMPTIED: the session's cgroup is verified empty, so an application whose
        record never saw its exit was ended by the session stopping. The oldest
        rows are dropped until the manifest fits its size limit.
        """
        rows = self.applications_by_launch()
        omitted = self.applications_skipped
        for row in rows:
            row['ended_by_session_stop'] = emptied and row['state'] not in ('all-exited', 'launch-failed')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            while True:
                value['applications'] = rows
                value['applications_omitted'] = omitted
                value['revision'] += 1
                value['updated_at'] = timestamp()
                try:
                    packed(value)
                    break
                except ContractError:
                    if not rows:
                        raise
                    rows, omitted = rows[1:], omitted + 1
                    value['revision'] -= 1
            self._write(self.fd, 'manifest.json', value)

    def worker_identity(self, *, managed=False):
        # Linux proc start ticks identify this PID lifetime; not process control authority.
        try:
            birth = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
            int(birth)
        except (OSError, ValueError, IndexError):
            fail('process_identity')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            value['process'] = {'state': 'collected', 'producer': 'worker', 'pid': os.getpid(),
                                'start_ticks': birth, 'service': ('agent-desktop-' + self.generation + '.service') if managed else 'not_collected'}
            value['revision'] += 1
            value['updated_at'] = timestamp()
            self._write(self.fd, 'manifest.json', value)
