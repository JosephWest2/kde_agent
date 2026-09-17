import importlib.util
import json
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


spec = importlib.util.spec_from_file_location(
    "capture_codec", Path(__file__).resolve().parents[1] / "tools/capture_codec.py")
codec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codec)

SCREEN = "Virtual-1"
METADATA = {"type": "raw", "width": 1280, "height": 720, "stride": 5120,
            "format": 6, "screen": SCREEN, "scale": 1.0}


class MetadataTests(unittest.TestCase):
    def test_exact_supported_contract(self):
        self.assertEqual(codec.validate_metadata(METADATA, SCREEN), 3686400)
        self.assertEqual(codec.validate_metadata(dict(METADATA, scale=1), SCREEN), 3686400)

    def test_each_required_field_is_required(self):
        for key in METADATA:
            metadata = dict(METADATA)
            del metadata[key]
            with self.subTest(key=key), self.assertRaises(codec.Failure):
                codec.validate_metadata(metadata, SCREEN)

    def test_rejects_format_dimensions_stride_scale_and_types(self):
        cases = {
            "type": ["png", b"raw", None, True],
            "width": [640, -1280, 0, 10**100, 1280.0, "1280", True],
            "height": [360, 0, -720, 720.0, True],
            "stride": [0, -5120, 5119, 5124, 2**40, 5120.0, True],
            "format": [4, 5, 0, 6.0, "6", True],
            "screen": ["Virtual-2", "", None, True],
            "scale": [0, -1, 1.5, float("inf"), float("nan"), "1", True],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(codec.Failure):
                    codec.validate_metadata(dict(METADATA, **{key: value}), SCREEN)
        for metadata in (None, [], "raw"):
            with self.subTest(metadata=metadata), self.assertRaises(codec.Failure):
                codec.validate_metadata(metadata, SCREEN)

    def test_rejects_short_extra_and_oversized_payload_before_decode(self):
        size = 3686400
        for raw in (b"", b"\0" * (size - 1), b"\0" * (size + 1),
                    b"\0" * (codec.MAX_RAW_BYTES + 1), "\0" * size):
            with self.subTest(length=len(raw), type=type(raw)), self.assertRaises(codec.Failure):
                codec.decode(raw, METADATA, SCREEN)


class DecodeTests(unittest.TestCase):
    def test_both_endians_preserve_channels_and_top_down_rows(self):
        words = (0xff204080, 0xffe06020, 0xff30c080, 0xff9040c0)
        for byteorder in ("little", "big"):
            raw = b"".join(word.to_bytes(4, byteorder) for word in words)
            image = codec._decode_argb32(raw, (2, 2), 8, byteorder)
            with self.subTest(byteorder=byteorder):
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual([image.getpixel((x, y)) for y in range(2) for x in range(2)],
                                 [(32, 64, 128, 255), (224, 96, 32, 255),
                                  (48, 192, 128, 255), (144, 64, 192, 255)])

    def test_premultiplication_and_zero_alpha_for_both_endians(self):
        # Premultiplied channels 64,32,16 at alpha 128 become 127,63,31
        # under Pillow's integer unpremultiplication, not 64,32,16.
        words = (0x80402010, 0x00000000, 0x00102030, 0xff010203)
        for byteorder in ("little", "big"):
            raw = b"".join(word.to_bytes(4, byteorder) for word in words)
            image = codec._decode_argb32(raw, (4, 1), 16, byteorder)
            with self.subTest(byteorder=byteorder):
                self.assertEqual([image.getpixel((x, 0)) for x in range(4)],
                                 [(127, 63, 31, 128), (0, 0, 0, 0),
                                  (0, 0, 0, 0), (1, 2, 3, 255)])

    def test_unknown_endian_is_rejected(self):
        with self.assertRaises(codec.Failure):
            codec._decode_argb32(b"\0" * 4, (1, 1), 4, "mixed")

    def test_full_output_uses_native_byte_order(self):
        raw = (0xff204080).to_bytes(4, sys.byteorder) * (1280 * 720)
        image = codec.decode(raw, METADATA, SCREEN)
        self.assertEqual(image.size, (1280, 720))
        self.assertEqual(image.getpixel((0, 0)), (32, 64, 128, 255))
        self.assertEqual(image.getpixel((1279, 719)), (32, 64, 128, 255))


class PixelChecksTests(unittest.TestCase):
    geometry = {"x": 137.0, "y": 83, "width": 640, "height": 360}

    def fixture_image(self, state):
        image = Image.new("RGBA", (1280, 720), (9, 8, 7, 255))
        draw = ImageDraw.Draw(image)
        # Independent rendering of the C fixture's regions (inclusive PIL
        # rectangles), offset from the traditional centered-window position.
        colors = [(32, 64, 128, 255), (224, 96, 32, 255),
                  (48, 192, 128, 255), (144, 64, 192, 255)]
        for row in range(2):
            for col in range(2):
                left, top = 137 + col * 320, 83 + row * 180
                draw.rectangle((left, top, left + 319, top + 179),
                               fill=colors[(col + 2 * row + state) % 4])
        for bit in range(32):
            left = 137 + 16 + 8 * bit
            color = (255, 255, 255, 255) if state & (1 << bit) else (0, 0, 0, 255)
            draw.rectangle((left, 83 + 16, left + 7, 83 + 47), fill=color)
        return image

    def test_all_markers_and_quadrants_use_current_geometry(self):
        for state in (0, 0xffffffff, 0x13579bdf, 0x2468ace0):
            with self.subTest(state=state):
                checks = codec.pixel_checks(self.fixture_image(state), state, self.geometry)
                self.assertEqual(len(checks), 36)
                self.assertTrue(all(check["matched"] for check in checks))
                self.assertEqual([check["bit"] for check in checks if check["kind"] == "bit"],
                                 list(range(32)))
                self.assertEqual(json.loads(json.dumps(checks)), checks)

    def test_high_state_bit_mismatch_fails_even_with_matching_quadrants(self):
        state = 0x13579bdf
        with self.assertRaises(codec.Failure) as caught:
            codec.pixel_checks(self.fixture_image(state), state ^ (1 << 31), self.geometry)
        failures = [check for check in caught.exception.checks if not check["matched"]]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["bit"], 31)

    def test_channels_orientation_and_alpha_mismatches_fail(self):
        image = self.fixture_image(0)
        changed = image.copy()
        changed.putpixel((137 + 160, 83 + 90), (128, 64, 32, 255))
        alpha = image.copy()
        alpha.putpixel((137 + 160, 83 + 90), (32, 64, 128, 0))
        flipped = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        for invalid in (changed, alpha, flipped):
            with self.subTest(image=invalid), self.assertRaises(codec.Failure):
                codec.pixel_checks(invalid, 0, self.geometry)

    def test_invalid_geometry_state_or_image_cannot_certify_pixels(self):
        image = self.fixture_image(0)
        for key, value in (("x", True), ("y", 83.5), ("x", -1),
                           ("height", float("nan")), ("width", 2000),
                           ("width", 271), ("height", 100), ("x", 10**1000)):
            with self.subTest(key=key, value=value), self.assertRaises(codec.Failure):
                codec.pixel_checks(image, 0, dict(self.geometry, **{key: value}))
        for state in (True, -1, 2**32, 0.0):
            with self.subTest(state=state), self.assertRaises(codec.Failure):
                codec.pixel_checks(image, state, self.geometry)
        for invalid in (image.convert("RGB"), image.crop((0, 0, 640, 360))):
            with self.assertRaises(codec.Failure):
                codec.pixel_checks(invalid, 0, self.geometry)


if __name__ == "__main__":
    unittest.main()
