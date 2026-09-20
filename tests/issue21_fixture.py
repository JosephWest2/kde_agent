"""Internal installed-wheel lifecycle fixture; never changes sys.path.

Ordinary descendants are deliberately outside the worker's Children registry.
The grandchild starts a new session and ignores SIGTERM, so only generation
cgroup ownership can guarantee its removal. Native readiness remains provisional.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19], 'ppid': int(fields[1]),
            'sid': int(fields[3]),
            'cgroup': Path('/proc', str(pid), 'cgroup').read_text().strip()}


def write(path, value):
    from agent_desktop.lifecycle import atomic
    atomic(Path(path), value)


def event(root, kind, **fields):
    fd = os.open(root / 'fixture-events.jsonl', os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps({'event': kind, 'at': time.monotonic(), **fields}) + '\n').encode())
    finally:
        os.close(fd)


def descendant(root, grandchild=False):
    root = Path(root)
    if grandchild:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        write(root / 'fixture-grandchild.json', identity(os.getpid()))
    else:
        def terminated(signum, frame):
            event(root, 'application_termination', signal=signum)
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminated)
        subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'grandchild', str(root)])
        write(root / 'fixture-child.json', identity(os.getpid()))
    while True:
        time.sleep(.1)


def helper(mode, operation, name, generation):
    from agent_desktop.runtime import Runtime
    from agent_desktop.lifecycle import read_metadata
    from agent_desktop.service_cleanup import main
    data = read_metadata(Runtime(), name, generation)
    root = Path(data['configuration']['artifacts']) / 'generations' / generation
    event(root, 'helper_entry', operation=operation, identity=identity(os.getpid()))
    if mode == 'blocked-' + operation:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        event(root, 'helper_blocked', operation=operation)
        while True:
            time.sleep(.1)
    return main([operation, name, generation])


def worker(name, generation, artifacts, kdotool, mode):
    from agent_desktop.contracts import ContractError
    from agent_desktop.worker import run
    root = Path(artifacts) / 'generations' / generation
    if mode == 'blocked-release-recordfail':
        # Worker-local record failure; independent installed service helpers
        # import a fresh unmodified Store in separate processes.
        from agent_desktop.artifacts import Store
        generation_update = Store.generation_update
        def injected_generation_update(self, **kwargs):
            if kwargs.get('state') == 'failed':
                event(root, 'early_manifest_write_failed')
                raise OSError('Injected worker-only failed-state manifest write failure')
            return generation_update(self, **kwargs)
        Store.generation_update = injected_generation_update
    subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'child', str(root)])
    write(root / 'fixture-worker.json', identity(os.getpid()))

    def freeze_with_record_lock(signum, frame):
        with (root / 'record.lock').open('r') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            write(root / 'fixture-lock-held.json', identity(os.getpid()))
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGSTOP)
    signal.signal(signal.SIGUSR1, freeze_with_record_lock)

    def freeze_with_generation_lock(signum, frame):
        from agent_desktop.ownership import generation_lock
        from agent_desktop.runtime import Runtime
        with generation_lock(Runtime(), generation):
            write(root / 'fixture-generation-lock-held.json', identity(os.getpid()))
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGSTOP)
    signal.signal(signal.SIGUSR2, freeze_with_generation_lock)

    class Task:
        cleanup_seconds = .1
        def __init__(self, request, context):
            self.request = request
            self.started = False
        def step(self, now):
            if not self.started:
                event(root, 'action_started', request_id=self.request.request_id)
                self.started = True
            return None
        def request_cancel(self, reason):
            event(root, 'action_cancel', reason=reason)
        def cleanup(self, now):
            event(root, 'action_cleanup')
            return True

    def release(now, deadline):
        event(root, 'release_attempt', deadline=deadline)
        if mode in ('blocked-release', 'blocked-release-recordfail'):
            event(root, 'release_blocked')
            while True:
                time.sleep(.1)
        return {'state': 'fixture_confirmed', 'confirmed': True, 'provider': 'issue21_fixture'}

    def close(now, deadline):
        event(root, 'normal_close_attempt', deadline=deadline)
        # This is a normal-close hook observer, not a production window adapter.
        # Leave the ordinary processes alive to prove later service escalation.
        return {'state': 'fixture_attempted', 'confirmed': False, 'provider': 'issue21_fixture'}

    def observe(desktop):
        if ((root / 'inject-startup-failure').exists() or
                (mode == 'failedstart' and desktop.phase == 'constructed' and
                 (root / 'fixture-grandchild.json').exists())):
            event(root, 'startup_failure')
            raise ContractError('session_failed', 'Injected startup failure after descendants exist.')

    run(name, generation, artifacts=artifacts, managed=True, desktop=True,
        kdotool=None if mode in ('starting', 'failedstart') else kdotool,
        factory=Task, desktop_observer=observe,
        shutdown_hooks={'release': release, 'close': close})


if __name__ == '__main__':
    os.umask(0o077)
    operation, *args = sys.argv[1:]
    if operation in ('child', 'grandchild'):
        descendant(*args, grandchild=operation == 'grandchild')
    elif operation == 'helper':
        raise SystemExit(helper(*args))
    elif operation == 'worker':
        worker(*args)
    else:
        raise ValueError(operation)
