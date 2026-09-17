#!/usr/bin/env python3
"""Build the opt-in issue #12 KWin fault plugin locally; never load it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ECM_VERSION = "6.26.0"
ECM_URL = f"https://download.kde.org/stable/frameworks/6.26/extra-cmake-modules-{ECM_VERSION}.tar.xz"
ECM_SHA256 = "f4e10d9d45aafb5273e996196040f4e420f0bc4071c208282aae94d9ad8e1743"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_bounded(argv, *, env, timeout, output_limit=1024 * 1024):
    """Bound build output/wait, kill its owned group, and reap the direct child."""
    process = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    chunks = bytearray()
    error = None
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "build subprocess deadline exceeded"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                available = output_limit - len(chunks)
                chunks.extend(data[:available])
                if len(data) > available:
                    error = "build subprocess output limit exceeded"
                    break
            if error:
                break
    finally:
        # Cleanup applies on success too: a detached compiler child must not
        # survive merely because the process-group leader exited successfully.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
        selector.close()
        process.stdout.close()
    return {"returncode": process.returncode, "output": chunks.decode("utf-8", errors="replace"),
            "error": error, "output_limit_bytes": output_limit,
            "process_group": process.pid, "group_cleanup": "SIGKILL and bounded direct-child wait"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/issue12-eis-fault")
    args = parser.parse_args()
    output = args.output.absolute()
    if not output.is_relative_to(ROOT / ".local") or not output.name.startswith("issue12-"):
        parser.error("output must be an issue12-* directory under this checkout's ignored .local")
    for path in (output, *output.parents):
        if path.is_symlink():
            parser.error("build path must not traverse symbolic links")
    output.mkdir(parents=True, exist_ok=True)
    build_home = output / "private-home"
    private_paths = {"HOME": build_home, "XDG_CONFIG_HOME": build_home / "config",
                     "XDG_DATA_HOME": build_home / "data", "XDG_CACHE_HOME": build_home / "cache",
                     "XDG_STATE_HOME": build_home / "state", "XDG_RUNTIME_DIR": output / "runtime",
                     "TMPDIR": output / "tmp"}
    for path in private_paths.values():
        if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
            parser.error("private build environment paths must be owned non-symlink directories")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    build_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                 **{key: str(path) for key, path in private_paths.items()}}
    receipt = {"schema_version": 1, "purpose": "test-only KWin EIS pause fault; no live load",
               "started_unix_ns": time.time_ns(), "commands": [], "status": "building",
               "plugin_id": "issue12_eis_fault", "kwin_abi": "6.7.5",
               "build_environment": build_env,
               "ecm": {"version": ECM_VERSION, "url": ECM_URL, "sha256": ECM_SHA256}}
    receipt_path = output / "build-receipt.json"
    if receipt_path.exists():
        receipt_path.rename(output / f"build-receipt-{time.time_ns()}.json")

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    def run(argv, timeout=90, check=True):
        item = {"argv": [str(arg) for arg in argv], "timeout_seconds": timeout}
        receipt["commands"].append(item)
        save()
        item.update(run_bounded(item["argv"], env=build_env, timeout=timeout))
        save()
        if item["error"] or (check and item["returncode"]):
            raise RuntimeError(f"command failed ({item['returncode']}): {item['argv']}\n"
                               f"{item['error'] or ''}\n{item['output']}")
        return item["output"]

    try:
        receipt["versions"] = run(["pacman", "-Q", "kwin", "qt6-base", "qt6-declarative",
                                   "kcoreaddons", "kconfig", "kwindowsystem", "cmake", "gcc",
                                   "wayland", "libepoxy", "libdrm", "vulkan-headers"])
        receipt["compiler"] = run(["c++", "--version"])
        archive = output / f"extra-cmake-modules-{ECM_VERSION}.tar.xz"
        if not archive.exists():
            with urllib.request.urlopen(ECM_URL, timeout=30) as response:
                archive.write_bytes(response.read(16 * 1024 * 1024))
        if digest(archive) != ECM_SHA256:
            raise RuntimeError("pinned ECM archive checksum mismatch")
        source = output / f"extra-cmake-modules-{ECM_VERSION}"
        if not source.exists():
            with tarfile.open(archive) as tar:
                tar.extractall(output, filter="data")
        prefix = output / "ecm-prefix"
        run(["cmake", "-S", source, "-B", output / "ecm-build",
             f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DBUILD_TESTING=OFF", "-DBUILD_HTML_DOCS=OFF",
             "-DBUILD_MAN_DOCS=OFF", "-DBUILD_QTHELP_DOCS=OFF"])
        run(["cmake", "--install", output / "ecm-build"])
        build = output / "build"
        run(["cmake", "-S", ROOT / "tools/eis_fault_plugin", "-B", build,
             f"-DCMAKE_PREFIX_PATH={prefix}", "-DCMAKE_BUILD_TYPE=RelWithDebInfo"])
        run(["cmake", "--build", build, "--parallel", "2"], timeout=120)
        binary = build / "issue12_eis_fault.so"
        receipt["binary"] = {"path": str(binary), "sha256": digest(binary)}
        metadata = json.loads(run([build / "issue12_eis_metadata_audit", binary]))
        receipt["embedded_metadata"] = metadata
        if (metadata.get("IID") != "org.kde.kwin.PluginFactoryInterface6.7.5"
                or metadata.get("MetaData", {}).get("KPlugin", {}).get("EnabledByDefault") is not False
                or metadata.get("MetaData", {}).get("KPlugin", {}).get("Id") != "issue12_eis_fault"):
            raise RuntimeError("embedded plugin ABI/opt-in metadata audit failed")
        receipt["dynamic_section"] = run(["readelf", "-d", binary])
        receipt["resolved_libraries"] = run(["ldd", binary])
        receipt["compile_commands"] = json.loads((build / "compile_commands.json").read_text())
        paths = [ROOT / "tools/build_eis_fault_plugin.py",
                 *sorted((ROOT / "tools/eis_fault_plugin").glob("*")),
                 *sorted(Path("/usr/include/kwin").rglob("*.h")),
                 *sorted(Path("/usr/lib/cmake/KWin").glob("*.cmake")),
                 Path("/usr/lib/libkwin.so.6.7.5")]
        receipt["input_sha256"] = {str(path): digest(path) for path in paths if path.is_file()}
        receipt["status"] = "built-not-loaded"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        raise
    finally:
        receipt["completed_unix_ns"] = time.time_ns()
        save()
        print(receipt_path)


if __name__ == "__main__":
    main()
