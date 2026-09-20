"""Finite evidence-only real processes; no product fault parameter or host endpoint.

Each process exits within 30 seconds even if its controller disappears. Events are
single bounded writes to inherited stdout; TERM receipt is distinct from pidfd
submission. Descendants use setsid and double fork in the root-first case.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def emit(event, **values):
    stat = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    value = {'event': event, 'at': time.monotonic(), 'pid': os.getpid(),
             'ppid': os.getppid(), 'start_ticks': int(stat[19]),
             'cgroup': Path('/proc/self/cgroup').read_text().strip(), **values}
    raw = (json.dumps(value, allow_nan=False) + '\n').encode()
    assert len(raw) < 4096
    os.write(1, raw)


def run(mode, duration, trigger=None, native=None):
    assert 0 < duration <= 30
    end = time.monotonic() + duration
    role = 'root'
    received = False
    late = False
    def term(signum, frame):
        nonlocal received
        emit('term_received', role=role)
        received = True
        if mode == 'normal' or (mode == 'root-first' and role == 'root'):
            emit('term_exit', role=role)
            os._exit(0)
    signal.signal(signal.SIGTERM, term)
    # SIGALRM remains default and bounds stopped/resistant fixture lifetime once
    # running; the independent generation owner always tears down stopped tasks.
    signal.alarm(int(duration) + 1)
    emit('fixture_ready', role=role, mode=mode, deadline=end)
    if native:
        child = subprocess.Popen([native, '--autonomous', '--exit-after-ms', str(int(duration * 1000))])
        emit('native_spawned', role=role, native_pid=child.pid)
    if mode in ('tree', 'root-first', 'late', 'kill-late', 'migration'):
        pid = os.fork()
        if pid == 0:
            role = 'child'
            os.setsid()
            emit('fixture_ready', role=role, mode=mode, deadline=end)
            pid = os.fork() if mode != 'migration' else -1
            if pid == 0:
                role = 'grandchild'
                emit('fixture_ready', role=role, mode=mode, deadline=end)
            elif mode == 'root-first':
                # Orphan grandchild remains contained in the app cgroup.
                emit('intermediate_exit', role=role)
                os._exit(0)
    while time.monotonic() < end:
        if mode == 'migration' and role == 'root' and trigger and Path(trigger).exists():
            emit('controlled_root_exit', role=role)
            os._exit(0)
        should_fork = mode == 'late' and received and role == 'root'
        should_fork |= mode == 'kill-late' and role == 'grandchild' and trigger and Path(trigger).exists()
        if should_fork and not late:
            late = True
            if os.fork() == 0:
                role = 'late-descendant'
                emit('fixture_ready', role=role, mode=mode, deadline=end)
        time.sleep(.005)
    emit('finite_exit', role=role)
    os._exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('normal', 'resistant', 'tree', 'root-first', 'late', 'kill-late', 'migration'))
    parser.add_argument('--duration', type=float, default=25)
    parser.add_argument('--trigger')
    parser.add_argument('--native')
    args = parser.parse_args()
    run(args.mode, args.duration, args.trigger, args.native)
