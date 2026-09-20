"""Generation-local kernel ownership. Durable identities are never signal handles."""
import os
import re
from dataclasses import dataclass
from collections import deque
from pathlib import Path
import select
import time
import uuid

from .contracts import ContractError
from .service_cleanup import membership

MAX_IDENTITIES = 4096
MAX_GROUPS = 256
MAX_DEPTH = 8
MAX_INPUT = 65536


def uncertain():
    return ContractError('session_failed', 'Application ownership could not be verified.', outcome='unknown')


def birth(pid):
    raw = Path('/proc', str(pid), 'stat').read_text()
    return int(raw.rsplit(')', 1)[1].split()[19])


def live(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return not poller.poll(0)


def identity(pid, cgroup, *, exact=False):
    """Acquire then validate against two proc observations and the pinned lifetime."""
    before = birth(pid)
    fd = os.pidfd_open(pid)
    retained = False
    try:
        group = membership(pid)
        after = birth(pid)
        if before != after or not live(fd):
            return None
        if group != cgroup and (exact or not group.startswith(cgroup + '/')):
            return None
        retained = True
        return fd, {'pid': pid, 'start_time_ticks': after, 'cgroup': group}
    finally:
        if not retained:
            os.close(fd)


def scan(root):
    """Streaming traversal; None is a scheduling yield after each read/entry.

    No entire recursive walk or membership file is materialized. Any incomplete
    observation raises; consumers must never interpret that as emptiness.
    """
    groups = 0
    identities = 0
    def visit(path, depth):
        nonlocal groups, identities
        groups += 1
        if groups > MAX_GROUPS or depth > MAX_DEPTH:
            raise uncertain()
        with open(path / 'cgroup.procs', 'rb', buffering=0) as stream:
            total, pending = 0, b''
            while True:
                chunk = stream.read(4096)
                total += len(chunk)
                if total > MAX_INPUT:
                    raise uncertain()
                if not chunk:
                    if pending:
                        raise uncertain()
                    break
                pending += chunk
                yield None
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    if not raw.isdigit() or len(raw) > 20 or int(raw) <= 0:
                        raise uncertain()
                    identities += 1
                    if identities > MAX_IDENTITIES:
                        raise uncertain()
                    yield int(raw)
                if len(pending) > 20:
                    raise uncertain()
        with os.scandir(path) as entries:
            for entry in entries:
                yield None
                if entry.is_dir(follow_symlinks=False):
                    yield from visit(Path(entry.path), depth + 1)
    yield from visit(Path(root), 0)


class Application:
    def __init__(self, registry, app_id, fd, path, cgroup):
        self.registry, self.id, self.fd, self.path, self.cgroup = registry, app_id, fd, path, cgroup
        self.child = None
        self.process = None
        self.handles = {}
        self.reap_queue = deque()
        self.observed = {}
        self.logs = {}
        self.executable = None
        self.state = 'prepared'
        self.authorized = False
        self.settled = False
        self.activated = False
        self.uncertain = False
        self.exit_code = None
        self.scanner = None
        self.pending_pid = None
        self.pending_processes = []
        self.completed = False
        self.last_check = self.last_write = 0
        self.dirty = False
        self.window_observation = None
        self.previous_observation = None
        self.pending_observation = None

    @property
    def handle(self):
        return {'generation': self.registry.generation, 'application_id': self.id}

    def snapshot(self):
        return {'application': self.handle, 'process': self.process, 'executable': self.executable,
                'logs': self.logs, 'state': self.state, 'exit_code': self.exit_code,
                'windows': [] if self.window_observation is None else self.window_observation['windows'],
                'window_observation': self.window_observation, 'previous_observation': self.previous_observation,
                'pending_observation': self.pending_observation}

    def observe_exit(self):
        """Retention seam for future waits; root exit is not complete app exit."""
        self.observe(force=True)
        return {'root_returncode': self.exit_code, 'all_exited': self.completed, 'state': self.state}

    def persist(self):
        self.registry.store.application_update(self.id, state=self.state, process=self.process,
            exit_code=self.exit_code, authorized=self.authorized, uncertain=self.uncertain)
        self.dirty = False
        self.last_write = time.monotonic()

    def root_identity(self):
        acquired = identity(self.child.process.pid, self.cgroup, exact=True)
        if acquired is None:
            raise uncertain()
        fd, process = acquired
        self.process = process | {'boot_id': self.registry.boot_id}
        key = (process['pid'], process['start_time_ticks'])
        old = self.handles.pop(key, None)
        if old is not None:
            os.close(old)
        else:
            self.reap_queue.append(key)
        self.handles[key] = fd
        self.registry.index_add(self, key)
        self.observed[key] = self.process
        self.activated = True
        self.persist()

    def populated(self):
        fd = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        try:
            values = dict(line.split() for line in raw.decode().splitlines())
            if len(raw) > 4096 or values.get('populated') not in ('0', '1'):
                raise ValueError()
            return values['populated'] == '1'
        except (ValueError, UnicodeError):
            raise uncertain() from None

    def observe(self, *, force=False):
        if self.completed:
            return
        if self.fd is None:
            raise uncertain()
        now = time.monotonic()
        if not force and now - self.last_check < .05:
            return
        self.last_check = now
        if self.uncertain:
            raise uncertain()
        if self.child is not None and self.child.returncode is not None:
            self.exit_code = self.child.returncode
            if self.state not in ('root-exited', 'all-exited', 'launch-failed'):
                self.state = 'root-exited' if self.authorized else 'launch-failed'
                self.dirty = True
        empty = not self.populated()
        # Empty before helper enters the group is NOT application completion.
        if empty and self.settled and (self.child is None or self.child.returncode is not None):
            # A prior scan may have used its entire turn before persistence.
            # Do not discard those observed identities when retiring ownership.
            if self.pending_processes:
                return
            self.state = 'all-exited' if self.authorized and self.state != 'launch-failed' else 'launch-failed'
            self.dirty = True
            self.persist()
            for path in self.logs.values():
                self.registry.store.artifact_state(path, 'complete')
            self.registry.checkpoint_windows()
            self.completed = True
            self.registry.active = None
            self.close()
            # cgroup rmdir requires empty nested groups, which ordinary apps do
            # not create. Keep a proved-empty tree until systemd removal if any.
            try:
                self.path.rmdir()
            except OSError:
                pass

    def scan_turn(self, *, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + .002
        published = False
        # A batch remains owned in memory until its write succeeds. Flush it
        # before collecting more, bounding deferred persistence to 16 identities.
        if self.pending_processes and time.monotonic() < deadline:
            self.registry.store.application_processes(self.id, self.pending_processes)
            self.pending_processes.clear()
            published = True
        for _ in range(min(16, len(self.reap_queue))):
            if time.monotonic() >= deadline:
                return
            key = self.reap_queue.popleft()
            fd = self.handles.get(key)
            if fd is None:
                continue
            if not live(fd):
                os.close(fd)
                del self.handles[key]
                self.registry.index_remove(self, key)
            else:
                self.reap_queue.append(key)
        if self.scanner is None:
            self.scanner = scan(self.path)
        identities = 0
        while identities < 16 and time.monotonic() < deadline:
            if self.pending_pid is None:
                try:
                    self.pending_pid = next(self.scanner)
                except StopIteration:
                    self.scanner = None
                    break
                if self.pending_pid is None:
                    # One read/entry boundary per turn bounds input to 4 KiB.
                    break
            # Iteration may itself have exhausted the budget. Retain the member
            # without starting another kernel observation on this turn.
            if time.monotonic() >= deadline:
                break
            pid, self.pending_pid = self.pending_pid, None
            identities += 1
            try:
                acquired = identity(pid, self.cgroup)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if acquired is None:
                continue
            fd, info = acquired
            key = (info['pid'], info['start_time_ticks'])
            if key in self.observed:
                os.close(fd)
                continue
            if len(self.observed) >= MAX_IDENTITIES:
                os.close(fd)
                raise uncertain()
            info['boot_id'] = self.registry.boot_id
            self.handles[key], self.observed[key] = fd, info
            self.registry.index_add(self, key)
            self.reap_queue.append(key)
            self.pending_processes.append(info)
        if self.pending_processes and not published and time.monotonic() < deadline:
            self.registry.store.application_processes(self.id, self.pending_processes)
            self.pending_processes.clear()
        now = time.monotonic()
        if self.dirty and now < deadline and now - self.last_write >= .05:
            self.persist()

    def close(self):
        if self.scanner is not None:
            self.scanner.close()
            self.scanner = None
        for fd in self.handles.values():
            os.close(fd)
        self.handles.clear()
        self.registry.index_clear(self)
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Registry:
    def __init__(self, generation, cgroup, store, children):
        if membership(os.getpid()) != cgroup + '/supervisor':
            raise uncertain()
        self.generation, self.cgroup, self.store = generation, cgroup, store
        self.children = children
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.root = Path('/sys/fs/cgroup' + cgroup)
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.mkdir('applications', dir_fd=self.root_fd)
        except FileExistsError:
            pass
        self.apps_fd = os.open('applications', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)
        self.active = None
        self.identity_index = {}
        self.identity_revision = 0
        self.window_checkpoint = None

    def index_add(self, app, key):
        # Compatibility with narrow unit test registries constructed via __new__.
        self.identity_revision = getattr(self, 'identity_revision', 0) + 1
        if not hasattr(self, 'identity_index'):
            self.identity_index = {}
        self.identity_index[key[0]] = (app, key, self.identity_revision)

    def index_remove(self, app, key):
        index = getattr(self, 'identity_index', {})
        entry = index.get(key[0])
        if entry is not None and entry[:2] == (app, key):
            del index[key[0]]

    def index_clear(self, app):
        # Only one live application is supported; clearing invalidates all tokens.
        if getattr(self, 'active', None) in (None, app):
            getattr(self, 'identity_index', {}).clear()
        self.identity_revision = getattr(self, 'identity_revision', 0) + 1

    def lookup(self, handle):
        if handle['generation'] != self.generation:
            raise ContractError('generation_mismatch', 'Application belongs to another generation.')
        ident = handle['application_id']
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise ContractError('target_not_found', 'Application was not found.')
        if self.active is not None and self.active.id == ident:
            return self.active.snapshot()
        return self.store.application_read(ident)

    def begin_window_observation(self):
        app = self.active
        return (self.generation, app, getattr(self, 'identity_revision', 0))

    def observe_application_exit(self, handle, *, force=False):
        snapshot = self.lookup(handle)
        app = self.active
        if app is not None and app.handle == handle:
            app.observe(force=force)
            return app.snapshot(), app.completed
        if snapshot['state'] not in ('all-exited', 'launch-failed'):
            raise uncertain()
        # Historical launch-failed records retire only after complete emptiness.
        return snapshot, True

    def window_identity(self, bracket, pid):
        if pid is None:
            return None
        generation, app, cutoff = bracket
        entry = getattr(self, 'identity_index', {}).get(pid)
        if (generation != self.generation or app is None or self.active is not app
                or app.uncertain or app.completed or entry is None or entry[0] is not app or entry[2] > cutoff):
            return None
        key = entry[1]
        fd = app.handles.get(key)
        if fd is None:
            return None
        try:
            if not live(fd) or birth(pid) != key[1]:
                return None
            group = membership(pid)
            if group != app.cgroup and not group.startswith(app.cgroup + '/'):
                return None
            if not live(fd) or birth(pid) != key[1]:
                return None
        except (OSError, ValueError):
            return None
        # Return values only, never borrowed descriptors or historic authority.
        return {'application': app.handle, 'pid': pid, 'start_time_ticks': key[1],
                'boot_id': self.boot_id, 'revision': entry[2]}

    def checkpoint_windows(self):
        owner = getattr(self, 'window_checkpoint', None)
        if owner is not None:
            self.store.application_windows(owner['application_id'], owner['previous'], owner['current'], 'confirmed')
            self.window_checkpoint = None

    def publish_windows(self, app_handle, observation, check):
        self.checkpoint_windows()
        current = self.lookup(app_handle)
        previous = current.get('window_observation')
        app = self.active if self.active is not None and self.active.handle == app_handle else None
        if app is not None:
            app.pending_observation = observation | {'publication': 'pending'}
        try:
            self.store.application_windows(app_handle['application_id'], previous, observation, 'pending')
            check()
        except Exception:
            if app is not None:
                app.pending_observation = observation | {'publication': 'uncertain'}
            # The prior immutable artifact remains independently recoverable.
            raise
        if app is not None:
            app.previous_observation, app.window_observation = previous, observation
            app.pending_observation = None
        self.window_checkpoint = {'application_id': app_handle['application_id'],
                                  'previous': previous, 'current': observation}

    def available(self):
        if self.active is not None:
            self.active.observe(force=True)
        if self.active is not None:
            raise ContractError('application_active', 'An application or its descendants remain active.',
                                context={'application': self.active.handle})

    def reserve(self):
        self.available()
        app_id = uuid.uuid4().hex
        os.mkdir(app_id, dir_fd=self.apps_fd)
        fd = os.open(app_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.apps_fd)
        self.active = Application(self, app_id, fd, self.root / 'applications' / app_id,
                                  self.cgroup + '/applications/' + app_id)
        return self.active

    def tick(self):
        app = self.active
        if app is None:
            self.checkpoint_windows()
            return
        deadline = time.monotonic() + .002
        try:
            self.checkpoint_windows()
            if time.monotonic() >= deadline:
                return
            app.observe()
            if self.active is app and time.monotonic() < deadline:
                app.scan_turn(deadline=deadline)
        except Exception:
            app.uncertain = True
            raise

    def close(self):
        if self.active is not None:
            self.active.close()
        os.close(self.apps_fd)
        os.close(self.root_fd)
