"""Detect host facilities that only the real desktop host provides (docs/TESTING.md).

A missing facility skips the test with its reason. AGENT_DESKTOP_REQUIRE_HOST_TESTS=1
turns that skip into a failure, so a run on the real host cannot lose coverage.
"""
import functools
import os
from pathlib import Path
import platform
import subprocess

REQUIRE = 'AGENT_DESKTOP_REQUIRE_HOST_TESTS'


def require(test, missing):
    if missing is None:
        return
    if os.environ.get(REQUIRE) == '1':
        test.fail(missing + ' (' + REQUIRE + '=1 forbids skipping)')
    test.skipTest(missing)


@functools.cache
def user_systemd():
    """The user service manager, its user bus and a delegated app.slice cgroup."""
    runtime = '/run/user/' + str(os.getuid())
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': runtime,
           'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
    if not Path('/usr/bin/systemd-run').exists():
        return 'no /usr/bin/systemd-run'
    try:
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', 'app.slice', '-p', 'ControlGroup', '--value'],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 'no user service manager: ' + str(error)
    group = result.stdout.strip()
    if result.returncode or not group.endswith('/app.slice'):
        return 'no user service manager on ' + runtime + '/bus: ' + (result.stderr.strip() or repr(group))
    if not Path('/sys/fs/cgroup' + group).is_dir():
        return 'user app.slice cgroup is not visible at /sys/fs/cgroup' + group
    return None


@functools.cache
def libei():
    """An installed libei that agent_desktop.libei_binding accepts."""
    from agent_desktop import libei_binding
    try:
        libei_binding.describe()
    except (OSError, libei_binding.Unsupported) as error:
        return 'no supported libei: ' + str(error)
    return None


def libei_file(binding):
    return None if binding.SUPPORTED_LIBRARY.exists() else 'no ' + str(binding.SUPPORTED_LIBRARY)


def reviewed_libei(binding):
    """The exact libei build tools/libei_binding.py was audited against (hash-pinned)."""
    if platform.machine() != 'x86_64':
        return 'reviewed libei build is x86_64 only, not ' + platform.machine()
    if libei_file(binding):
        return libei_file(binding)
    if binding.digest(binding.SUPPORTED_LIBRARY.resolve()) != binding.SUPPORTED_LIBRARY_SHA256:
        return str(binding.SUPPORTED_LIBRARY) + ' is not the reviewed build (SHA-256 differs)'
    return None


def libei_headers(binding):
    """reviewed_libei plus the compiler, pkg-config entry and header the audit uses."""
    for path in ('/usr/bin/cc', '/usr/bin/pkg-config', '/usr/include/libei-1.0/libei.h'):
        if not Path(path).exists():
            return 'no ' + path
    if reviewed_libei(binding):
        return reviewed_libei(binding)
    # The audit's own sanitized environment: no caller PKG_CONFIG_PATH.
    try:
        result = binding.command(['/usr/bin/pkg-config', '--cflags', '--libs', 'libei-1.0'])
    except (OSError, subprocess.TimeoutExpired) as error:
        return 'pkg-config failed: ' + str(error)
    if result.returncode:
        return 'pkg-config cannot resolve libei-1.0: ' + (result.stderr.strip().splitlines() or ['exit ' + str(result.returncode)])[-1]
    return None
