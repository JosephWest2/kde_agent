"""Real trusted child: report only its deliberately clean constructed environment."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from agent_desktop.environment import FIXED, DISABLED, SETTINGS

env = dict(os.environ)
assert 'HOST_CONTAMINATION' not in env.values()
assert 'SECRET' not in env
assert 'DISPLAY' not in env and 'XAUTHORITY' not in env and 'AT_SPI_BUS_ADDRESS' not in env
assert not any(key.startswith('KWIN_') for key in env)
for key, value in (FIXED | DISABLED).items():
    assert env[key] == value, (key, env[key])
root = Path(env['XDG_RUNTIME_DIR'])
assert all(Path(env[key]).is_relative_to(root) for key in SETTINGS)
for name in ('bus', env['WAYLAND_DISPLAY']):
    info = (root / name).stat()
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
assert not Path(env['DBUS_SYSTEM_BUS_ADDRESS'].removeprefix('unix:path=')).exists()
project = Path(sys.argv[2]) / 'README.md'
raw = project.read_bytes()
assert os.getcwd() == sys.argv[2]
Path(sys.argv[1]).write_text(json.dumps({'environment': env, 'cwd': os.getcwd(),
    'project_file': str(project), 'project_sha256': hashlib.sha256(raw).hexdigest(),
    'cgroup': Path('/proc/self/cgroup').read_text()}))
print('project readable; private desktop endpoints and settings verified')
