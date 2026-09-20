"""Provisional packaged ScreenShot2 codec derived from tools/capture_codec.py.

This is deliberately limited to the private desktop's single unscaled 1280x720 output,
not a general screenshot API. The caller must establish EOF separately: a raw
buffer of the expected length alone cannot establish capture completion.
"""

import sys
from collections.abc import Mapping

from PIL import Image


MAX_RAW_BYTES = 8 * 1024 * 1024
WIDTH, HEIGHT, STRIDE = 1280, 720, 5120
QT_ARGB32_PREMULTIPLIED = 6


class Failure(ValueError):
    """A capture cannot satisfy the narrowly supported pixel contract."""


def validate_metadata(metadata, screen):
    """Validate normalized ScreenShot2 metadata and return required byte count.

    The D-Bus boundary must normalize typed values to Python builtins while
    retaining booleans as bool. Numeric coercion here would conceal bad types.
    """
    if not isinstance(metadata, Mapping):
        raise Failure("capture metadata must be a mapping")
    if not isinstance(screen, str) or not screen:
        raise Failure("expected screen must be a nonempty string")
    for key, expected in (("type", "raw"), ("screen", screen)):
        if not isinstance(metadata.get(key), str) or metadata[key] != expected:
            raise Failure(f"unexpected capture {key}")
    for key, expected in (("width", WIDTH), ("height", HEIGHT),
                          ("stride", STRIDE), ("format", QT_ARGB32_PREMULTIPLIED)):
        if type(metadata.get(key)) is not int or metadata[key] != expected:
            raise Failure(f"unexpected capture {key}")
    scale = metadata.get("scale")
    if type(scale) not in (int, float) or scale != 1:
        raise Failure("unexpected capture scale")
    expected_bytes = metadata["stride"] * metadata["height"]
    if not 0 < expected_bytes <= MAX_RAW_BYTES:
        raise Failure("capture payload exceeds raw byte cap")
    return expected_bytes


def _decode_argb32(raw, size, stride, byteorder):
    """Decode native Qt premultiplied words, with rows already top-down.

    A word 0xAARRGGBB is B,G,R,A in little endian and A,R,G,B in big
    endian. Pillow's RGBa image mode retains premultiplication until convert;
    converting to RGBA unpremultiplies. We explicitly normalize zero alpha to
    (0,0,0,0), including malformed input with nonzero transparent channels.
    This small helper permits synthetic endian/alpha tests without weakening
    the fixed output metadata contract.
    """
    if byteorder not in ("little", "big"):
        raise Failure("unsupported capture byte order")
    raw_mode = "BGRa" if byteorder == "little" else "aRGB"
    try:
        image = Image.frombytes("RGBa", size, raw, "raw", raw_mode, stride, 1).convert("RGBA")
        zero_alpha = image.getchannel("A").point([255] + [0] * 255)
        image.paste((0, 0, 0, 0), mask=zero_alpha)
        return image
    except (ValueError, OSError) as exc:
        raise Failure(f"cannot decode premultiplied capture: {exc}") from exc


def decode(raw, metadata, screen):
    """Return straight RGBA pixels only for the complete supported payload."""
    expected_bytes = validate_metadata(metadata, screen)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != expected_bytes:
        raise Failure("capture payload length/type does not match metadata")
    return _decode_argb32(raw, (WIDTH, HEIGHT), STRIDE, sys.byteorder)

