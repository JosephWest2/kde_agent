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
        if not root.is_absolute():
            fail('root')
        canonical = root.resolve()
        for raw in disposable:
            other = Path(raw).resolve()
            if canonical.is_relative_to(other) or other.is_relative_to(canonical):
                fail('disposable_root')
        self.root, self.session, self.generation = root, session, generation
        self.path = root / 'generations' / generation
        self.fd = None
        try:
            # Reject aliases, including existing ancestor symlinks. We never chmod user roots.
            for part in (root, *root.parents):
                if part.is_symlink():
                    fail('root')
            if create:
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_info = root.stat()
            if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
                fail('root')
            generations = root / 'generations'
            if create:
                generations.mkdir(mode=0o700, exist_ok=True)
            parent = os.open(generations, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                check_fd(parent, True)
                if create:
                    os.mkdir(generation, mode=0o700, dir_fd=parent)
                self.fd = os.open(generation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                check_fd(self.fd, True)
                if create:
                    os.fsync(parent)
            finally:
                os.close(parent)
            if create:
                for name in ('requests', 'applications', 'logs'):
                    os.mkdir(name, mode=0o700, dir_fd=self.fd)
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
                        'records': {'requests': 'requests', 'applications': 'applications', 'events': 'events.jsonl',
                                    'logs': {source: f'logs/{source}.log' for source in ('worker', 'compositor', 'bus')}},
                    })
                for name in ('worker', 'compositor', 'bus'):
                    self.log_path(name)
            else:
                self.read()
        except (OSError, ContractError):
            self.close()
            fail('open')

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
            fd = os.open('record.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=self.fd)
            check_fd(fd)
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
            if (not isinstance(value, dict) or value.get('schema_version') != 1
                    or value.get('generation') != self.generation or value.get('session') != self.session
                    or type(value.get('revision')) is not int):
                fail('identity')
            return value
        finally:
            os.close(fd)

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
        if state not in (None, 'running', 'stopped', 'failed') or failure not in (None, *EXIT_CODES):
            fail('schema')
        if cleanup not in (None, 'in_progress', 'complete', 'uncertain'):
            fail('schema')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            if failure:
                value['first_failure'] = value['first_failure'] or failure
            terminal = value['state'] in ('stopped', 'failed')
            if state and not (terminal and state == 'running'):
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
            try:
                os.mkdir(request.request_id, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            with self.directory('requests', request.request_id) as group:
                for _ in range(8):
                    attempt = uuid.uuid4().hex
                    try:
                        os.mkdir(attempt, mode=0o700, dir_fd=group)
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
                    'phase': 'admitted', 'outcome': 'pending', 'references': {}, 'error_code': None})
        return (request.request_id, attempt)

    def transition(self, token, event, *, outcome='pending', error_code=None, references=None):
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
            fd = os.open('events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
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
            fd = os.open(source + '.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
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
            fd = os.open(source + '.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
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
            os.mkdir(application_id, mode=0o700, dir_fd=parent)
            with self.directory('applications', application_id) as directory:
                self._write(directory, 'record.json', value)
            os.fsync(parent)

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

    def worker_identity(self):
        # Linux proc start ticks identify this PID lifetime; not process control authority.
        try:
            birth = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
            int(birth)
        except (OSError, ValueError, IndexError):
            fail('process_identity')
        with self.lock():
            value = self._read(self.fd, 'manifest.json')
            value['process'] = {'state': 'collected', 'producer': 'worker', 'pid': os.getpid(),
                                'start_ticks': birth, 'service': 'not_collected'}
            value['revision'] += 1
            value['updated_at'] = timestamp()
            self._write(self.fd, 'manifest.json', value)
