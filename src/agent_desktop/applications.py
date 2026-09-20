"""Public launch task; application lifetime belongs to the worker registry."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

from .contracts import ContractError
from .environment import DEFAULTS, compose, executable


class LaunchTask:
    def __init__(self, request, context, registry, desktop, records, *, healthy=None, adapter=None):
        self.request, self.context = request, context
        self.registry, self.desktop, self.records = registry, desktop, records
        self.healthy = healthy or desktop.tick
        self.app = None
        self.gate = self.status = None
        self.cancelled = False
        self.phase = 'prepare'
        self.deadline = context.work.admission.deadline
        self.handshake = None
        self.exec_receipt = b''
        self.adapter = adapter
        self.window_wait = None

    def retain(self):
        if self.app is not None and self.app.authorized:
            self.context.effects(self.app.snapshot(), uncertain=self.phase in ('ready', 'exec'))

    def check(self):
        if self.cancelled:
            raise ContractError('cancelled', 'Launch request was cancelled.')
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Launch deadline expired.')
        self.healthy()
        if time.monotonic() >= self.deadline:
            raise ContractError('timeout', 'Launch deadline expired.')

    def prepare(self):
        args = self.request.arguments
        self.check()
        self.registry.available()
        env = compose(DEFAULTS, args['env'], self.desktop.private)
        selected = executable(args['argv'][0], args['cwd'], env)['executable']
        token = self.records.token(self.request.request_id)
        store = self.registry.store
        logs = {key: str(store.allocate(token, key)) for key in ('stdout', 'stderr')}
        store.launch(token, args['argv'], args['cwd'])
        app = self.app = self.registry.reserve()
        app.logs, app.executable = logs, selected
        try:
            store.application_prepare(token, app.id, executable=selected, logs=logs, cgroup=app.cgroup)
        except Exception:
            # No helper exists, but a failed prepared record must not lose the
            # reserved ownership. Session failure handles any uncertain storage.
            app.uncertain = True
            raise
        config = gate_read = status_write = None
        outputs = []
        try:
            config = os.memfd_create('agent-desktop-launch', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            raw = json.dumps({'argv': args['argv'], 'cwd': args['cwd'], 'env': env,
                              'executable': selected}).encode()
            if len(raw) > 1048576:
                raise ContractError('invalid_arguments', 'Launch configuration is too large.')
            with os.fdopen(os.dup(config), 'wb') as stream:
                stream.write(raw)
                stream.flush()
            os.lseek(config, 0, os.SEEK_SET)
            fcntl.fcntl(config, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW |
                        fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
            gate_read, self.gate = os.pipe2(os.O_CLOEXEC)
            self.status, status_write = os.pipe2(os.O_CLOEXEC)
            os.set_blocking(self.status, False)
            for key in ('stdout', 'stderr'):
                outputs.append(os.open(logs[key], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW))
            self.check()
            self.handshake = min(self.deadline, time.monotonic() + 2)
            helper = str(Path(__file__).with_name('app_launcher.py'))
            app.child = self.registry.children.start(
                [sys.executable, '-I', helper, str(app.fd), str(config), str(gate_read),
                 str(status_write), str(self.handshake)],
                pass_fds=(app.fd, config, gate_read, status_write), env=DEFAULTS, cwd='/',
                stdout=outputs[0], stderr=outputs[1])
            self.phase = 'ready'
        except Exception:
            app.settled = True
            raise
        finally:
            for fd in (config, gate_read, status_write, *outputs):
                if fd is not None:
                    os.close(fd)

    def step(self, now):
        if self.window_wait is not None:
            result = self.window_wait.step(now)
            if result is not None:
                self.retain()
                self.window_wait.check()
                return self.app.snapshot() | {'window_wait': result}
            return None
        self.check()
        if self.phase == 'prepare':
            self.prepare()
            return None
        if time.monotonic() >= self.handshake:
            raise ContractError('timeout', 'Launch helper handshake expired.')
        try:
            receipt = os.read(self.status, 64)
        except BlockingIOError:
            return None
        if self.phase == 'ready':
            if receipt != b'R':
                raise ContractError('session_failed', 'Launch helper did not enter its owned group.')
            self.app.root_identity()
            self.check()
            # This record conservatively precedes the irrevocable gate write.
            self.app.state = 'execution-authorized'
            self.app.persist()
            self.context.effects(self.app.snapshot(), uncertain=True)
            self.check()
            if time.monotonic() >= self.handshake:
                raise ContractError('timeout', 'Launch gate expired.')
            self.app.authorized = True
            os.write(self.gate, b'G')
            os.close(self.gate)
            self.gate = None
            self.handshake = min(self.deadline, time.monotonic() + 1)
            self.phase = 'exec'
            return None
        self.exec_receipt += receipt
        if len(self.exec_receipt) > 64 or b'E' in self.exec_receipt:
            self.app.settled = True
            self.app.state = 'launch-failed'
            self.app.persist()
            raise ContractError('prerequisite_missing', 'Application could not execute.')
        if receipt:
            return None
        self.app.settled = True
        self.registry.children.poll()
        if self.exec_receipt != b'X' or (self.app.child.returncode is not None and self.app.child.returncode < 0):
            raise ContractError('completion_unknown', 'Application execution could not be confirmed.', outcome='unknown')
        self.app.state = 'running' if self.app.child.returncode is None else 'root-exited'
        self.app.persist()
        self.close_endpoints()
        self.phase = 'done'
        self.context.effects(self.app.snapshot())
        if self.request.arguments['wait_window']:
            from .targeting import TargetTask
            self.phase = 'window_wait'
            self.handshake = None
            self.window_wait = TargetTask(self.request, self.context, self.adapter,
                self.registry, self.healthy, condition='window', application=self.app.handle,
                progress=self.retain)
            return None
        return self.app.snapshot()

    def close_endpoints(self):
        for name in ('gate', 'status'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def request_cancel(self, reason):
        self.cancelled = True
        if self.window_wait is not None:
            self.window_wait.request_cancel(reason)
        self.close_endpoints()
        if self.app is not None:
            self.app.settled = True
            if not self.app.authorized and self.app.child is not None:
                # Owned Popen is the sole reaper and protects against PID reuse.
                self.app.child.abort()
        # Diagnostics cannot replace an already-latched cause or consume the
        # native operation's cleanup allowance. Context captured current refs
        # before reporting observer failure.
        try:
            self.retain()
        except Exception:
            pass

    def cleanup(self, now):
        self.close_endpoints()
        return self.window_wait is None or self.window_wait.cleanup(now)

    @property
    def cleanup_seconds(self):
        return 1.5 if self.window_wait is not None and self.window_wait.operation is not None else .2
