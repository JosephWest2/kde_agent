"""Trusted gated helper; launched with isolated Python and a clean environment."""
import json
import os
import select
import sys
import time


def main():
    group, config, gate, status = map(int, sys.argv[1:5])
    deadline = float(sys.argv[5])
    os.set_inheritable(status, False)
    try:
        fd = os.open('cgroup.procs', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=group)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        os.close(group)
        with os.fdopen(config, 'rb') as stream:
            data = json.load(stream)
        os.write(status, b'R')
        remaining = deadline - time.monotonic()
        poller = select.poll()
        poller.register(gate, select.POLLIN | select.POLLHUP)
        if remaining <= 0 or not poller.poll(max(1, int(remaining * 1000))):
            return 125
        if os.read(gate, 1) != b'G' or time.monotonic() >= deadline:
            return 125
        os.close(gate)
        os.chdir(data['cwd'])
        os.write(status, b'X')
        os.execve(data['executable'], data['argv'], data['env'])
    except BaseException as error:
        try:
            os.write(status, b'E' + str(getattr(error, 'errno', 0) or 0).encode())
        except OSError:
            pass
        return 126


if __name__ == '__main__':
    raise SystemExit(main())
