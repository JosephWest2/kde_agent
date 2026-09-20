"""Short generation mutations. Lock order: name, generation, nonblocking store.

No service/transport waits are allowed inside this lock. Service hooks never
acquire the name lock, which a controller may hold while waiting for those hooks.
"""
from contextlib import contextmanager
import fcntl
import os
import time

from .contracts import ContractError
from .runtime import check_directory, check_file
from .protocol import decode


@contextmanager
def generation_lock(runtime, generation):
    root = runtime.socket_path(generation).parent
    check_directory(root)
    fd = os.open(root / 'cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        check_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError('session_unavailable', 'Generation mutation is busy.', outcome='unknown') from None
        yield root
    finally:
        os.close(fd)


def current(runtime, data):
    if runtime.read(data['session']) != data['generation']:
        raise ContractError('generation_mismatch', 'Generation is no longer current.')


def read_record(root, filename):
    fd = os.open(root / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        check_file(fd)
        return decode(os.read(fd, 16385))
    finally:
        os.close(fd)


def identity_record(root, filename, data):
    value = read_record(root, filename)
    if (type(value.get('schema_version')) is not int or value.get('schema_version') != 1 or value.get('session') != data['session']
            or value.get('generation') != data['generation']):
        raise ContractError('protocol_error', 'Invalid generation record.')
    return value


def intent(runtime, data, origin, request_id):
    """Caller owns generation lock; the earliest explicit stop cannot be renewed."""
    from .lifecycle import atomic
    root = runtime.socket_path(data['generation']).parent
    current(runtime, data)
    try:
        return identity_record(root, 'stop-intent.json', data)
    except FileNotFoundError:
        value = dict(schema_version=1, session=data['session'], generation=data['generation'],
                     origin=origin, request_id=request_id, admitted_at=time.monotonic(),
                     prior_state=data['state'])
        atomic(root / 'stop-intent.json', value)
        return value


def write_metadata(runtime, data):
    from .lifecycle import atomic, read_metadata
    with generation_lock(runtime, data['generation']) as root:
        try:
            existing = read_metadata(runtime, data['session'], data['generation'])
        except FileNotFoundError:
            existing = None
        if existing and existing['state'] in ('failed', 'stopped'):
            if data['state'] != 'failed':
                data['state'] = existing['state']
        atomic(root / 'lifecycle.json', data)
