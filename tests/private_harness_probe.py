#!/usr/bin/python3
"""Real fixture/control/environment negatives, executed inside the M1 service."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("harness", project / "tools/private_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
generation = os.environ["HARNESS_GENERATION"]
runtime = Path(os.environ["XDG_RUNTIME_DIR"])
artifacts = Path(os.environ["HARNESS_ARTIFACTS"])
control_path = os.environ["HARNESS_CONTROL"]
expected = harness.clean_env(runtime, generation)
for key, value in expected.items():
    assert os.environ.get(key) == value, key
for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_SOCKET", "AT_SPI_BUS_ADDRESS", "PYTHONPATH", "LD_PRELOAD", "KDE_APPLICATIONS_AS_SCOPE"):
    assert key not in os.environ, key
assert Path.cwd() == Path(sys.argv[1])
manifest = json.loads((artifacts / "manifest.json").read_text())
owned_cgroup = manifest["worker"]["cgroup"]
assert Path('/proc/self/cgroup').read_text().strip() == owned_cgroup
checks = {}
for label, path in {"runtime": runtime, "home": runtime / "home", "bus": runtime / "bus", "control": runtime / "control", "wayland": runtime / expected["WAYLAND_DISPLAY"]}.items():
    st = path.stat()
    assert st.st_uid == os.getuid() and not (stat.S_IMODE(st.st_mode) & 0o077), (label, oct(st.st_mode))
    checks[label] = {"mode": oct(stat.S_IMODE(st.st_mode)), "owner_matches": True}
environment_observations = {}
for name, process in manifest["processes"].items():
    assert Path(f'/proc/{process["pid"]}/cgroup').read_text().strip() == owned_cgroup, name
    try:
        entries = Path(f'/proc/{process["pid"]}/environ').read_bytes().split(b'\0')
    except PermissionError:
        assert name == 'kwin', name
        environment_observations[name] = 'proc_environ_denied; explicit launch environment and private endpoints recorded'
        continue
    environment_observations[name] = 'protected actual process environment checked'
    env = dict(item.decode().split('=', 1) for item in entries if item)
    for key in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY"):
        assert env[key] == expected[key], (name, key)
    assert "DISPLAY" not in env and "AT_SPI_BUS_ADDRESS" not in env, name

def raw(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect(control_path)
        sock.sendall(payload)
        data = b''
        while b'\n' not in data:
            chunk = sock.recv(32768)
            assert chunk
            data += chunk
        return json.loads(data)

before = harness.control(control_path, generation, "status")["presented"]
negative = []
for request, expected_code in [
    ({"generation": "0" * 32, "request_id": "wrong-generation", "op": "set_state", "state": 999}, "generation_mismatch"),
    ({"generation": generation, "request_id": "invalid-state", "op": "set_state", "state": -1}, "invalid_request"),
    ([], "invalid_request"),
]:
    response = raw(json.dumps(request).encode() + b'\n')
    assert not response["ok"] and response["code"] == expected_code, response
    negative.append(response)
response = raw(b'x' * 17000 + b'\n')
assert not response['ok'] and response['code'] == 'invalid_request', response
negative.append(response)
response = raw(b'{bad json}\n')
assert not response['ok'] and response['code'] == 'invalid_request', response
negative.append(response)
after = harness.control(control_path, generation, "status")["presented"]
assert before["revision"] == after["revision"], (before, after)
results = []
for state in (0, 17, 42, 0xffffffff):
    result = harness.control(control_path, generation, "set_state", state)
    event = result["presented"]
    assert event["state"] == state and event["source"] == "control"
    assert event["revision"] > after["revision"]
    assert event["sole_output_name"] == "Virtual-0"
    results.append(result)
    after = event
harness.atomic(artifacts / "boundary-probe.json", {"generation": generation, "cwd": str(Path.cwd()),
               "owner_modes": checks, "environment_observations": environment_observations, "cgroup_membership_checked": True,
               "negative_results": negative, "state_results": results})
