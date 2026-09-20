"""Owner-private, bounded generation records independent of disposable runtime.

One nonblocking advisory lock protects read/modify/replace. Request history lives
in separate attempt records, never an ever-growing generation snapshot. Storage
is synchronous under the documented normal-storage assumption, not a hard
latency guarantee on a hung filesystem.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import stat
import time
import uuid

from . import __version__
from .contracts import ContractError, GENERATION, NAME, APP_ID, OPERATIONS, EXIT_CODES, handle

LIMIT = 65536
EVENT_LIMIT = 4096
LAUNCH_LIMIT = 1048576
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



def mkdir_durable(parent, name, *, exclusive=False):
    """Persist the parent entry before any descendant can authorize effects."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        if exclusive:
            raise
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


def actual_path(fd):
    # Linux is the worker's supported platform. procfs reflects renamed/unlinked
    # directory identities, unlike a prior Path.resolve check.
    return Path(os.readlink(f'/proc/self/fd/{fd}'))


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
    if isinstance(windows, list) and len(windows) <= 64:
        out['windows'] = [ref['window'] for window in windows
                          if (ref := safe_projection({'window': window}, generation)).get('window')]
    phase = value.get('phase')
    if isinstance(phase, str) and phase in PHASES:
        out['phase'] = phase
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
        if set(value) != required or value['mode'] != 'headless' or value['records'] != record_layout():
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

    def generation_update(self, *, state=None, failure=None, cleanup=None):
        if state not in (None, 'running', 'ready', 'stopping', 'stopped', 'failed') or failure not in (None, *EXIT_CODES):
            fail('schema')
        if cleanup not in (None, 'in_progress', 'complete', 'uncertain'):
            fail('schema')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            if failure:
                value['first_failure'] = value['first_failure'] or failure
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
                os.fsync(group)
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
        with self.lock(), self.directory('logs') as directory:
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
        with self.lock(), self.directory('logs') as directory:
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
                name = allocation + '.' + kind + '.partial'
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
