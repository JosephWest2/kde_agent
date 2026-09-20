"""Internal real service probe; uses the installed production Desktop owner."""
import json
import os
from pathlib import Path
import sys

from agent_desktop.contracts import ContractError
from agent_desktop.environment import PROTECTED, FIXED, DISABLED
from agent_desktop.worker import run

name, generation, artifacts, project, binary, fault = sys.argv[1:7]
root = Path(artifacts) / 'generations' / generation
# Simulate a contaminated worker without changing its control routing root.
for key in PROTECTED - {'XDG_RUNTIME_DIR'}:
    os.environ[key] = 'HOST_CONTAMINATION'
os.environ['KWIN_SCREENSHOT_NO_PERMISSION_CHECKS'] = 'HOST_CONTAMINATION'
os.environ['SECRET'] = 'HOST_CONTAMINATION'
launched = []
reported = False


def identity(child):
    pid = child.process.pid
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19]}


def environment(child):
    try:
        raw = Path('/proc', str(child.process.pid), 'environ').read_bytes()
        return dict(part.decode().split('=', 1) for part in raw.split(b'\0') if part)
    except PermissionError:
        return 'proc_environ_permission_denied'


def observe(desktop):
    global reported
    if desktop.phase != 'constructed' or reported:
        return
    if not launched:
        rejected = []
        for key in [*PROTECTED, 'KWIN_SCREENSHOT_NO_PERMISSION_CHECKS', 'KWIN_EIS_NO_PERMISSION_CHECKS']:
            try:
                desktop.launch(['/usr/bin/touch', str(root / 'forbidden-launch')], '/', {key: 'bad'},
                               stdout=None, stderr=None)
            except ContractError as error:
                assert error.code == 'invalid_arguments'
                rejected.append(key)
            else:
                raise AssertionError('Protected override launched a child')
        (root / 'rejected.json').write_text(json.dumps(sorted(rejected)))
        for kind in ('application', 'adapter'):
            with (root / (kind + '.stdout')).open('w') as output, (root / (kind + '.stderr')).open('w') as error:
                launched.append(desktop.launch(['python', '-I', str(Path(__file__).with_name('desktop_reporter.py')),
                    str(root / (kind + '.json')), project], project, {'APP_FLAG': kind, 'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin'}, stdout=output, stderr=error))
        with (root / 'native.jsonl').open('w') as output, (root / 'native.stderr').open('w') as error:
            launched.append(desktop.launch([binary], project, {'HARNESS_GENERATION': generation}, stdout=output, stderr=error))
        (root / 'owner.json').write_text(json.dumps({'phase': desktop.phase, 'private': desktop.private,
            'bus': identity(desktop.bus), 'compositor': identity(desktop.compositor),
            'bus_observed_environment': environment(desktop.bus),
            'compositor_observed_environment': environment(desktop.compositor),
            'compositor_launch_environment': desktop.compositor_env,
            'children': [identity(child) for child in launched], 'desktop_ready': False}))
    if any(child.returncode is None for child in launched):
        return
    assert all(child.returncode == 0 for child in launched), [child.returncode for child in launched]
    output = [json.loads(line) for line in (root / 'native.jsonl').read_text().splitlines()]
    geometry = next(row for row in output if row['event'] == 'output')
    assert {k: geometry[k] for k in ('count', 'width', 'height', 'scale')} == {'count': 1, 'width': 1280, 'height': 720, 'scale': 1}
    desktop.store.provenance(output={k: geometry[k] for k in ('width', 'height', 'scale')})
    (root / 'probe-complete').write_text('complete')
    reported = True
    if fault == 'bus':
        desktop.bus.abort()


run(name, generation, artifacts=artifacts, managed=True, desktop=True, desktop_observer=observe)
