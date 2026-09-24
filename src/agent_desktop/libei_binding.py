"""Limited production libei sender ABI, audited against the pinned native build."""
from __future__ import annotations

import ctypes as C
import hashlib
from pathlib import Path
import platform

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
    "ei_device_get_seat": ("seat", ["device"]),
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
