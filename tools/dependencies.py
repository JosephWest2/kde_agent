#!/usr/bin/python3
"""Bounded M1 prerequisite setup; never connects to a desktop."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

PROJECT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT / "dependencies.json"
CLEANUP_SECONDS = 2
MAX_OUTPUT = 8 * 1024 * 1024


class Failure(Exception):
    def __init__(self, code, message, repair, **context):
        self.error = dict(code=code, message=message, repair=repair, **context)
        super().__init__(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def clean_env(home):
    home = Path(home)
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
            "TMPDIR": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "CARGO_TERM_COLOR": "never", "CARGO_NET_RETRY": "0"}


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        raise Failure("cleanup_timeout", "Owned process did not reap within cleanup bound",
                      "Inspect the owned process before retrying", pid=process.pid)


class Runner:
    def __init__(self, home, seconds):
        self.env = clean_env(home)
        self.deadline = time.monotonic() + seconds

    def remaining(self, limit):
        value = min(limit, self.deadline - time.monotonic())
        if value <= 0:
            raise Failure("timeout", "Operation deadline exhausted", "Retry after checking system load")
        return value

    def run(self, argv, *, seconds=5, cwd=None, env=None):
        timeout = self.remaining(seconds)
        # Files avoid a descendant holding a pipe open after the direct child exits.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=self.env | (env or {}),
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           start_new_session=True)
            except OSError as exc:
                raise Failure("missing_tool", f"Cannot execute {Path(argv[0]).name}: {exc.strerror}",
                              "Install the documented Arch prerequisites", tool=Path(argv[0]).name) from exc
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(process)
                raise Failure("timeout", f"{Path(argv[0]).name} exceeded its deadline",
                              "Check prerequisites/system load and retry", timeout_seconds=timeout)
            finally:
                # These tools must not leave background processes, even after success.
                kill_group(process)
            out.seek(0)
            err.seek(0)
            stdout = out.read(MAX_OUTPUT + 1)
            stderr = err.read(MAX_OUTPUT + 1)
            if len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT:
                raise Failure("output_limit", "Probe output exceeded 8 MiB", "Inspect the dependency outside the report")
            if process.returncode:
                raise Failure("command_failed", f"{Path(argv[0]).name} exited {process.returncode}",
                              "Check the documented dependency versions and rerun setup",
                              exit_status=process.returncode, diagnostic=stderr.decode(errors="replace")[-2000:])
            return stdout.decode(errors="replace").strip()


def policy():
    return json.loads(POLICY_PATH.read_text())


def private_directory(path):
    path = Path(path)
    if path.exists() and (path.is_symlink() or path.stat().st_uid != os.getuid()):
        raise Failure("unsafe_path", "Work root must be owned and not a symlink", "Choose a new project-local --root")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


PYTHON_PROBE = r'''
import importlib.metadata as metadata, json, sys, ctypes
import gi
from gi.repository import GLib
import dbus
import PIL
print(json.dumps({
    "version": sys.version.split()[0],
    "system_site_packages": sys.prefix != sys.base_prefix,
    "libei": {"requested_soname": "libei.so.1", "loaded": bool(ctypes.CDLL("libei.so.1")), "loaded_files": sorted(set(line.split()[-1] for line in open("/proc/self/maps") if "/libei.so." in line))},
    "bindings": {"PyGObject": gi.__version__, "GLib": ".".join(map(str, (GLib.MAJOR_VERSION, GLib.MINOR_VERSION, GLib.MICRO_VERSION))), "dbus-python": dbus.__version__, "Pillow": PIL.__version__},
    "binding_origins": {"gi": "distribution" if gi.__file__.startswith("/usr/lib/") else "other", "dbus": "distribution" if dbus.__file__.startswith("/usr/lib/") else "other", "PIL": "distribution" if PIL.__file__.startswith("/usr/lib/") else "other"},
    "resolved_distributions": sorted([{"name": d.metadata["Name"], "version": d.version} for d in metadata.distributions()], key=lambda d: (d["name"].lower(), d["version"]))
}))
'''


def native_report(runner, data, python):
    errors = []
    report = {"scope": "prerequisites", "desktop_compatibility": "untested",
              "maintained_patches": data["kdotool"]["patches"],
              "excluded_dependencies": data["excluded_dependencies"]}
    os_data = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k in ("ID", "VERSION_ID", "BUILD_ID"):
                os_data[k] = v.strip('"')
    report["target"] = {"os": os_data, "kernel": os.uname().release, "architecture": os.uname().machine}
    if os_data.get("ID") != "arch":
        errors.append(Failure("unsupported_target", "Only Arch Linux is recorded for M1", "Run on the target Arch/KDE machine").error)
    try:
        inventory = dict(line.split(" ", 1) for line in runner.run(["/usr/bin/pacman", "-Q"]).splitlines())
        report["native_installed_inventory"] = inventory
        report["required_arch_packages"] = {name: inventory.get(name) for name in data["arch_packages"]}
        for name, version in report["required_arch_packages"].items():
            if version is None:
                errors.append(Failure("missing_package", f"Missing Arch package {name}", f"Install {name} through your normal Arch package workflow", package=name).error)
        if not inventory.get("kwin", "").startswith("6."):
            errors.append(Failure("unsupported_version", "KWin 6 is required", "Use the recorded KDE 6 target").error)
    except Failure as exc:
        errors.append(exc.error)
    report["native_releases"] = {}
    for name in ("libei-1.0", "dbus-1", "glib-2.0", "gobject-2.0", "libffi", "wayland-client", "xkbcommon"):
        try:
            report["native_releases"][name] = runner.run(["/usr/bin/pkg-config", "--modversion", name])
        except Failure as exc:
            errors.append(exc.error | {"dependency": name})
    try:
        report["python"] = json.loads(runner.run([str(python), "-I", "-c", PYTHON_PROBE]))
        if any(origin != "distribution" for origin in report["python"]["binding_origins"].values()):
            raise Failure("binding_origin", "Native Python bindings are shadowed", "Recreate the venv with distro system-site-packages and no pip overrides")
    except Failure as exc:
        errors.append(exc.error | {"dependency": "Python native bindings", "repair": "Run setup; install python-gobject, python-dbus and python-pillow if missing"})
    try:
        _, report["current_build_toolchain"] = rust_tools(runner)
    except Failure as exc:
        errors.append(exc.error)
    return report, errors


def rust_tools(runner):
    rust_home = str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".rustup")
    env = {"RUSTUP_HOME": rust_home, "RUSTUP_TOOLCHAIN": "stable", "RUSTUP_AUTO_INSTALL": "0"}
    tools = {name: runner.run(["/usr/bin/rustup", "which", name], env=env) for name in ("rustc", "cargo")}
    versions = {name: runner.run([path, "--version"]) for name, path in tools.items()}
    return tools, versions


def source_state(runner, root, data):
    source = root / "kdotool-source"
    revision = runner.run(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"])
    if revision != data["kdotool"]["revision"]:
        raise Failure("provenance_mismatch", "kdotool checkout revision differs from policy", "Use a fresh --root and rerun setup")
    if runner.run(["/usr/bin/git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        raise Failure("provenance_mismatch", "kdotool source has local changes", "Use a fresh --root; maintained patches must be explicitly reviewed")
    lock_digest = digest(source / "Cargo.lock")
    if lock_digest != data["kdotool"]["cargo_lock_sha256"]:
        raise Failure("provenance_mismatch", "kdotool Cargo.lock differs from policy", "Use a fresh --root and rerun setup")
    return {"revision": revision, "cargo_lock_sha256": lock_digest, "clean_checkout": True}


def validate_receipt(receipt):
    """Reject incompatible JSON shapes before using any receipt fields."""
    def invalid():
        raise Failure("provenance_mismatch", "Incomplete or malformed build receipt",
                      "Use a fresh --root and rerun setup")

    def text(value):
        return isinstance(value, str) and bool(value)

    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    if not isinstance(receipt, dict):
        invalid()
    for key in ("revision", "cargo_lock_sha256", "binary_sha256", "build_command", "release"):
        if not text(receipt.get(key)):
            invalid()
    if receipt.get("clean_checkout") is not True or not isinstance(receipt.get("patches"), list):
        invalid()
    toolchain = receipt.get("toolchain")
    if not isinstance(toolchain, dict) or not all(text(toolchain.get(name)) for name in ("rustc", "cargo")):
        invalid()
    resolved = receipt.get("resolved_cargo")
    if not isinstance(resolved, dict):
        invalid()
    for key in ("lock_packages", "resolved_nodes"):
        items = resolved.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
            invalid()
    for package in resolved["lock_packages"]:
        if not all(text(package.get(key)) for key in ("name", "version")):
            invalid()
        if any(not text(package[key]) for key in ("source", "checksum") if key in package):
            invalid()
        if "dependencies" in package and not strings(package["dependencies"]):
            invalid()
    for node in resolved["resolved_nodes"]:
        if not text(node.get("id")) or not strings(node.get("dependencies")) or not strings(node.get("features")):
            invalid()
        node_deps = node.get("deps")
        if not isinstance(node_deps, list):
            invalid()
        for dependency in node_deps:
            if not isinstance(dependency, dict) or not all(text(dependency.get(key)) for key in ("name", "pkg")):
                invalid()
            kinds = dependency.get("dep_kinds")
            if not isinstance(kinds, list) or any(
                    not isinstance(kind, dict) or any(
                        key not in kind or (kind[key] is not None and not isinstance(kind[key], str))
                        for key in ("kind", "target")) for kind in kinds):
                invalid()


def verify_build(runner, root, data):
    receipt_path = root / "build.json"
    if not receipt_path.is_file():
        raise Failure("missing_build", "No completed kdotool build receipt", "Run tools/dependencies.py setup")
    receipt = json.loads(receipt_path.read_text())
    validate_receipt(receipt)
    state = source_state(runner, root, data)
    if any(receipt.get(key) != value for key, value in state.items()):
        raise Failure("provenance_mismatch", "Stale build receipt", "Use a fresh --root and rerun setup")
    if receipt["patches"] != data["kdotool"]["patches"] or receipt["release"] != data["kdotool"]["release"]:
        raise Failure("provenance_mismatch", "Build policy differs from receipt", "Use a fresh --root and rerun setup")
    if digest(root / "bin/kdotool") != receipt["binary_sha256"]:
        raise Failure("provenance_mismatch", "Built executable digest differs from receipt", "Use a fresh --root and rerun setup")
    release = runner.run([str(root / "bin/kdotool"), "--version"], env={"KDE_SESSION_VERSION": "6"})
    if data["kdotool"]["release"] not in release:
        raise Failure("provenance_mismatch", "Executable release differs from policy", "Use a fresh --root and rerun setup")
    return receipt | {"provenance_verified": True, "version_output": release}


def check_kdotool(runner, root):
    with tempfile.TemporaryDirectory(prefix="kde-check-") as tmp:
        directory = Path(tmp)
        address = "unix:path=" + str(directory / "bus")
        env = clean_env(directory) | {"DBUS_SESSION_BUS_ADDRESS": address, "KDE_SESSION_VERSION": "6"}
        with tempfile.TemporaryFile() as daemon_log:
            daemon = subprocess.Popen(["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile", "--address=" + address],
                                      env=env, stdin=subprocess.DEVNULL, stdout=daemon_log, stderr=daemon_log, start_new_session=True)
            start = time.monotonic()
            deadline = min(runner.deadline, start + 10)
            try:
                while not (directory / "bus").exists():
                    if daemon.poll() is not None:
                        raise Failure("private_bus_failed", "Private D-Bus daemon exited", "Check the installed dbus package")
                    if time.monotonic() >= deadline:
                        raise Failure("timeout", "Private bus readiness timed out", "Check the installed dbus package")
                    time.sleep(0.01)
                body = 'output_result(JSON.stringify({probe:"issue-9"}));'
                generated = runner.run([str(root / "bin/kdotool"), "--dry-run", "kwinscript", "--inline", body],
                                       seconds=max(0, deadline - time.monotonic()), env=env)
                if body not in generated or "finished" not in generated:
                    raise Failure("unsupported_capability", "Candidate custom-script generation failed", "Review the pinned candidate before changing dependencies")
            finally:
                kill_group(daemon)
            return {"status": "passed", "evaluation": "custom-script generation on private bus; no script executed",
                    "daemon_reaped": daemon.poll() is not None, "elapsed_seconds": round(time.monotonic() - start, 3)}


def build(runner, root, data):
    import shutil
    source = root / "kdotool-source"
    if not source.exists():
        runner.run(["/usr/bin/git", "init", str(source)])
        runner.run(["/usr/bin/git", "-C", str(source), "fetch", "--depth=1", data["kdotool"]["repository"], data["kdotool"]["revision"]], seconds=120)
        runner.run(["/usr/bin/git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
    state = source_state(runner, root, data)
    if (root / "build.json").exists():
        return verify_build(runner, root, data)
    tool_paths, versions = rust_tools(runner)
    # Cargo reads cwd ancestor .cargo/config even with --manifest-path. Keep cwd
    # outside the project, and reject config in its ancestors instead of trusting it.
    with tempfile.TemporaryDirectory(prefix="kde-cargo-") as tmp:
        cwd = Path(tmp)
        for ancestor in (cwd, *cwd.parents):
            if any((ancestor / ".cargo" / name).exists() for name in ("config", "config.toml")):
                raise Failure("unexpected_config", "Cargo configuration exists above controlled build directory", "Remove that configuration from the controlled build ancestry")
        cargo_home = private_directory(root / "cargo-home")
        if any((cargo_home / name).exists() for name in ("config", "config.toml", "credentials", "credentials.toml")):
            raise Failure("unexpected_config", "Unexpected config/credentials in project Cargo home", "Use a fresh --root")
        env = {"CARGO_HOME": str(cargo_home), "RUSTC": tool_paths["rustc"], "CARGO_TARGET_DIR": str(root / "cargo-target")}
        argv = [tool_paths["cargo"], "build", "--release", "--locked", "--manifest-path", str(source / "Cargo.toml")]
        runner.run(argv, seconds=600, cwd=cwd, env=env)
        metadata = json.loads(runner.run([tool_paths["cargo"], "metadata", "--locked", "--format-version=1", "--manifest-path", str(source / "Cargo.toml")], seconds=60, cwd=cwd, env=env))
    lock = tomllib.loads((source / "Cargo.lock").read_text())
    packages = [{key: value for key, value in package.items() if key in ("name", "version", "source", "checksum", "dependencies")} for package in lock["package"]]
    # Only portable dependency IDs, no machine paths or package metadata URLs.
    nodes = json.loads(json.dumps(metadata["resolve"]["nodes"]).replace("path+file://" + str(source), "path+file://<pinned-source>"))
    resolved = {"lock_packages": packages, "resolved_nodes": nodes}
    (root / "bin").mkdir(exist_ok=True)
    shutil.copy2(root / "cargo-target/release/kdotool", root / "bin/kdotool")
    state = source_state(runner, root, data)
    receipt = state | {"release": data["kdotool"]["release"], "patches": data["kdotool"]["patches"],
                       "binary_sha256": digest(root / "bin/kdotool"), "toolchain": versions,
                       "build_command": "cargo build --release --locked --manifest-path <pinned-source>/Cargo.toml",
                       "resolved_cargo": resolved}
    (root / "build.json.tmp").write_text(json.dumps(receipt, indent=2) + "\n")
    (root / "build.json.tmp").replace(root / "build.json")
    return verify_build(runner, root, data)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"schema_version": 1, "ok": False, "errors": [{"code": "invalid_arguments", "message": message, "repair": "Use --help"}]}))
        raise SystemExit(2)


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("command", choices=("setup", "report", "check-kdotool"))
    parser.add_argument("--root", type=Path, default=PROJECT / ".local/dependencies")
    args = parser.parse_args()
    start = time.monotonic()
    result = {"schema_version": 1, "command": args.command, "ok": False, "errors": []}
    try:
        root = args.root.absolute()
        data = policy()
        with tempfile.TemporaryDirectory(prefix="kde-dependencies-") as home:
            runner = Runner(home, 900 if args.command == "setup" else 120 if args.command == "report" else 10)
            if args.command == "setup":
                root = private_directory(root)
                _, errors = native_report(runner, data, Path("/usr/bin/python"))
                if errors:
                    result["errors"] = errors
                else:
                    python = root / "venv/bin/python"
                    if not python.exists():
                        runner.run(["/usr/bin/python", "-I", "-m", "venv", "--system-site-packages", str(root / "venv")], seconds=60)
                    result["kdotool"] = build(runner, root, data)
                    result["custom_script_check"] = check_kdotool(runner, root)
                    result["environment"], result["errors"] = native_report(runner, data, python)
            elif args.command == "report":
                result["environment"], result["errors"] = native_report(runner, data, root / "venv/bin/python")
                try:
                    result["kdotool"] = verify_build(runner, root, data)
                    result["custom_script_check"] = check_kdotool(runner, root)
                except Failure as exc:
                    result["errors"].append(exc.error)
            else:
                result["kdotool"] = verify_build(runner, root, data)
                result["custom_script_check"] = check_kdotool(runner, root)
    except Failure as exc:
        result["errors"].append(exc.error)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["errors"].append({"code": "invalid_state", "message": f"Invalid dependency state: {exc}", "repair": "Use a fresh --root and rerun setup"})
    result["ok"] = not result["errors"]
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
