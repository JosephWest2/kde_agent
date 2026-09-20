"""Explicit private environment and executable policy; never launch or read ambient env."""
import os
from pathlib import Path
from .contracts import ContractError

SETTINGS = frozenset({'HOME', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                      'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_DIRS'})
ENDPOINTS = frozenset({'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET',
                       'DBUS_SESSION_BUS_ADDRESS', 'DBUS_SESSION_BUS_PID',
                       'DBUS_SESSION_BUS_WINDOWID', 'DBUS_SYSTEM_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS'})
DISABLED = {'QT_ACCESSIBILITY': '0', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON': '0', 'NO_AT_BRIDGE': '1'}
PROTECTED = SETTINGS | ENDPOINTS | DISABLED.keys()


def check_overrides(overrides):
    if PROTECTED.intersection(overrides):
        raise ContractError('invalid_arguments', 'Private session environment cannot be overridden.',
                            context={'field': 'env'})


def compose(base, overrides, private):
    """Return launch-ready values only when every required private root is present.

    M3 supplies and provisions these roots. This helper does not create a desktop.
    The private system-bus address deliberately names an absent socket.
    """
    check_overrides(overrides)
    required = SETTINGS | {'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'DBUS_SYSTEM_BUS_ADDRESS'}
    if not required <= private.keys() or set(private) - (required | DISABLED.keys()):
        raise ContractError('session_unavailable', 'Private environment is incomplete.')
    if any(not isinstance(v, str) or not v or '\0' in v for v in private.values()):
        raise ContractError('session_unavailable', 'Private environment is invalid.')
    root = Path(private['XDG_RUNTIME_DIR'])
    if not root.is_absolute():
        raise ContractError('session_unavailable', 'Private runtime must be absolute.')
    for key in SETTINGS:
        path = Path(private[key])
        if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            raise ContractError('session_unavailable', 'Private settings must belong to the runtime tree.')
    for key in ('DBUS_SESSION_BUS_ADDRESS', 'DBUS_SYSTEM_BUS_ADDRESS'):
        value = private[key]
        if not value.startswith('unix:path=') or any(c in value[len('unix:path='):] for c in ';,%='):
            raise ContractError('session_unavailable', 'Private bus address is invalid.')
        path = Path(value[len('unix:path='):])
        if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            raise ContractError('session_unavailable', 'Private bus address escapes runtime.')
        if key == 'DBUS_SYSTEM_BUS_ADDRESS' and os.path.lexists(path):
            raise ContractError('session_unavailable', 'Disabled system-bus endpoint exists.')
    wayland = private['WAYLAND_DISPLAY']
    if wayland in ('.', '..'):
        raise ContractError('session_unavailable', 'Private Wayland endpoint is invalid.')
    if '/' in wayland and (not Path(wayland).is_absolute() or not Path(wayland).resolve().is_relative_to(root.resolve())):
        raise ContractError('session_unavailable', 'Private Wayland endpoint is invalid.')
    if any(private.get(k, v) != v for k, v in DISABLED.items()):
        raise ContractError('session_unavailable', 'Private accessibility policy is invalid.')
    result = {k: v for k, v in base.items() if k not in PROTECTED}
    result.setdefault('PATH', os.defpath)
    result.update(overrides)
    result.update(private)
    result.update(DISABLED)
    return result


def executable(argv0, cwd, environment):
    """Select an absolute executable without changing argv[0] or worker cwd."""
    if not os.path.isabs(cwd) or not argv0 or '\0' in argv0:
        raise ContractError('invalid_arguments', 'Invalid executable or working directory.')
    if '/' in argv0:
        candidates = [os.path.normpath(os.path.join(cwd, argv0))]
        kind = 'slash'
    else:
        candidates = [os.path.normpath(os.path.join(cwd, part, argv0))
                      for part in environment.get('PATH', os.defpath).split(':')]
        kind = 'path'
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return {'executable': candidate, 'lookup': kind}
    raise ContractError('prerequisite_missing', 'Application executable is unavailable.')
