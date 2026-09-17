#!/usr/bin/python3
"""Issue #11 feasibility probe, only inside private_harness.py's service."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("private_harness", PROJECT / "tools/private_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
Failure = harness.Failure
QUERY_SECONDS = .5
CLEANUP_SECONDS = 1.5
FOCUS_SECONDS = 2
POLL_SECONDS = .1
MAX_BYTES = 1024 * 1024
PIN = "be03ce90c09350898556436bac74ed35fe928617"


def encoded(data):
    return "const request = " + json.dumps(data, ensure_ascii=True, allow_nan=False) + ";\n"


def window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F-]{36}|\{[0-9a-fA-F-]{36}\})", value):
        raise Failure("invalid_result", "Window UUID must be a string")
    try:
        if str(uuid.UUID(value.strip("{}"))) != value.strip("{}").lower():
            raise ValueError()
    except ValueError:
        raise Failure("invalid_result", "Invalid window UUID")
    return value


def snapshot(value, request_id):
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("request_id") != request_id:
        raise Failure("invalid_result", "Snapshot identity/schema mismatch")
    active = value.get("active_uuid")
    if active is not None:
        window_id(active)
    if "active_uuid" not in value or not isinstance(value.get("windows"), list):
        raise Failure("invalid_result", "Missing active identity/windows")
    seen = set()
    for row in value["windows"]:
        if not isinstance(row, dict) or set(row) != {"uuid", "pid", "title", "class", "client", "frame", "active"}:
            raise Failure("invalid_result", "Incomplete window metadata")
        ident = window_id(row["uuid"])
        if ident in seen:
            raise Failure("invalid_result", "Duplicate UUID")
        seen.add(ident)
        if row["pid"] is not None and (type(row["pid"]) is not int or row["pid"] <= 0):
            raise Failure("invalid_result", "Invalid reported PID")
        for key in ("title", "class"):
            if row[key] is not None and not isinstance(row[key], str):
                raise Failure("invalid_result", "Invalid text metadata")
        for key in ("client", "frame"):
            bounds = row[key]
            if bounds is None:
                continue
            if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}:
                raise Failure("invalid_result", "Invalid geometry fields")
            for name, number in bounds.items():
                if type(number) not in (int, float) or not math.isfinite(number) or (name in ("width", "height") and number <= 0):
                    raise Failure("invalid_result", "Invalid geometry value")
        if type(row["active"]) is not bool or row["active"] != (ident == active):
            raise Failure("invalid_result", "Inconsistent focus metadata")
    if active is not None and active not in seen:
        raise Failure("invalid_result", "Active UUID absent from snapshot")
    return value


def private_context(env):
    generation = env.get("HARNESS_GENERATION", "")
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise Failure("private_context", "Missing private harness generation")
    runtime = Path(env.get("XDG_RUNTIME_DIR", "/nonexistent"))
    if json.loads((runtime / "owner.json").read_text()) != {"generation": generation}:
        raise Failure("private_context", "Runtime ownership mismatch")
    expected = harness.clean_env(runtime, generation)
    if any(env.get(k) != v for k, v in expected.items()):
        raise Failure("private_context", "Private endpoint/environment mismatch")
    if any(k in env for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_SOCKET", "AT_SPI_BUS_ADDRESS", "LD_PRELOAD", "PYTHONPATH")):
        raise Failure("private_context", "Host endpoint/loader override present")
    for path in (runtime, runtime / "bus", runtime / expected["WAYLAND_DISPLAY"], runtime / "control"):
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or stat.S_ISLNK(info.st_mode):
            raise Failure("private_context", "Unsafe private endpoint ownership/mode")
    artifacts = Path(env["HARNESS_ARTIFACTS"])
    manifest = json.loads((artifacts / "manifest.json").read_text())
    if manifest["generation"] != generation or manifest["runtime"] != str(runtime) or manifest["phase"] != "foundation_ready":
        raise Failure("private_context", "Manifest identity/readiness mismatch")
    if Path("/proc/self/cgroup").read_text().strip() != manifest["worker"]["cgroup"]:
        raise Failure("private_context", "Probe outside private service cgroup")
    if env["HARNESS_CONTROL"] != str(runtime / "control"):
        raise Failure("private_context", "Control endpoint mismatch")
    deadline = float(env["HARNESS_DEADLINE"])
    if not math.isfinite(deadline) or not 0 < deadline - time.monotonic() <= 60:
        raise Failure("private_context", "Invalid harness deadline")
    return generation, runtime, artifacts, manifest, deadline


class Probe:
    def __init__(self, binary, scenario):
        self.env = dict(os.environ)
        self.generation, self.runtime, self.artifacts, self.manifest, deadline = private_context(self.env)
        self.deadline = deadline - 3  # Leave worker/probe failure cleanup room.
        self.binary = Path(binary).resolve(strict=True)
        report = json.loads((PROJECT / "evidence/issue-9/environment.json").read_text())
        # Exact selected artifact is the reviewed #9 baseline, not any same-version executable.
        def hashes(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "binary_sha256":
                        yield item
                    yield from hashes(item)
            elif isinstance(value, list):
                for item in value:
                    yield from hashes(item)
        binary_hash = harness.digest(self.binary)
        if binary_hash not in set(hashes(report)):
            raise Failure("dependency_mismatch", "Candidate binary differs from #9 evidence")
        import dbus
        self.bus = dbus.bus.BusConnection(self.env["DBUS_SESSION_BUS_ADDRESS"])
        self.scripting = dbus.Interface(self.bus.get_object("org.kde.KWin", "/Scripting", introspect=False), "org.kde.kwin.Scripting")
        self.source = (PROJECT / "tools/kwin_window_query.js").read_text()
        self.operations = []
        self.result = {"generation": self.generation, "scenario": scenario, "scope": "feasibility",
                       "kdotool_revision": PIN, "binary_sha256": binary_hash,
                       "probe_sha256": harness.digest(__file__), "query_sha256": harness.digest(PROJECT / "tools/kwin_window_query.js"),
                       "kwin_header_sha256": harness.digest("/usr/include/kwin/window.h"),
                       "bounds_seconds": {"query": QUERY_SECONDS, "cleanup": CLEANUP_SECONDS, "focus": FOCUS_SECONDS, "poll_interval": POLL_SECONDS},
                       "operations": self.operations}

    def remaining(self, deadline):
        value = min(deadline, self.deadline) - time.monotonic()
        if value <= 0:
            raise Failure("timeout", "Shared operation deadline expired")
        return value

    def loaded(self, name, deadline):
        return bool(self.scripting.isScriptLoaded(name, timeout=self.remaining(deadline)))

    def execute(self, source=None, *, data=None, native=None, seconds=QUERY_SECONDS, deadline=None, stop_loaded=False, parse=None):
        started = time.monotonic()
        work_deadline = min(started + seconds, deadline or self.deadline, self.deadline - CLEANUP_SECONDS)
        index = len(self.operations)
        name = f"kde-agent-{self.generation}-{index}"
        folder = self.artifacts / f"query-{index:04d}"
        folder.mkdir(mode=0o700)
        temporary = self.runtime / "tmp" / name
        temporary.mkdir(mode=0o700)
        request = {"request_id": name} | (data or {})
        receipt = {"name": name, "started": started, "work_limit": work_deadline - started,
                   "native": native, "stopped_after_registration": False}
        self.operations.append(receipt)
        process = None
        error = None
        output = None
        try:
            receipt["loaded_before"] = self.loaded(name, work_deadline)
            if receipt["loaded_before"]:
                raise Failure("script_collision", "Unique script name already exists")
            script = folder / "input.js"
            if native is None:
                script.write_text(encoded(request) + (self.source if source is None else source))
                argv = [str(self.binary), "--name", name, "kwinscript", "--file", str(script)]
            else:
                argv = [str(self.binary), "--name", name, *native]
            with (folder / "stdout").open("wb") as out, (folder / "stderr").open("wb") as err:
                self.remaining(work_deadline)
                process = subprocess.Popen(argv, env=self.env | {"TMPDIR": str(temporary)}, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err)
                receipt["process"] = harness.identity(process.pid)
                while process.poll() is None:
                    self.remaining(work_deadline)
                    if stop_loaded and not receipt["stopped_after_registration"] and self.loaded(name, work_deadline):
                        process.send_signal(signal.SIGSTOP)
                        receipt["stopped_after_registration"] = True
                    time.sleep(min(.002, self.remaining(work_deadline)))
            receipt["returncode"] = process.returncode
            if process.returncode:
                raise Failure("query_failed", f"kdotool exited {process.returncode}")
            if (folder / "stdout").stat().st_size > MAX_BYTES:
                raise Failure("invalid_result", "Oversized kdotool output")
            text = (folder / "stdout").read_text()
            output = parse(text, name) if parse else text
            self.remaining(work_deadline)
            receipt["loaded_after_native"] = self.loaded(name, work_deadline)
            if receipt["loaded_after_native"]:
                raise Failure("script_leak", "Successful kdotool left its script loaded")
            self.remaining(work_deadline)
        except Exception as exc:
            error = exc
        finally:
            receipt["request_finished"] = time.monotonic()
            cleanup_deadline = min(self.deadline, receipt["request_finished"] + CLEANUP_SECONDS)
            try:
                if process and process.poll() is None:
                    # SIGKILL works even for the intentionally SIGSTOP'd owned child.
                    process.kill()
                    process.wait(timeout=self.remaining(cleanup_deadline))
                receipt["process_reaped"] = process is None or process.poll() is not None
                receipt["process_reaped_at"] = time.monotonic()
                receipt["returncode"] = None if process is None else process.returncode
                receipt["loaded_before_cleanup"] = self.loaded(name, cleanup_deadline)
                if receipt["loaded_before_cleanup"]:
                    with (folder / "remove.stdout").open("wb") as out, (folder / "remove.stderr").open("wb") as err:
                        remover = subprocess.Popen([str(self.binary), "--remove", name], env=self.env,
                                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err)
                        receipt["remove_process"] = harness.identity(remover.pid)
                        try:
                            remover.wait(timeout=self.remaining(cleanup_deadline))
                        finally:
                            if remover.poll() is None:
                                remover.kill()
                                remover.wait(timeout=self.remaining(cleanup_deadline))
                        receipt["remove_returncode"] = remover.returncode
                receipt["loaded_after_cleanup"] = self.loaded(name, cleanup_deadline)
                if receipt["loaded_after_cleanup"]:
                    raise Failure("cleanup_failed", "Owned script remains loaded")
                receipt["abandoned_temp_files"] = sorted(p.name for p in temporary.iterdir())
                shutil.rmtree(temporary)
                receipt["temporary_removed"] = not temporary.exists()
                self.remaining(cleanup_deadline)
                receipt["cleanup_ok"] = True
            except Exception as exc:
                error = Failure("cleanup_failed", str(exc))
                receipt["cleanup_ok"] = False
            receipt["finished"] = time.monotonic()
            receipt["seconds"] = receipt["finished"] - started
            receipt["cleanup_seconds"] = receipt["finished"] - receipt["request_finished"]
            if error is None and receipt["finished"] > work_deadline:
                error = Failure("timeout", "Full query observation/cleanup exceeded work deadline")
            receipt["error"] = None if error is None else {"code": getattr(error, "code", "invalid_result"), "message": str(error)}
            harness.atomic(folder / "receipt.json", receipt)
            harness.atomic(self.artifacts / "kdotool-probe.json", self.result)
        if error:
            raise error
        return output

    def query(self, deadline=None):
        def parse(text, name):
            return snapshot(json.loads(text), name)
        return self.execute(deadline=deadline, parse=parse)

    def focus(self, ident, noop=False):
        window_id(ident)
        started = time.monotonic()
        deadline = min(self.deadline, started + FOCUS_SECONDS)
        def exists(value):
            if ident not in {w["uuid"] for w in value["windows"]}:
                raise Failure("target_missing", "Requested window vanished")
        exists(self.query(deadline))
        if noop:
            self.execute("// Fixed successful no-op activation fault.", deadline=deadline)
        else:
            self.execute(native=["windowactivate", ident], deadline=deadline)
        while True:
            tick = time.monotonic()
            value = self.query(deadline)
            exists(value)
            self.remaining(deadline)
            if value["active_uuid"] == ident:
                return {"uuid": ident, "seconds": time.monotonic() - started, "observed": value}
            time.sleep(min(max(0, POLL_SECONDS - (time.monotonic() - tick)), self.remaining(deadline)))

    def expect(self, code, callback):
        before = len(self.operations)
        try:
            callback()
        except Failure as exc:
            if exc.code != code:
                raise
            result = {"expected": code, "observed": exc.code, "operations_from": before}
            self.result.setdefault("negative", []).append(result)
            return result
        raise AssertionError(f"Expected {code}")

    def bridge(self):
        """Blocking query worker for #12; the caller observes this child asynchronously."""
        print(json.dumps({"ready": True}), flush=True)
        while True:
            line = sys.stdin.buffer.readline(4097)
            if not line:
                return
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise Failure("invalid_request", "Oversized query bridge request")
            request = json.loads(line)
            try:
                if request["op"] == "query":
                    value = self.query()
                elif request["op"] == "slow-query":
                    value = self.execute("const end = Date.now() + 750; while (Date.now() < end) {}\n" + self.source,
                                         parse=lambda text, name: snapshot(json.loads(text), name))
                elif request["op"] == "focus":
                    value = self.focus(request["uuid"])
                else:
                    raise Failure("invalid_request", "Unknown query bridge operation")
                result = {"ok": True, "value": value}
            except Exception as exc:
                result = {"ok": False, "code": getattr(exc, "code", "query_failed"), "message": str(exc)}
            print(json.dumps(result), flush=True)

    def functional(self):
        initial = self.query()
        primary_pid = self.manifest["processes"]["fixture"]["pid"]
        primary = [w for w in initial["windows"] if w["pid"] == primary_pid]
        assert len(primary) == 1, primary
        first = primary[0]
        assert first["title"] == "KDE Agent Native Fixture" and first["class"] == "org.kde_agent.fixture", first
        assert first["client"]["width"] == 640 and first["client"]["height"] == 360, first
        assert self.manifest["initial_presented"]["event"] == "presented"
        self.result["initial_snapshot"] = initial
        payload = 'quotes " \\ newline\n ` ${throw new Error()} 雪 \u2028'
        value = self.execute(data={"check_data": True, "echo": payload}, parse=lambda text, _: json.loads(text))
        assert value["echo"] == payload
        assert value["unavailable"] == {"uuid": None, "pid": None, "title": None, "class": None, "client": None, "frame": None, "active": False}
        self.result["synthetic_encoding_null"] = value
        with (self.artifacts / "secondary.events.jsonl").open("wb") as out, (self.artifacts / "secondary.stderr").open("wb") as err:
            second = subprocess.Popen([self.manifest["fixture_binary"]], env=self.env, stdin=subprocess.PIPE, stdout=out, stderr=err)
            self.result["secondary_process"] = harness.identity(second.pid)
            try:
                assert Path(f"/proc/{second.pid}/cgroup").read_text().strip() == self.manifest["worker"]["cgroup"]
                deadline = time.monotonic() + 3
                while True:
                    candidates = self.query()["windows"]
                    matches = [w for w in candidates if w["pid"] == second.pid]
                    if matches:
                        break
                    self.remaining(deadline)
                    time.sleep(.01)
                assert len(matches) == 1
                other = matches[0]
                assert other["title"] == first["title"] and other["class"] == first["class"]
                self.result["ambiguous_title_candidates"] = candidates
                self.result["focus_transitions"] = [self.focus(w["uuid"]) for w in [first, other] * 5]
                self.expect("timeout", lambda: self.focus(first["uuid"], noop=True))
                second.stdin.write(b"close\n"); second.stdin.flush()
                second.wait(timeout=2)
                deadline = time.monotonic() + 2
                while any(w["uuid"] == other["uuid"] for w in self.query()["windows"]):
                    self.remaining(deadline)
                    time.sleep(.01)
                self.result["vanished_uuid"] = other["uuid"]
                self.expect("target_missing", lambda: self.focus(other["uuid"]))
                self.result["final_focus"] = self.focus(first["uuid"])
            finally:
                if second.poll() is None:
                    second.kill(); second.wait(timeout=1)
                second.stdin.close()
                self.result["secondary_reaped"] = second.returncode

    def latency(self):
        begin = len(self.operations)
        samples = []
        for _ in range(100):
            started = time.monotonic()
            self.query()
            samples.append(time.monotonic() - started)
        self.result["sequential_indices"] = [begin, len(self.operations)]
        ordered = sorted(samples)
        self.result["full_query_seconds"] = samples
        self.result["full_query_statistics"] = {
            "count": len(samples), "min": min(samples), "median": statistics.median(samples),
            "p95": ordered[math.ceil(.95 * len(samples)) - 1],
            "p99": ordered[math.ceil(.99 * len(samples)) - 1], "max": max(samples)}
        starts = []
        polled = []
        for _ in range(30):
            started = time.monotonic()
            starts.append(started)
            self.query()
            polled.append(time.monotonic() - started)
            time.sleep(max(0, POLL_SECONDS - (time.monotonic() - started)))
        self.result["poll_start_intervals"] = [b - a for a, b in zip(starts, starts[1:])]
        self.result["polled_query_seconds"] = polled
        self.result["poll_query_overruns"] = sum(sample > POLL_SECONDS for sample in polled)
        ordering = 'for (var i=0; i<64; ++i) output_result(JSON.stringify({request_id:request.request_id,index:i,payload:request.echo}));'
        payload = '"\\\n` ${42} 雪'
        for _ in range(20):
            def parse(text, name):
                rows = [json.loads(line) for line in text.splitlines()]
                assert rows == [{"request_id": name, "index": i, "payload": payload} for i in range(64)]
                return rows
            self.execute(ordering, data={"echo": payload}, parse=parse)
        self.result["ordering"] = {"runs": 20, "records_per_run": 64, "exact_order_and_payload": True}

    def faults(self):
        self.expect("query_failed", lambda: self.execute('throw new Error("fixed error");'))
        # A parse failure cannot execute the pin's finished callback. The outer
        # work deadline, rather than a JS error callback, detects this failure.
        self.expect("timeout", lambda: self.execute('syntax {{{'))
        for source in ('', 'output_result("not-json");'):
            def parse(text, _):
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise Failure("invalid_result", str(exc))
            self.expect("invalid_result", lambda: self.execute(source, parse=parse))
        self.expect("timeout", lambda: self.execute('var end=Date.now()+750; while(Date.now()<end) {}', seconds=.1))
        assert self.operations[-1]["loaded_before_cleanup"]
        assert self.operations[-1]["cleanup_seconds"] <= CLEANUP_SECONDS
        self.query()
        # Suppress the pin's own finished callback, without stalling KWin's loop.
        no_completion = 'callDBus = function() {};'
        self.expect("query_failed", lambda: self.execute(no_completion, seconds=12))
        native_timeout = self.operations[-1]
        assert 5 <= native_timeout["seconds"] < 12
        assert not native_timeout["loaded_before_cleanup"]
        assert "Timed out waiting for KWin script completion" in (self.artifacts / f"query-{len(self.operations)-1:04d}" / "stderr").read_text()
        self.query()
        before = len(self.operations)
        self.expect("timeout", lambda: self.execute(no_completion, seconds=.2, stop_loaded=True))
        assert self.operations[before]["stopped_after_registration"]
        self.query()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("scenario", choices=("functional", "latency", "faults", "bridge"))
    args = parser.parse_args()
    probe = Probe(args.binary, args.scenario)
    try:
        getattr(probe, args.scenario)()
        probe.result["outcome"] = "passed"
    except Exception as exc:
        probe.result.update(outcome="failed", error={"code": getattr(exc, "code", "assertion"), "message": str(exc)})
        raise
    finally:
        harness.atomic(probe.artifacts / "kdotool-probe.json", probe.result)
        probe.bus.close()


if __name__ == "__main__":
    main()
