#!/usr/bin/python3
"""Bounded M1 feasibility desktop. This is not the supported session CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
BOUNDS = dict(build=30, startup=30, probe=60, control=3, graceful=2,
              cleanup=15, finalization=5, manager_call=5, service_lifetime=95,
              overall=170)
MAX_MESSAGE = 16384


class Failure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def atomic(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    temporary.replace(path)


def private_dir(path):
    path = Path(path).absolute()
    # Existing ancestors must not redirect an apparently safe root through links.
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise Failure("unsafe_path", "Artifact/build paths must not contain symlinks")
    if path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()):
        raise Failure("unsafe_path", "Directory must be owned by the current user")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clean_env(runtime, generation):
    runtime = Path(runtime)
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": str(runtime / "home"), "XDG_RUNTIME_DIR": str(runtime),
            "XDG_CONFIG_HOME": str(runtime / "home/config"),
            "XDG_DATA_HOME": str(runtime / "home/data"),
            "XDG_CACHE_HOME": str(runtime / "home/cache"),
            "XDG_STATE_HOME": str(runtime / "home/state"),
            "XDG_CONFIG_DIRS": str(runtime / "empty"),
            "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
            "TMPDIR": str(runtime / "tmp"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus"),
            "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(runtime / "no-system-bus"),
            "WAYLAND_DISPLAY": "fixture-wayland", "QT_QPA_PLATFORM": "wayland",
            "QT_ACCESSIBILITY": "0", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "0",
            "NO_AT_BRIDGE": "1", "KDE_SESSION_VERSION": "6",
            "XDG_SESSION_TYPE": "wayland", "XKB_DEFAULT_LAYOUT": "us",
            "HARNESS_GENERATION": generation}


def manager_env():
    # Lifecycle transport only. Never give this environment to desktop children.
    root = Path(f"/run/user/{os.getuid()}")
    if root.stat().st_uid != os.getuid():
        raise Failure("manager_unavailable", "User runtime directory has the wrong owner")
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(root),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(root / "bus")}


def command(argv, *, env, timeout=5, cwd=None):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=1)
            raise Failure("timeout", f"{Path(argv[0]).name} exceeded {timeout:.2f}s")
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out.seek(0); err.seek(0)
        stdout, stderr = out.read(1024 * 1024), err.read(4096)
        if p.returncode:
            raise Failure("command_failed", f"{Path(argv[0]).name}: {stderr.decode(errors='replace').strip()}")
        return stdout.decode()


def unit_info(unit, seconds=5):
    raw = command(["/usr/bin/systemctl", "--user", "--no-pager", "show", unit,
                   "--property=LoadState,ActiveState,SubState,Result,ControlGroup,MainPID,ExecMainStatus"],
                  env=manager_env(), timeout=seconds)
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def cgroup_members(cgroup):
    if not cgroup or not cgroup.startswith("/") or ".." in Path(cgroup).parts:
        raise Failure("invalid_cgroup", "Missing or invalid owned control group")
    path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    if not path.exists():
        return []
    members = set()
    for procs in [path / "cgroup.procs", *path.rglob("cgroup.procs")]:
        try:
            members.update(int(line) for line in procs.read_text().split())
        except FileNotFoundError:
            pass
    return sorted(members)


def identity(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return {"pid": pid, "start_ticks": raw[raw.rfind(')') + 2:].split()[19],
                "cgroup": Path(f"/proc/{pid}/cgroup").read_text().strip()}
    except FileNotFoundError:
        return {"pid": pid, "exited": True}


def build(root):
    root = private_dir(root)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root)}
    started = time.monotonic()
    def run(args):
        remaining = BOUNDS["build"] - (time.monotonic() - started)
        if remaining <= 0:
            raise Failure("timeout", "Fixture build deadline expired")
        return command(args, env=env, timeout=remaining)
    protocols = Path(run(["/usr/bin/pkg-config", "--variable=pkgdatadir", "wayland-protocols"]).strip())
    sources = []
    for name in ("xdg-shell", "presentation-time"):
        xml = protocols / "stable" / name / (name + ".xml")
        header = root / (name + "-client-protocol.h")
        code = root / (name + "-protocol.c")
        run(["/usr/bin/wayland-scanner", "client-header", str(xml), str(header)])
        run(["/usr/bin/wayland-scanner", "private-code", str(xml), str(code)])
        sources.append((xml, code))
    flags = run(["/usr/bin/pkg-config", "--cflags", "--libs", "wayland-client", "xkbcommon"]).split()
    binary = root / "wayland-fixture"
    argv = ["/usr/bin/gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(root),
            str(PROJECT / "tools/wayland_fixture.c"), *(str(code) for _, code in sources), *flags, "-o", str(binary)]
    run(argv)
    receipt = {"source_sha256": digest(PROJECT / "tools/wayland_fixture.c"),
               "binary_sha256": digest(binary), "compiler": run(["/usr/bin/gcc", "--version"]).splitlines()[0],
               "argv": argv, "protocols": {str(xml): digest(xml) for xml, _ in sources},
               "elapsed_seconds": round(time.monotonic() - started, 3)}
    atomic(root / "build.json", receipt)
    return binary, receipt


def control(path, generation, operation, state=None, request_id=None, timeout=3):
    request = {"generation": generation, "request_id": request_id or uuid.uuid4().hex, "op": operation}
    if state is not None:
        request["state"] = state
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(MAX_MESSAGE - len(data) + 1)
            if not chunk:
                raise Failure("control_closed", "Fixture control connection closed")
            data += chunk
            if len(data) > MAX_MESSAGE:
                raise Failure("control_limit", "Fixture response exceeded limit")
        result = json.loads(data)
        if result.get("generation") != generation or result.get("request_id") != request["request_id"]:
            raise Failure("control_identity", "Control response identity mismatch")
        if not result.get("ok"):
            raise Failure(result.get("code", "control_failed"), result.get("message", "Fixture request failed"))
        return result


class Worker:
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.data = json.loads(self.path.read_text())
        self.artifacts = self.path.parent
        self.runtime = Path(self.data["runtime"])
        self.env = clean_env(self.runtime, self.data["generation"])
        self.selector = selectors.DefaultSelector()
        self.children = {}
        self.control_counter = 0
        self.active_control_id = None
        self.latest = None
        self.output = None
        self.event_buffer = b""
        self.client = None
        self.client_buffer = b""
        self.request = None
        self.target_revision = None
        self.client_deadline = None
        self.stopping = False
        self.streams = []
        self.started = time.monotonic()
        self.event_log = (self.artifacts / "fixture-events.jsonl").open("ab", buffering=0)

    def save(self):
        atomic(self.path, self.data)

    def spawn(self, name, argv, *, pipe=False, env=None):
        out = subprocess.PIPE if pipe else (self.artifacts / (name + ".stdout.log")).open("wb")
        err = (self.artifacts / (name + ".stderr.log")).open("wb")
        if not pipe:
            self.streams.append(out)
        self.streams.append(err)
        p = subprocess.Popen(argv, cwd=self.data["cwd"], env=env or self.env,
                             stdin=subprocess.PIPE if pipe else subprocess.DEVNULL,
                             stdout=out, stderr=err)
        self.children[name] = p
        self.data.setdefault("processes", {})[name] = identity(p.pid) | {"argv": argv, "cwd": self.data["cwd"]}
        self.save()
        return p

    def respond(self, ok, **result):
        if self.client:
            payload = {"generation": self.data["generation"],
                       "request_id": self.request.get("request_id") if self.request else None,
                       "ok": ok, **result}
            try:
                self.client.sendall(json.dumps(payload).encode() + b"\n")
            except (OSError, TimeoutError):
                pass
            self.selector.unregister(self.client)
            self.client.close()
        self.client = self.request = self.target_revision = self.client_deadline = None
        self.active_control_id = None
        self.client_buffer = b""

    def read_request(self):
        try:
            chunk = self.client.recv(MAX_MESSAGE + 1)
            if not chunk:
                self.respond(False, code="disconnected")
                return
            self.client_buffer += chunk
            if len(self.client_buffer) > MAX_MESSAGE:
                raise Failure("invalid_request", "Request exceeds 16 KiB")
            if b"\n" not in self.client_buffer:
                return
            request = json.loads(self.client_buffer)
            if not isinstance(request, dict):
                raise Failure("invalid_request", "Request must be an object")
            self.request = request
            if request.get("generation") != self.data["generation"]:
                raise Failure("generation_mismatch", "Generation differs from this fixture")
            if not isinstance(request.get("request_id"), str) or not 1 <= len(request["request_id"]) <= 64:
                raise Failure("invalid_request", "Request ID must be 1–64 characters")
            op = request.get("op")
            if op == "status":
                self.respond(True, presented=self.latest, output=self.output)
            elif op == "set_state":
                value = request.get("state")
                if type(value) is not int or not 0 <= value <= 0xffffffff:
                    raise Failure("invalid_request", "State must be a uint32")
                self.control_counter += 1
                self.active_control_id = self.control_counter
                self.children["fixture"].stdin.write(f"state {value} {self.active_control_id}\n".encode())
                self.children["fixture"].stdin.flush()
            elif op == "close":
                self.children["fixture"].stdin.write(b"close\n")
                self.children["fixture"].stdin.flush()
                self.respond(True, close_requested=True)
                self.stopping = True
            else:
                raise Failure("invalid_request", "Unknown fixture operation")
        except (ValueError, Failure, OSError) as exc:
            self.respond(False, code=getattr(exc, "code", "invalid_request"), message=str(exc))

    def read_events(self):
        chunk = os.read(self.children["fixture"].stdout.fileno(), 65536)
        if not chunk:
            self.selector.unregister(self.children["fixture"].stdout)
            return
        self.event_log.write(chunk)
        self.event_buffer += chunk
        if len(self.event_buffer) > 1024 * 1024:
            raise Failure("fixture_protocol", "Fixture event buffer exceeded limit")
        while b"\n" in self.event_buffer:
            line, self.event_buffer = self.event_buffer.split(b"\n", 1)
            event = json.loads(line)
            if event.get("generation") != self.data["generation"]:
                raise Failure("fixture_protocol", "Fixture event generation mismatch")
            kind = event["event"]
            if kind == "error":
                raise Failure("fixture_failed", event["message"])
            if kind == "output":
                self.output = event
            elif kind == "presented":
                self.latest = event
            if (self.request and self.request.get("op") == "set_state" and
                    event.get("control_id") == self.active_control_id):
                if kind == "control_busy":
                    self.respond(False, code="fixture_busy", message="A rendered update is still pending")
                elif kind == "committed" and event["source"] == "control" and self.target_revision is None:
                    self.target_revision = event["revision"]
                elif kind in ("presented", "discarded") and event["revision"] == self.target_revision:
                    if kind == "presented":
                        self.respond(True, presented=event)
                    else:
                        self.respond(False, code="render_discarded", message="Requested revision was discarded", revision=event["revision"])

    def tick(self, seconds=0.05):
        for key, _ in self.selector.select(seconds):
            if key.data == "events":
                self.read_events()
            elif key.data == "listen":
                client, _ = key.fileobj.accept()
                client.settimeout(0.1)
                if self.client:
                    client.close()
                else:
                    self.client = client
                    self.client_deadline = time.monotonic() + BOUNDS["control"]
                    self.selector.register(client, selectors.EVENT_READ, "client")
            elif key.data == "client":
                if key.fileobj is not self.client:
                    continue
                if self.request:
                    # One request per connection. A disconnect abandons the waiter,
                    # never rewrites the immutable pending fixture frame.
                    try:
                        if self.client.recv(1):
                            self.respond(False, code="invalid_request", message="One request per connection")
                        else:
                            self.respond(False, code="disconnected")
                    except BlockingIOError:
                        pass
                    except OSError:
                        self.respond(False, code="disconnected")
                else:
                    self.read_request()
        if self.client_deadline and time.monotonic() >= self.client_deadline:
            self.respond(False, code="render_timeout", message="Fixture control deadline expired; requested state may still render", uncertain_state=True)
        for name in ("bus", "kwin", "fixture"):
            if name in self.children and self.children[name].poll() is not None and not self.stopping:
                raise Failure("essential_exit", f"{name} exited {self.children[name].returncode}")

    def run(self):
        self.data["worker"] = identity(os.getpid())
        self.save()
        config = self.runtime / "bus.conf"
        config.write_text('<busconfig><type>session</type><listen>' + self.env["DBUS_SESSION_BUS_ADDRESS"] +
                          '</listen><auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>'
                          '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>')
        self.spawn("bus", ["/usr/bin/dbus-daemon", "--nofork", "--config-file=" + str(config)])
        deadline = self.started + BOUNDS["startup"]
        while not (self.runtime / "bus").is_socket():
            if time.monotonic() >= deadline:
                raise Failure("startup_timeout", "Private bus did not create its socket")
            self.tick()
        os.chmod(self.runtime / "bus", 0o600)
        command(["/usr/bin/dbus-send", "--address=" + self.env["DBUS_SESSION_BUS_ADDRESS"], "--type=method_call",
                 "--print-reply", "--reply-timeout=1000", "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                 "org.freedesktop.DBus.Hello"], env=self.env, timeout=min(2, deadline - time.monotonic()))
        self.spawn("kwin", ["/usr/bin/kwin_wayland", "--virtual", "--width", "1280", "--height", "720",
                            "--scale", "1", "--output-count", "1", "--socket", self.env["WAYLAND_DISPLAY"],
                            "--no-lockscreen", "--no-global-shortcuts", "--no-kactivities"])
        while not (self.runtime / self.env["WAYLAND_DISPLAY"]).is_socket():
            if time.monotonic() >= deadline:
                raise Failure("startup_timeout", "KWin did not create its private display")
            self.tick()
        os.chmod(self.runtime / self.env["WAYLAND_DISPLAY"], 0o600)
        fixture = self.spawn("fixture", [self.data["fixture_binary"]], pipe=True)
        self.selector.register(fixture.stdout, selectors.EVENT_READ, "events")
        while self.latest is None:
            if time.monotonic() >= deadline:
                raise Failure("startup_timeout", "Fixture initial presentation did not arrive")
            self.tick()
        if self.data["inject"] in ("after-fixture", "startup-timeout"):
            self.spawn("descendant", ["/usr/bin/python", "-I", str(SCRIPT), "_descendant", str(self.artifacts / "descendant.json")])
            # Wait for the resistant grandchild identity before injecting failure.
            while not (self.artifacts / "descendant.json").exists():
                if time.monotonic() >= deadline:
                    raise Failure("startup_timeout", "Descendant did not initialize")
                self.tick()
            if self.data["inject"] == "startup-timeout":
                # Exercise the real shared startup deadline, with no readiness.
                while time.monotonic() < deadline:
                    self.tick()
                raise Failure("startup_timeout", "Injected startup stall reached deadline")
            raise Failure("injected_startup_failure", "Injected failure after fixture presentation")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.runtime / "control")); listener.listen(4)
        os.chmod(self.runtime / "control", 0o600)
        self.selector.register(listener, selectors.EVENT_READ, "listen")
        self.data.update(phase="foundation_ready", output=self.output, initial_presented=self.latest,
                         startup_seconds=round(time.monotonic() - self.started, 3), environment=self.env)
        self.save()
        if self.data["inject"] == "worker-kill":
            self.data["cleanup_started_monotonic_ns"] = time.monotonic_ns()
            self.save()
            os.kill(os.getpid(), signal.SIGKILL)
        argv = self.data["probe"] or ["/usr/bin/python", "-I", str(SCRIPT), "_smoke"]
        probe_env = self.env | {"HARNESS_CONTROL": str(self.runtime / "control"),
                               "HARNESS_ARTIFACTS": str(self.artifacts),
                               "HARNESS_DEADLINE": str(time.monotonic() + BOUNDS["probe"])}
        probe = self.spawn("probe", argv, env=probe_env)
        deadline = time.monotonic() + BOUNDS["probe"]
        while probe.poll() is None:
            if time.monotonic() >= deadline:
                raise Failure("probe_timeout", "Probe deadline expired")
            self.tick()
        if probe.returncode:
            raise Failure("probe_failed", f"Probe exited {probe.returncode}")
        self.data.update(phase="complete", outcome="passed", probe_exit=probe.returncode)
        self.save()

    def close(self):
        self.stopping = True
        self.data["cleanup_started_monotonic_ns"] = time.monotonic_ns()
        self.save()
        fixture = self.children.get("fixture")
        if fixture and fixture.poll() is None:
            try:
                fixture.stdin.write(b"close\n"); fixture.stdin.flush()
                deadline = time.monotonic() + BOUNDS["graceful"]
                while fixture.poll() is None and time.monotonic() < deadline:
                    self.tick(0.01)
                self.tick(0)
                self.data["fixture_close_exit"] = fixture.poll()
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.data["worker_elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.save()
        # Remaining ordinary descendants belong to systemd, including new process
        # groups. Do not pretend a direct-child wait proves unit cleanup.


def worker(manifest):
    w = Worker(manifest)
    try:
        w.run()
        return 0
    except Exception as exc:
        w.data.update(phase="failed", outcome="failed", error={"code": getattr(exc, "code", "internal_error"), "message": str(exc)})
        w.save()
        return 1
    finally:
        w.close()


def finalize(manifest):
    path = Path(manifest)
    data = json.loads(path.read_text())
    runtime = Path(data["runtime"])
    marker = runtime / "owner.json"
    if runtime.exists():
        if (runtime.is_symlink() or runtime.stat().st_uid != os.getuid() or
                json.loads(marker.read_text()) != {"generation": data["generation"]} or
                runtime.parent != Path("/tmp") or not runtime.name.startswith("kde-m1-")):
            raise Failure("unsafe_cleanup", "Disposable runtime ownership check failed")
        shutil.rmtree(runtime)
    # Stop-post is deliberately separate from the worker manifest: no lost-update
    # race and no claim that this hook proves the entire cgroup is empty.
    service_result = os.environ.get("SERVICE_RESULT", "controller_finalization")
    if service_result != "controller_finalization":
        data["service_result"] = service_result
        if service_result != "success" or data.get("outcome") != "passed":
            data["outcome"] = "failed"
            data.setdefault("error", {"code": "service_failed", "message": service_result})
        data["phase"] = "stopped" if data.get("outcome") == "passed" else "failed"
        atomic(path, data)
    atomic(path.parent / "finalizer.json", {"generation": data["generation"], "runtime_removed": not runtime.exists(),
                                           "service_result": service_result, "monotonic_ns": time.monotonic_ns()})


def run(args):
    started = time.monotonic()
    overall = started + BOUNDS["overall"]
    artifacts = private_dir(args.artifacts) / uuid.uuid4().hex
    artifacts.mkdir(mode=0o700)
    runtime = Path(tempfile.mkdtemp(prefix="kde-m1-", dir="/tmp"))
    generation = artifacts.name
    atomic(runtime / "owner.json", {"generation": generation})
    for sub in ("home/config", "home/data", "home/cache", "home/state", "tmp", "empty"):
        (runtime / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    unit = "kde-agent-m1-" + generation + ".service"
    manifest = artifacts / "manifest.json"
    data = {"schema_version": 1, "scope": "feasibility", "mode": "headless", "generation": generation,
            "runtime": str(runtime), "unit": unit, "cwd": str(Path.cwd()), "probe": args.probe,
            "inject": args.inject, "bounds_seconds": BOUNDS,
            "requested_output": {"count": 1, "width": 1280, "height": 720, "scale": 1},
            "phase": "building", "outcome": "pending", "started_monotonic_ns": time.monotonic_ns(),
            "dependency_policy_sha256": digest(PROJECT / "dependencies.json"),
            "issue9_report_sha256": digest(PROJECT / "evidence/issue-9/environment.json"),
            "python_version": sys.version.split()[0], "harness_source_sha256": digest(SCRIPT)}
    atomic(manifest, data)
    launched = False
    cgroup = None
    controller_error = None
    def bounded(limit):
        remaining = overall - time.monotonic()
        if remaining <= 0:
            raise Failure("overall_timeout", "Overall harness deadline expired")
        return min(limit, remaining)
    try:
        binary, receipt = build(args.build_root)
        data.update(fixture_binary=str(binary), build=receipt, phase="starting")
        data["native_packages"] = command(["/usr/bin/pacman", "-Q", "kwin", "systemd", "dbus", "wayland", "wayland-protocols", "libxkbcommon", "gcc"], env=manager_env()).splitlines()
        atomic(manifest, data)
        env = clean_env(runtime, generation)
        exec_argv = ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items()), "/usr/bin/python", "-I", str(SCRIPT), "_worker", str(manifest)]
        # systemd-run parses the stop-post command; quote paths using systemd's
        # double-quoted word syntax and escape percent specifiers explicitly.
        def unit_quote(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        stoppost = " ".join(unit_quote(v) for v in ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "SERVICE_RESULT=${SERVICE_RESULT}", "/usr/bin/python", "-I", str(SCRIPT), "_finalize", str(manifest)])
        argv = ["/usr/bin/systemd-run", "--user", "--no-ask-password", "--no-block", "--quiet", "--service-type=exec",
                "--unit=" + unit, "--property=Restart=no", "--property=UMask=0077", "--property=KillMode=control-group",
                "--property=SendSIGKILL=yes", "--property=TimeoutStopSec=3s", "--property=RuntimeMaxSec=95s",
                "--property=ExecStopPost=" + stoppost, "--property=StandardOutput=append:" + str(artifacts / "worker.stdout.log"),
                "--property=StandardError=append:" + str(artifacts / "worker.stderr.log"),
                "--working-directory=" + str(Path.cwd()), "--expand-environment=no", "--", *exec_argv]
        data["service_argv"] = argv
        atomic(manifest, data)
        # A lost response may still have created the unit; cleanup always queries
        # the exact unique unit after the launch attempt.
        launched = True
        command(argv, env=manager_env(), timeout=bounded(BOUNDS["manager_call"]))
        deadline = min(overall - BOUNDS["cleanup"] - BOUNDS["finalization"] - 1,
                       time.monotonic() + BOUNDS["service_lifetime"] + 5)
        while True:
            info = unit_info(unit, bounded(BOUNDS["manager_call"]))
            cgroup = info.get("ControlGroup") or cgroup
            if cgroup:
                atomic(artifacts / "ownership.json", {"generation": generation, "cgroup": cgroup,
                                                       "members": [identity(pid) for pid in cgroup_members(cgroup)]})
            if info.get("ActiveState") in ("inactive", "failed") or info.get("LoadState") == "not-found":
                break
            if time.monotonic() >= deadline:
                raise Failure("service_timeout", "Service lifetime plus observation allowance expired")
            time.sleep(0.05)
    except BaseException as exc:
        controller_error = {"code": getattr(exc, "code", "interrupted" if isinstance(exc, KeyboardInterrupt) else "controller_error"), "message": str(exc)}
    finally:
        cleanup_started = time.monotonic()
        cleanup = {"generation": generation, "started_monotonic_ns": time.monotonic_ns(), "verified_empty": False}
        try:
            if launched:
                stop_deadline = min(overall - BOUNDS["finalization"], cleanup_started + BOUNDS["cleanup"])
                def remaining():
                    value = min(BOUNDS["manager_call"], stop_deadline - time.monotonic())
                    if value <= 0:
                        raise Failure("cleanup_timeout", "Cleanup verification deadline expired")
                    return value
                info = unit_info(unit, remaining())
                cgroup = info.get("ControlGroup") or cgroup
                cleanup["before"] = info
                if info.get("LoadState") != "not-found":
                    command(["/usr/bin/systemctl", "--user", "--no-ask-password", "--no-block", "stop", unit], env=manager_env(), timeout=remaining())
                while True:
                    info = unit_info(unit, remaining())
                    cgroup = info.get("ControlGroup") or cgroup
                    members = cgroup_members(cgroup) if cgroup else []
                    if info.get("ActiveState") in ("inactive", "failed") or info.get("LoadState") == "not-found":
                        if members:
                            raise Failure("cleanup_survivors", "Owned processes remain after unit stop")
                        if not cgroup and (artifacts / "ownership.json").exists():
                            cgroup = json.loads((artifacts / "ownership.json").read_text())["cgroup"]
                            members = cgroup_members(cgroup)
                        cleanup.update(verified_empty=bool(cgroup) and not members, cgroup=cgroup, members=members, after=info)
                        if not cleanup["verified_empty"]:
                            raise Failure("cleanup_unverified", "No observed owned cgroup to verify")
                        break
                    time.sleep(min(0.05, remaining()))
                # Preserve state first; then release only this failed transient unit.
                if info.get("ActiveState") == "failed":
                    command(["/usr/bin/systemctl", "--user", "reset-failed", unit], env=manager_env(), timeout=remaining())
            else:
                cleanup.update(verified_empty=True, never_launched=True)
            if runtime.exists():
                finalize(manifest)
        except Exception as exc:
            cleanup["error"] = {"code": getattr(exc, "code", "cleanup_error"), "message": str(exc)}
        cleanup["elapsed_seconds"] = round(time.monotonic() - cleanup_started, 3)
        atomic(artifacts / "cleanup.json", cleanup)
        data = json.loads(manifest.read_text())
        shutdown_ns = data.get("cleanup_started_monotonic_ns", cleanup["started_monotonic_ns"])
        cleanup["total_shutdown_seconds"] = round((time.monotonic_ns() - shutdown_ns) / 1e9, 3)
        if cleanup["total_shutdown_seconds"] > BOUNDS["cleanup"]:
            cleanup["error"] = {"code": "cleanup_bound_exceeded", "message": "Complete shutdown exceeded provisional limit"}
        atomic(artifacts / "cleanup.json", cleanup)
        data["cleanup"] = cleanup
        if controller_error:
            data["controller_error"] = controller_error
        if data.get("outcome") != "passed" or controller_error or not cleanup.get("verified_empty") or cleanup.get("error"):
            data["outcome"] = "failed"
        data["elapsed_seconds"] = round(time.monotonic() - started, 3)
        data["within_overall_bound"] = time.monotonic() <= overall
        if not data["within_overall_bound"]:
            data["outcome"] = "failed"
        atomic(manifest, data)
    print(json.dumps({"ok": data["outcome"] == "passed", "generation": generation, "manifest": str(manifest),
                      "outcome": data["outcome"], "cleanup": cleanup, "elapsed_seconds": data["elapsed_seconds"],
                      "error": data.get("error", data.get("controller_error"))}))
    return 0 if data["outcome"] == "passed" else 1


def smoke():
    env = os.environ
    results = []
    # Deliberately separate command invocations inside the service, for later
    # window/input/capture scripts to reuse this same topology.
    for state in (17, 42):
        raw = command(["/usr/bin/python", "-I", str(SCRIPT), "control", "--socket", env["HARNESS_CONTROL"],
                       "--generation", env["HARNESS_GENERATION"], "--state", str(state), "set_state"], env=dict(env), timeout=4)
        result = json.loads(raw)
        if not result["ok"] or result["presented"]["state"] != state or result["presented"]["source"] != "control":
            raise Failure("smoke_failed", "Unexpected fixture state acknowledgment")
        results.append(result)
    if results[0]["presented"]["checksum"] == results[1]["presented"]["checksum"]:
        raise Failure("smoke_failed", "Fixture state checksums did not change")
    atomic(Path(env["HARNESS_ARTIFACTS"]) / "smoke.json", {"generation": env["HARNESS_GENERATION"], "results": results})
    return 0


def descendant(path):
    pid = os.fork()
    if pid:
        os._exit(0)
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    atomic(path, identity(os.getpid()))
    while True:
        signal.pause()


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1].startswith("_"):
        operation = sys.argv[1]
        if operation == "_worker":
            return worker(sys.argv[2])
        if operation == "_finalize":
            finalize(sys.argv[2]); return 0
        if operation == "_smoke":
            return smoke()
        if operation == "_descendant":
            descendant(sys.argv[2]); return 0
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    launch = sub.add_parser("run")
    launch.add_argument("--artifacts", default=str(PROJECT / ".local/harness-runs"))
    launch.add_argument("--build-root", default=str(PROJECT / ".local/fixture-build"))
    launch.add_argument("--inject", choices=("none", "after-fixture", "startup-timeout", "worker-kill"), default="none")
    launch.add_argument("probe", nargs=argparse.REMAINDER)
    client = sub.add_parser("control")
    client.add_argument("--socket", required=True)
    client.add_argument("--generation", required=True)
    client.add_argument("--state", type=int)
    client.add_argument("action", choices=("status", "set_state", "close"))
    args = parser.parse_args()
    try:
        if args.operation == "run":
            if args.probe and args.probe[0] == "--":
                args.probe = args.probe[1:]
            return run(args)
        print(json.dumps(control(args.socket, args.generation, args.action, args.state)))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "error"), "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
