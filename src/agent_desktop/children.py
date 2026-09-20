"""Owned direct-child observation; requests never own the sole reaper reference."""
from __future__ import annotations

import subprocess


class Children:
    """Popen.poll is the only reaper; no GLib child-watch or second waitpid.

    Output goes to already-open private files or DEVNULL, never a PIPE. Callers
    may use nonblocking bounded drains separately, but cannot call communicate.
    poll() remains registered with the worker after a request fails/times out.
    """
    def __init__(self):
        self.owned = set()

    def start(self, argv, *, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=None, cwd=None):
        if stdout == subprocess.PIPE or stderr == subprocess.PIPE:
            raise ValueError("Undrained child pipes are not supported")
        child = Child(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=stderr, env=env, cwd=cwd, close_fds=True))
        self.owned.add(child)
        return child

    def poll(self, now=None):
        for child in tuple(self.owned):
            if child.process.poll() is not None:
                self.owned.remove(child)

    def close(self):
        # Worker process shutdown is the ownership boundary. SIGKILL does not
        # prove compositor cleanup or descendant termination; later cgroups own it.
        for child in tuple(self.owned):
            child.abort()
        self.poll()


class Child:
    def __init__(self, process):
        self.process = process
        self.aborted = False

    @property
    def returncode(self):
        return self.process.returncode

    def abort(self):
        if self.aborted:
            return
        self.aborted = True
        # Popen.kill polls under its waitpid lock before signalling, preventing
        # signalling a recycled pid after a previous poll reaped this child.
        self.process.kill()
