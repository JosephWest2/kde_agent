"""Generation-owned asynchronous fixed KWin query and bounded cleanup.

Children owns every reaper reference. Query owns pipes, exact script name and
anchored temporary directory until cleanup is proved; failure never frees the
adapter for reuse while any of those resources remains uncertain.
"""
import os
import stat
import time
import uuid
from importlib.resources import files

from .contracts import ContractError
from .private_bus import PrivateBus
from .protocol import encode
from .window_types import Decoder, encoded

MAX_PINS = 4096


def failure(message='Structured window query failed.'):
    return ContractError('window_query_failed', message)


class Adapter:
    def __init__(self, desktop, generation, binary):
        self.desktop, self.generation, self.binary = desktop, generation, binary
        self.registry = None
        self.active = None
        self.pins = {}

    def start(self, request_id, deadline, *, application=None):
        if self.active is not None:
            raise ContractError('session_unavailable', 'Window query cleanup is unresolved.')
        query = Query(self, request_id, deadline, application)
        self.active = query
        return query

    def close(self):
        if self.active is not None:
            self.active.cancel('cancelled')
        self.pins.clear()


class Query:
    cleanup_seconds = 1.5

    def __init__(self, owner, request_id, deadline, application):
        self.owner, self.desktop, self.registry = owner, owner.desktop, owner.registry
        self.request_id, self.deadline, self.application = request_id, deadline, application
        self.id = uuid.uuid4().hex
        self.name = 'agent-window-' + owner.generation + '-' + self.id
        self.phase = 'prepare'
        self.error = None
        self.cleanup_deadline = None
        self.bus = None
        self.epoch = 0
        self.reply = None
        self.call_pending = False
        self.child = self.remover = None
        self.spawned = self.absent = False
        self.streams = {}
        self.buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        self.totals = {'stdout': 0, 'stderr': 0}
        self.parent_fd = self.dir_fd = None
        self.folder = None
        self.directory_created = False
        self.entries = None
        self.entry_count = 0
        self.decoder = None
        self.rows, self.identities, self.delta = [], [], {}
        self.index = 0
        self.observed_at = self.accepted_at = None
        self.result = None
        self.done = False
        self.bracket = None
        self.cleanup_phase = 'reap'

    def check(self):
        if self.error is not None:
            raise self.error
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Window query deadline expired.')

    def _bus(self, deadline):
        if self.bus is None:
            self.bus = PrivateBus(self.desktop.env['DBUS_SESSION_BUS_ADDRESS'], deadline)
        self.bus.tick()
        return self.bus.connection is not None

    def _loaded(self, deadline):
        if self.reply is not None:
            value, self.reply = self.reply, None
            return value[0]
        if not self.call_pending:
            from gi.repository import GLib
            epoch = self.epoch
            self.call_pending = True
            def reply(value, _fds, error):
                if epoch != self.epoch:
                    return
                self.call_pending = False
                if error is not None or value is None or value.unpack() not in ((True,), (False,)):
                    self.bus.error = failure('Window script absence could not be observed.')
                else:
                    self.reply = value.unpack()
            self.bus.call('loaded', 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                          'isScriptLoaded', GLib.Variant('(s)', (self.name,)), '(b)', deadline, reply)
        return None

    def _spawn(self, remove=False):
        reads, writes = {}, {}
        try:
            for key in ('stdout', 'stderr'):
                reads[key], writes[key] = os.pipe2(os.O_CLOEXEC)
                os.set_blocking(reads[key], False)
            self.streams = reads
            argv = [self.owner.binary, '--remove', self.name] if remove else [self.owner.binary, '--name', self.name, 'kwinscript', '--file', str(self.folder / 'input.js')]
            child = self.desktop.children.start(argv, env=self.desktop.env | {'TMPDIR': str(self.folder)},
                cwd=str(self.desktop.root), stdout=writes['stdout'], stderr=writes['stderr'])
            if remove:
                self.remover = child
            else:
                self.child, self.spawned = child, True
        except Exception:
            self._close_streams()
            raise
        finally:
            for fd in writes.values():
                os.close(fd)

    def _close_streams(self):
        for fd in self.streams.values():
            os.close(fd)
        self.streams.clear()

    def _drain(self):
        budget = 16384
        end = time.monotonic() + .002
        for key, fd in tuple(self.streams.items()):
            while budget and time.monotonic() < end:
                try:
                    raw = os.read(fd, min(4096, budget))
                except BlockingIOError:
                    break
                if not raw:
                    os.close(fd)
                    del self.streams[key]
                    break
                budget -= len(raw)
                self.totals[key] += len(raw)
                limit = 262144 if key == 'stdout' else 65536
                if self.totals[key] > limit:
                    raise failure('Window query output exceeded its limit.')
                self.buffers[key].extend(raw)

    def _local_cleanup(self):
        if self.dir_fd is None:
            if self.directory_created:
                # A failed open after our exclusive mkdir still owns the entry.
                self.dir_fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
                info = os.fstat(self.dir_fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise failure('Window query temporary directory is unsafe.')
            else:
                return True
        if self.entries is None:
            self.entries = os.scandir(self.dir_fd)
        end = time.monotonic() + .002
        for _ in range(4):
            if time.monotonic() >= end:
                return False
            try:
                entry = next(self.entries)
            except StopIteration:
                self.entries.close()
                self.entries = None
                os.rmdir(self.name, dir_fd=self.parent_fd)
                try:
                    os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise failure('Window query temporary directory remains present.')
                os.close(self.dir_fd)
                os.close(self.parent_fd)
                self.dir_fd = self.parent_fd = None
                self.directory_created = False
                return True
            self.entry_count += 1
            info = os.stat(entry.name, dir_fd=self.dir_fd, follow_symlinks=False)
            if self.entry_count > 32 or info.st_uid != os.getuid() or not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise failure('Window query temporary directory is unsafe.')
            os.unlink(entry.name, dir_fd=self.dir_fd)
        return False

    def _association(self, window):
        identity = None if self.registry is None else self.registry.window_identity(self.bracket, window.pid)
        reason = 'unverified_process' if window.pid is not None else 'missing_pid'
        if identity is not None:
            binding = (identity['application']['application_id'], identity['pid'], identity['start_time_ticks'], identity['boot_id'])
            pin = self.owner.pins.get(window.uuid)
            if pin is not None and pin != binding:
                identity, reason = None, 'identity_changed'
            elif pin is None and len(self.owner.pins) + len(self.delta) >= MAX_PINS:
                identity, reason = None, 'association_capacity'
            else:
                if pin is None:
                    self.delta[window.uuid] = binding
                reason = 'verified_process'
        return identity, reason

    def step(self):
        self.check()
        if self.phase == 'prepare':
            self.deadline = min(self.deadline, time.monotonic() + .5)
            if self.application is not None:
                self.registry.lookup(self.application)
            self.bracket = None if self.registry is None else self.registry.begin_window_observation()
            self.parent_fd = os.open(self.desktop.root / 'tmp', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.mkdir(self.name, 0o700, dir_fd=self.parent_fd)
            self.directory_created = True
            self.dir_fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self.folder = self.desktop.root / 'tmp' / self.name
            fd = os.open('input.js', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.dir_fd)
            with os.fdopen(fd, 'w') as stream:
                stream.write(encoded(self.name) + files('agent_desktop').joinpath('window_query.js').read_text())
            self.phase = 'collision'
        if self.phase == 'collision' and self._bus(self.deadline):
            loaded = self._loaded(self.deadline)
            if loaded is True:
                raise failure('Window query script name collided.')
            if loaded is False:
                self.check()
                self._spawn()
                self.phase = 'running'
        elif self.phase == 'running':
            self._drain()
            if self.child.returncode is not None and not self.streams:
                if self.child.returncode != 0:
                    raise failure()
                self.observed_at = time.monotonic()
                self.decoder = Decoder(bytes(self.buffers['stdout']), self.name)
                self.check()
                self.phase = 'decode'
        elif self.phase == 'decode':
            if self.decoder.step(min(self.deadline, time.monotonic() + .002), time.monotonic):
                self.phase = 'associate'
        elif self.phase == 'associate':
            end = min(self.deadline, time.monotonic() + .002)
            for _ in range(16):
                if self.index == len(self.decoder.windows):
                    self.phase = 'absence'
                    break
                if time.monotonic() >= end:
                    break
                window = self.decoder.windows[self.index]
                identity, reason = self._association(window)
                self.rows.append(window.wire(self.owner.generation) | {'app': None if identity is None else identity['application'],
                    'association': {'reason': reason, 'verified_at': None if identity is None else time.monotonic(), 'process': None if identity is None else {k: identity[k] for k in ('pid', 'start_time_ticks', 'boot_id')}}})
                self.identities.append(identity)
                self.index += 1
        elif self.phase == 'absence' and self._bus(self.deadline):
            loaded = self._loaded(self.deadline)
            if loaded is True:
                raise failure('Window query script remained loaded.')
            if loaded is False:
                self.absent = True
                self.bus.close()
                self.bus = None
                self.phase = 'local'
        elif self.phase == 'local':
            self._close_streams()
            if self._local_cleanup():
                self.phase = 'observation'
        elif self.phase == 'observation':
            self.result = {'generation': self.owner.generation, 'request_id': self.request_id, 'query_id': self.id,
                'observation_state': 'observed', 'observed_at': self.observed_at, 'active_window': None if self.decoder.active is None else
                {'generation': self.owner.generation, 'window_id': self.decoder.active}, 'windows': self.rows,
                'query_artifact': 'window-observations/' + self.id + '.json',
                'cleanup': {'child_reaped': True, 'script_absent': True, 'temporary_removed': True}}
            # Check the complete public envelope, including escaping, before acceptance.
            encode({'schema_version': 1, 'request_id': self.request_id, 'operation': 'windows', 'ok': True,
                    'session': self.desktop.store.session, 'result': self.result | {'accepted_at': 999999999999999.9}, 'error': None})
            self.check()
            artifact = self.desktop.store.window_observation(self.id, self.result)
            self.result['query_artifact'] = artifact
            self.check()
            self.index = 0
            self.phase = 'recheck'
        elif self.phase == 'recheck':
            end = min(self.deadline, time.monotonic() + .002)
            for _ in range(16):
                if self.index == len(self.rows):
                    self.check()
                    # Association acceptance is distinct from later record/transport success.
                    if self.registry is not None and self.bracket[1] is not None and (self.registry.generation != self.bracket[0] or self.registry.active is not self.bracket[1] or self.bracket[1].uncertain or self.bracket[1].completed):
                        raise failure('Application ownership changed during observation.')
                    if len(self.owner.pins) + len(self.delta) > MAX_PINS or any(key in self.owner.pins and self.owner.pins[key] != value for key, value in self.delta.items()):
                        raise failure('Window identity acceptance changed.')
                    self.accepted_at = time.monotonic()
                    if self.accepted_at >= self.deadline:
                        raise ContractError('timeout', 'Window query acceptance expired.')
                    self.owner.pins.update(self.delta)
                    self.result['accepted_at'] = self.accepted_at
                    self.result['observation_state'] = 'accepted'
                    self.phase = 'publish'
                    break
                if time.monotonic() >= end:
                    break
                identity = self.identities[self.index]
                window = self.decoder.windows[self.index]
                if identity is not None and self.registry.window_identity(self.bracket, window.pid) != identity:
                    self.rows[self.index]['app'] = None
                    self.rows[self.index]['association'] = {'reason': 'identity_changed', 'verified_at': None, 'process': None}
                    self.delta.pop(window.uuid, None)
                elif identity is not None:
                    self.rows[self.index]['association']['verified_at'] = time.monotonic()
                self.index += 1
        elif self.phase == 'publish':
            app = self.application
            if app is None and self.bracket is not None and self.bracket[1] is not None:
                app = self.bracket[1].handle
            if app is not None:
                observation = {k: self.result[k] for k in ('query_artifact', 'query_id', 'observed_at', 'accepted_at')}
                observation['windows'] = [row['window'] for row in self.rows if row['app'] == app]
                self.registry.publish_windows(app, observation, self.check)
            if self.application is not None:
                self.result['windows'] = [row for row in self.rows if row['app'] == self.application]
            self.check()
            self.done = True
            self.owner.active = None
            self.phase = 'done'
            return self.result
        self.check()
        return None

    def cancel(self, cause='cancelled'):
        if self.error is None:
            self.error = cause if isinstance(cause, ContractError) else ContractError(cause, 'Window query did not complete.')
            self.cleanup_deadline = time.monotonic() + self.cleanup_seconds
            self.epoch += 1
            if self.bus is not None:
                self.bus.close()
            self.bus, self.reply, self.call_pending = None, None, False
            if self.child is not None and self.child.returncode is None:
                self.child.abort()
            self._close_streams()

    def cleanup(self, deadline=None):
        if self.done:
            return True
        if self.error is None:
            self.cancel()
        if deadline is not None:
            self.cleanup_deadline = min(self.cleanup_deadline, deadline)
        if time.monotonic() >= self.cleanup_deadline:
            if self.remover is not None and self.remover.returncode is None:
                self.remover.abort()
            return False
        if self.cleanup_phase == 'reap':
            if self.child is not None and self.child.returncode is None:
                return False
            self.cleanup_phase = 'absence' if self.spawned and not self.absent else 'local'
        if self.cleanup_phase in ('absence', 'verify') and self._bus(self.cleanup_deadline):
            loaded = self._loaded(self.cleanup_deadline)
            if loaded is False:
                self.absent = True
                self.bus.close()
                self.bus = None
                self.cleanup_phase = 'local'
            elif loaded is True:
                if self.cleanup_phase == 'verify':
                    return False
                self.buffers = {'stdout': bytearray(), 'stderr': bytearray()}
                self._spawn(remove=True)
                self.cleanup_phase = 'remove'
        elif self.cleanup_phase == 'remove':
            try:
                self._drain()
            except ContractError:
                self.remover.abort()
                self._close_streams()
            if self.remover.returncode is not None and not self.streams:
                self.cleanup_phase = 'verify'
        if self.cleanup_phase == 'local':
            if self._local_cleanup():
                if self.parent_fd is not None:
                    os.close(self.parent_fd)
                    self.parent_fd = None
                self.done = True
                self.owner.active = None
                return True
        return False


class WindowsTask:
    cleanup_seconds = 1.5

    def __init__(self, request, context, adapter, healthy):
        self.request, self.context, self.adapter, self.healthy = request, context, adapter, healthy
        self.query = None
        self.cancelled = False

    def step(self, now):
        if self.cancelled:
            raise ContractError('cancelled', 'Window query was cancelled.')
        if time.monotonic() >= self.context.work.admission.deadline:
            raise ContractError('timeout', 'Window query deadline expired.')
        self.healthy()
        if self.request.expected_generation != self.adapter.generation:
            raise ContractError('generation_mismatch', 'Window query generation changed.')
        if self.query is None:
            self.query = self.adapter.start(self.request.request_id, self.context.work.admission.deadline,
                                           application=self.request.arguments.get('app'))
        try:
            return self.query.step()
        except ContractError as error:
            self.query.cancel(error)
            raise

    def request_cancel(self, reason):
        self.cancelled = True
        if self.query is not None:
            self.query.cancel(reason)

    def cleanup(self, now):
        return self.query is None or self.query.cleanup(self.context.work.cleanup_deadline)
