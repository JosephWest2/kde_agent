"""Issue #12's deliberately small, header-audited libei sender surface."""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import tempfile

# C spellings preserve opaque object distinctions for compiler checking, although
# ctypes represents every opaque pointer as void*. Never dereference them here.
TYPES = {
    "context": ("struct ei *", C.c_void_p), "seat": ("struct ei_seat *", C.c_void_p),
    "device": ("struct ei_device *", C.c_void_p), "event": ("struct ei_event *", C.c_void_p),
    "opaque": ("void *", C.c_void_p), "string": ("const char *", C.c_char_p),
    "int": ("int", C.c_int), "cap": ("enum ei_device_capability", C.c_int),
    "event_type": ("enum ei_event_type", C.c_int), "bool": ("bool", C.c_bool),
    "u32": ("uint32_t", C.c_uint32), "u64": ("uint64_t", C.c_uint64),
    "void": ("void", None),
}
DECLARATIONS = {
    "ei_new_sender": ("context", ["opaque"]), "ei_unref": ("context", ["context"]),
    "ei_configure_name": ("void", ["context", "string"]),
    "ei_setup_backend_fd": ("int", ["context", "int"]), "ei_get_fd": ("int", ["context"]),
    "ei_dispatch": ("void", ["context"]), "ei_get_event": ("event", ["context"]),
    "ei_now": ("u64", ["context"]), "ei_event_get_type": ("event_type", ["event"]),
    "ei_event_get_seat": ("seat", ["event"]), "ei_event_get_device": ("device", ["event"]),
    "ei_event_unref": ("event", ["event"]),
    "ei_seat_ref": ("seat", ["seat"]), "ei_seat_unref": ("seat", ["seat"]),
    "ei_seat_has_capability": ("bool", ["seat", "cap"]),
    "ei_seat_bind_capabilities": ("void", ["seat", "..."]),
    "ei_seat_unbind_capabilities": ("void", ["seat", "..."]),
    "ei_device_ref": ("device", ["device"]), "ei_device_unref": ("device", ["device"]),
    "ei_device_has_capability": ("bool", ["device", "cap"]),
    "ei_device_start_emulating": ("void", ["device", "u32"]),
    "ei_device_stop_emulating": ("void", ["device"]),
    "ei_device_keyboard_key": ("void", ["device", "u32", "bool"]),
    "ei_device_frame": ("void", ["device", "u64"]),
}
CONSTANTS = {"EI_EVENT_CONNECT": 1, "EI_EVENT_DISCONNECT": 2,
             "EI_EVENT_SEAT_ADDED": 3, "EI_EVENT_SEAT_REMOVED": 4,
             "EI_EVENT_DEVICE_ADDED": 5, "EI_EVENT_DEVICE_REMOVED": 6,
             "EI_EVENT_DEVICE_PAUSED": 7, "EI_EVENT_DEVICE_RESUMED": 8,
             "EI_DEVICE_CAP_KEYBOARD": 4}

# The negative setup ownership rule below is implementation-specific, not the
# generic header promise. Reject unreviewed native builds before obtaining an FD.
SUPPORTED_LIBRARY = Path("/usr/lib/libei.so.1")
SUPPORTED_LIBRARY_SHA256 = "93897fc311319920c1c25e9422db62ebe8324d54c5e0c3d4a9f15a0a6cac2501"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, seconds=5):
    """Compiler/pkg-config children receive no caller compiler/loader overrides."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   start_new_session=True)
        try:
            process.wait(timeout=seconds)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        out.seek(0); err.seek(0)
        stdout, stderr = out.read(1024 * 1024), err.read(1024 * 1024)
        return subprocess.CompletedProcess(argv, process.returncode, stdout.decode(), stderr.decode())


def load():
    path = SUPPORTED_LIBRARY.resolve(strict=True)
    if platform.machine() != "x86_64" or digest(path) != SUPPORTED_LIBRARY_SHA256:
        raise RuntimeError("Unsupported libei build: re-audit ABI and failed-setup FD ownership before updating the tested library hash")
    library = C.CDLL(str(path))
    for name, (result, args) in DECLARATIONS.items():
        function = getattr(library, name)
        function.restype = TYPES[result][1]
        function.argtypes = [TYPES[arg][1] for arg in args if arg != "..."]
    return library


def capabilities(function, seat):
    # Supported x86_64 SysV ABI: each enum is promoted to int. Upstream 1.6's
    # va_arg consumes enum values until integer zero (headers call it NULL).
    # Explicit int zero avoids an implicit Python vararg conversion.
    function(seat, C.c_int(CONSTANTS["EI_DEVICE_CAP_KEYBOARD"]), C.c_int(0))


def audit(output):
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    lines = ["#include <libei.h>", "#include <stdio.h>", "#include <stdint.h>", "#include <stdbool.h>"]
    for name, (result, args) in DECLARATIONS.items():
        argtypes = ", ".join("..." if arg == "..." else TYPES[arg][0] for arg in args)
        lines.append(f'_Static_assert(__builtin_types_compatible_p(__typeof__(&{name}), '
                     f'{TYPES[result][0]} (*)({argtypes})), "{name}");')
    for name, value in CONSTANTS.items():
        lines.append(f'_Static_assert({name} == {value}, "{name}");')
    for typename, (_, ctype) in TYPES.items():
        if ctype:
            lines.append(f'_Static_assert(sizeof({TYPES[typename][0]}) == {C.sizeof(ctype)}, "sizeof {typename}");')
    lines += ['int main(void) { puts("header signatures, enum constants and ABI sizes passed"); return 0; }']
    source = output / "libei-audit.c"
    source.write_text("\n".join(lines) + "\n")
    flags_result = command(["/usr/bin/pkg-config", "--cflags", "--libs", "libei-1.0"])
    flags_result.check_returncode()
    compile_argv = ["/usr/bin/cc", "-std=c11", "-Wall", "-Werror", str(source), "-o", str(output / "libei-audit"), *flags_result.stdout.split()]
    result = command(compile_argv, seconds=10)
    (output / "compiler.stderr").write_text(result.stderr)
    result.check_returncode()
    observed_result = command([str(output / "libei-audit")], seconds=3)
    observed_result.check_returncode()
    observed = observed_result.stdout.strip()
    library = load()
    resolved = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines() if "/libei.so." in line})
    compiler = command(["/usr/bin/cc", "--version"], seconds=3)
    compiler.check_returncode()
    version = command(["/usr/bin/pkg-config", "--modversion", "libei-1.0"], seconds=3)
    version.check_returncode()
    receipt = {"passed": True, "command": compile_argv, "observed": observed,
               "compiler": compiler.stdout.splitlines()[0],
               "machine": __import__("platform").machine(),
               "native_release": version.stdout.strip(),
               "header_sha256": digest("/usr/include/libei-1.0/libei.h"), "binding_sha256": digest(__file__),
               "audit_source_sha256": digest(source), "library_files": {p: digest(p) for p in resolved},
               "declarations": DECLARATIONS, "constants": CONSTANTS,
               "dispatch_restype_is_void": library.ei_dispatch.restype is None}
    if receipt["machine"] != "x86_64" or receipt["native_release"] != "1.6.0":
        raise RuntimeError("This feasibility audit supports only the recorded x86_64 libei 1.6.0 baseline")
    (output / "audit.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    print(json.dumps(audit(parser.parse_args().output)))
