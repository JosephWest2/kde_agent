"""Tests-only controller for internal infrastructure start, never installed."""
import json
from pathlib import Path
import sys

# Optional explicit source location is only for source-tree regression tests.
operation, name, artifacts, expected, source = sys.argv[1:6]
if source != '-':
    sys.path.insert(0, source)
from agent_desktop.contracts import ContractError, make_request, response
from agent_desktop.lifecycle import Manager, Systemd

request = make_request(operation, caller_cwd=str(Path.cwd()), session=name,
                       expected_generation=None if expected == '-' else expected,
                       arguments={'artifacts': artifacts} if operation == 'session.start' else {})
def worker(data):
    return [sys.executable, '-I', str(Path(__file__).with_name('lifecycle_worker_fixture.py')),
            data['session'], data['generation'], data['configuration']['artifacts'], source]
try:
    helper = None if source == '-' else [sys.executable, '-I', '-c',
        "import sys,runpy;sys.path.insert(0," + repr(source) + ");runpy.run_module('agent_desktop.service_cleanup',run_name='__main__')"]
    manager = Manager(worker_command=worker, systemd=Systemd(helper_command=helper))
    value = manager.start(request) if operation == 'session.start' else manager.handle(request)
except ContractError as error:
    value = response(request.request_id, request.operation, session=name, error=error)
print(json.dumps(value))
raise SystemExit(0 if value['ok'] else 4)
