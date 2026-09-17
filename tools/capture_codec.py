"""Strict raw ScreenShot2 decoding for the private M1 feasibility probe.

This is deliberately limited to the harness's single unscaled 1280x720 output,
not a general screenshot API. The caller must establish EOF separately: a raw
buffer of the expected length alone cannot establish capture completion.
"""

import math
import sys
from collections.abc import Mapping

from PIL import Image


MAX_RAW_BYTES = 8 * 1024 * 1024
WIDTH, HEIGHT, STRIDE = 1280, 720, 5120
QT_ARGB32_PREMULTIPLIED = 6
FIXTURE_COLORS = ((32, 64, 128, 255), (224, 96, 32, 255),
                  (48, 192, 128, 255), (144, 64, 192, 255))


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


def pixel_checks(image, state, geometry):
    """Check fixture quadrants and every marker bit at current client origin.

    Return JSON-safe per-sample evidence; on mismatch raise Failure with that
    same evidence available as ``checks``. Geometry is the query's ``client``
    rectangle, in unscaled output pixels. Fractional geometry is unsupported
    because silently rounding it could certify the wrong source pixel.
    """
    if image.mode != "RGBA" or image.size != (WIDTH, HEIGHT):
        raise Failure("fixture checks require the full RGBA output")
    if type(state) is not int or not 0 <= state <= 0xffffffff:
        raise Failure("fixture state must be a uint32")
    if not isinstance(geometry, Mapping):
        raise Failure("fixture client geometry must be a mapping")
    coords = []
    for key in ("x", "y", "width", "height"):
        value = geometry.get(key)
        if (type(value) not in (int, float)
                or (type(value) is float and not math.isfinite(value))
                or value != int(value)):
            raise Failure(f"invalid fixture client geometry {key}")
        coords.append(int(value))
    x, y, width, height = coords
    # The fixture is normally 640x360. These minima keep the marker complete
    # and quarter-height quadrant samples below its y=16..47 strip.
    if (x < 0 or y < 0 or width < 272 or height < 192
            or x + width > WIDTH or y + height > HEIGHT):
        raise Failure("fixture client geometry lies outside supported bounds")
    checks = []

    def sample(kind, index, local_x, local_y, expected):
        gx, gy = x + local_x, y + local_y
        observed = tuple(image.getpixel((gx, gy)))
        checks.append({"kind": kind, kind: index, "x": gx, "y": gy,
                       "expected": list(expected), "observed": list(observed),
                       "matched": observed == expected})

    for quadrant in range(4):
        local_x = width // 4 if quadrant % 2 == 0 else 3 * width // 4
        local_y = height // 4 if quadrant < 2 else 3 * height // 4
        sample("quadrant", quadrant, local_x, local_y,
               FIXTURE_COLORS[(quadrant + state) % 4])
    for bit in range(32):
        color = 255 if state & (1 << bit) else 0
        sample("bit", bit, 16 + bit * 8 + 4, 32, (color, color, color, 255))
    failed = [check for check in checks if not check["matched"]]
    if failed:
        first = failed[0]
        error = Failure(f"fixture {first['kind']} pixel mismatch at "
                        f"({first['x']}, {first['y']}): expected "
                        f"{first['expected']}, observed {first['observed']}")
        error.checks = checks
        raise error
    return checks
