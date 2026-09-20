"""Private disposable routing, never durable session health or artifact state."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import os
from pathlib import Path
import socket
import stat
import struct
import uuid
import time
from .contracts import ContractError, GENERATION, NAME
from .protocol import decode


def failure(code="transport_error"):
    raise ContractError(code, "Session runtime is unavailable or unsafe.")


def check_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        failure()


def check_file(fd, *, regular=True):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or (regular and not stat.S_ISREG(info.st_mode)):
        failure()
    return info


def peer_owner(sock):
    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        failure()


class Runtime:
    def __init__(self, *, create=False):
        raw = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        root = Path(raw)
        if not root.is_absolute():
            failure()
        try:
            # Ancestors need not belong to us (e.g. /run); none may be a symlink.
            for path in (root, *root.parents):
                if path.is_symlink():
                    failure()
            check_directory(root)
            self.root = root / "agent-desktop"
            self.current = self.root / "current"
            self.generations = self.root / "g"
            for path in (self.root, self.current, self.generations):
                if create:
                    try:
                        path.mkdir(mode=0o700)
                    except FileExistsError:
                        pass
                check_directory(path)
        except FileNotFoundError:
            failure("session_not_found")
        except OSError:
            failure()

    def socket_path(self, generation, *, priority=False):
        if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
            failure("protocol_error")
        path = self.generations / generation / ("priority.sock" if priority else "control.sock")
        if len(os.fsencode(path)) >= 108:
            failure()
        return path

    @contextmanager
    def lock(self, name, *, deadline=None):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            failure("protocol_error")
        fd = os.open(self.current / (name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            check_file(fd)
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ContractError("timeout", "Lifecycle lock deadline expired.")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is None:
                        failure("session_conflict")
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(fd)

    def read(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            failure("protocol_error")
        try:
            fd = os.open(self.current / (name + ".json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                check_file(fd)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    value = decode(stream.read(4097))
            finally:
                os.close(fd)
        except FileNotFoundError:
            failure("session_not_found")
        except OSError:
            failure()
        if (set(value) != {"schema_version", "session", "generation"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["session"] != name or not isinstance(value["generation"], str)
                or not GENERATION.fullmatch(value["generation"])):
            failure("protocol_error")
        return value["generation"]

    def discover(self, name, expected, *, priority=False):
        generation = self.read(name)
        if expected is not None and generation != expected:
            raise ContractError("generation_mismatch", "Expected generation differs from current routing.",
                                context={"expected_generation": expected, "resolved_generation": generation})
        path = self.socket_path(generation, priority=priority)
        try:
            check_directory(path.parent)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                failure()
        except FileNotFoundError:
            failure("session_unavailable")
        except OSError:
            failure()
        return generation, path


class Endpoint:
    def __init__(self, name, generation, *, managed=False):
        import json
        if not isinstance(name, str) or not NAME.fullmatch(name):
            failure("protocol_error")
        self.runtime = Runtime(create=True)
        self.name, self.generation = name, generation
        self.path = self.runtime.socket_path(generation)
        self.priority_path = self.runtime.socket_path(generation, priority=True)
        self.priority_listener = None
        self.priority_inode = None
        self.listener = None
        self.inode = None
        self.published = False
        try:
            # Never remove this exclusive lifetime claim, even after failure/exit.
            if managed:
                from .lifecycle import read_metadata
                read_metadata(self.runtime, name, generation)
                if self.runtime.read(name) != generation:
                    failure("generation_mismatch")
            else:
                try:
                    self.path.parent.mkdir(mode=0o700)
                except FileExistsError:
                    failure("session_conflict")
            with nullcontext() if managed else self.runtime.lock(name):
                pointer = self.runtime.current / (name + ".json")
                if not managed and os.path.lexists(pointer):
                    failure("session_conflict")
                self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                old_mask = os.umask(0o177)
                try:
                    self.listener.bind(str(self.path))
                finally:
                    os.umask(old_mask)
                self.inode = self.path.lstat().st_ino
                self.listener.listen(32)
                self.listener.setblocking(False)
                self.priority_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                old_mask = os.umask(0o177)
                try:
                    self.priority_listener.bind(str(self.priority_path))
                finally:
                    os.umask(old_mask)
                self.priority_inode = self.priority_path.lstat().st_ino
                self.priority_listener.listen(8)
                self.priority_listener.setblocking(False)
                if managed:
                    return
                temp = self.runtime.current / ("." + uuid.uuid4().hex)
                try:
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(dict(schema_version=1, session=name, generation=generation), stream)
                    os.replace(temp, pointer)
                    self.published = True
                finally:
                    temp.unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.priority_listener is not None:
            self.priority_listener.close()
            self.priority_listener = None
        if self.priority_inode is not None:
            try:
                info = self.priority_path.lstat()
                if info.st_ino == self.priority_inode and info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode):
                    self.priority_path.unlink()
            except FileNotFoundError:
                pass
            self.priority_inode = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.inode is not None:
            try:
                info = self.path.lstat()
                if info.st_ino == self.inode and info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.inode = None
        if self.published:
            try:
                with self.runtime.lock(self.name):
                    if self.runtime.read(self.name) == self.generation:
                        (self.runtime.current / (self.name + ".json")).unlink()
            except (ContractError, OSError):
                # Never remove a replacement or unsafe pointer during cleanup.
                pass
            self.published = False
