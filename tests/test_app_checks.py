"""The desktop-free checks of tests/integration/apps.py: canvas search, exported pixels, versions, skips."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integration'))
import apps  # noqa: E402
from smoke import SmokeFailure  # noqa: E402

GREY = (60, 60, 60)


class ImageCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def save(self, image):
        path = Path(self.temp.name) / f'{id(image)}.png'
        image.save(path)
        return path


class CanvasTests(ImageCase):
    def window(self, x, y, width, height):
        """A GIMP-like window: grey UI, small white brush previews, the image with a dashed boundary."""
        image = Image.new('RGB', (800, 600), GREY)
        draw = ImageDraw.Draw(image)
        for n in range(6):
            draw.rectangle([600 + 34 * (n % 3), 20 + 34 * (n // 3), 631 + 34 * (n % 3), 51 + 34 * (n // 3)],
                           fill='white')
        # GIMP's layer boundary: yellow and black dashes over the image's edge pixels.
        draw.rectangle([x, y, x + width - 1, y + height - 1], fill='white', outline=(255, 255, 0))
        for px in range(x, x + width, 8):
            draw.line([(px, y), (px + 3, y)], fill='black')
            draw.line([(px, y + height - 1), (px + 3, y + height - 1)], fill='black')
        for py in range(y, y + height, 8):
            draw.line([(x, py), (x, py + 3)], fill='black')
            draw.line([(x + width - 1, py), (x + width - 1, py + 3)], fill='black')
        return image

    def test_finds_the_image_at_full_and_reduced_zoom(self):
        self.assertEqual(apps.find_canvas(self.save(self.window(215, 191, 320, 240)), (320, 240)), [215, 191, 320, 240])
        self.assertEqual(apps.find_canvas(self.save(self.window(300, 300, 160, 120)), (320, 240)), [300, 300, 160, 120])

    def test_rejects_a_covered_unfinished_or_wrongly_shaped_image(self):
        covered = self.window(215, 191, 320, 240)
        ImageDraw.Draw(covered).rectangle([400, 250, 700, 300], fill=GREY)  # A dialog over part of it.
        self.assertIsNone(apps.find_canvas(self.save(covered), (320, 240)))
        self.assertIsNone(apps.find_canvas(self.save(self.window(215, 191, 320, 100)), (320, 240)))
        self.assertIsNone(apps.find_canvas(self.save(Image.new('RGB', (800, 600), GREY)), (320, 240)))


class DotTests(ImageCase):
    def exported(self, dot, size=(320, 240), radius=6):
        image = Image.new('RGB', size, 'white')
        if dot is not None:
            x, y = dot
            ImageDraw.Draw(image).ellipse([x - radius, y - radius, x + radius, y + radius], fill=(20, 20, 20))
        return self.save(image)

    def test_a_dab_on_the_clicked_pixel_passes(self):
        detail = apps.check_dot(self.exported((80, 60)), (320, 240), (80, 60))
        self.assertIn('pixel (80, 60) = 20', detail)

    def test_missing_shifted_mirrored_or_oversized_paint_fails(self):
        for path in (self.exported(None), self.exported((86, 60)), self.exported((239, 179)),
                     self.exported((80, 60), radius=50), self.exported((80, 60), size=(321, 240))):
            with self.subTest(path=path.name), self.assertRaises(SmokeFailure):
                apps.check_dot(path, (320, 240), (80, 60))

    def test_paint_elsewhere_as_well_fails(self):
        image = Image.open(self.exported((80, 60)))
        ImageDraw.Draw(image).point((319, 239), fill='black')
        with self.assertRaises(SmokeFailure):
            apps.check_dot(self.save(image), (320, 240), (80, 60))


class OutputTests(unittest.TestCase):
    def test_version_requirements(self):
        self.assertIsNone(apps.version_problem('gimp', 'GNU Image Manipulation Program version 3.2.6\n'))
        self.assertIn('older', apps.version_problem('gimp', 'GNU Image Manipulation Program version 2.10.38\n'))
        self.assertIsNone(apps.version_problem('blender', 'Blender 5.2.2 LTS\n\tbuild date: 2026-09-22\n'))
        self.assertIn('older', apps.version_problem('blender', 'Blender 4.1.1\n'))
        self.assertIn('no version', apps.version_problem('blender', 'Read prefs: something\n'))

    def test_blender_object_listing(self):
        listing = {'file': '/w/edited.blend', 'objects': [['Cube', 'MESH']]}
        output = 'Blender 5.2.2\nOBJECTS ' + json.dumps(listing) + '\nBlender quit\n'
        self.assertEqual(apps.parse_objects(output, Path('/w/edited.blend')), [['Cube', 'MESH']])
        # A missing file leaves the factory scene loaded under no file name.
        with self.assertRaises(SmokeFailure):
            apps.parse_objects(output.replace('/w/edited.blend', ''), Path('/w/edited.blend'))
        with self.assertRaises(SmokeFailure):
            apps.parse_objects('Blender quit\n', Path('/w/edited.blend'))


class SkipTests(unittest.TestCase):
    def run_main(self, require):
        environment = {key: value for key, value in os.environ.items() if key != apps.REQUIRE}
        if require:
            environment[apps.REQUIRE] = '1'
        out = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(apps, 'availability', return_value='/usr/bin/gimp is not installed'), \
                contextlib.redirect_stdout(out):
            code = apps.main(['--cli', '/nonexistent/agent-desktop', 'gimp'])
        return code, out.getvalue()

    def test_missing_application_skips_with_its_reason(self):
        code, out = self.run_main(require=False)
        self.assertEqual(code, 0)
        self.assertIn('skip gimp: /usr/bin/gimp is not installed', out)
        self.assertIn('PASS 0 scenario run(s), skipped 1 (gimp)', out)

    def test_required_host_tests_turn_the_skip_into_a_failure(self):
        code, out = self.run_main(require=True)
        self.assertEqual(code, 1)
        self.assertIn('FAIL gimp: /usr/bin/gimp is not installed', out)

    def test_missing_executable_is_reported_without_running_anything(self):
        with patch.object(apps, 'GIMP', '/nonexistent/gimp'), patch('subprocess.run') as run:
            self.assertEqual(apps.availability('gimp'), '/nonexistent/gimp is not installed')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
