"""Installed provider capture boundary: real native FD copies and PNG codec."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import dbus
import dbus.lowlevel
from gi.repository import GLib
from PIL import Image

from agent_desktop import provisional_capture as capture
from agent_desktop import provisional_codec as codec

METADATA = {'type': 'raw', 'width': 1280, 'height': 720, 'stride': 5120,
            'format': 6, 'screen': 'Virtual-1', 'scale': 1.0}
RAW_BYTES = 720 * 5120


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'desktop'
        self.runtime.mkdir(mode=0o700)
        self.folder = self.root / 'capture-test'
        self.folder.mkdir(mode=0o700)
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.runtime / 'bus'))
        (self.runtime / 'bus').chmod(0o600)
        self.addCleanup(self.socket.close)
        self.config = {'generation': 'test-generation', 'request_id': self.folder.name,
                       'deadline': time.monotonic() + 3, 'screen': 'Virtual-1',
                       'output_dir': str(self.folder), 'runtime_dir': str(self.runtime),
                       'bus_address': 'unix:path=' + str(self.runtime / 'bus')}
        self.config_path = self.folder / 'request.json'
        self.write_config()
        self.env = {'DBUS_SESSION_BUS_ADDRESS': self.config['bus_address'], 'XDG_RUNTIME_DIR': str(self.runtime)}
        self.writers = []
        self.addCleanup(self.close_writers)
        self.bus = Mock()
        self.pending = Mock()
        self.bus.close.side_effect = self.close_writers
        self.inodes = []

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def close_writers(self):
        for writer in self.writers:
            if writer.poll() is None:
                writer.kill()
            writer.wait(timeout=1)

    def fake_call(self, *, eof=True, metadata=METADATA, byte_count=RAW_BYTES):
        def call(destination, path, interface, method, signature, args, reply, error, **kwargs):
            message = dbus.lowlevel.MethodCallMessage(destination, path, interface, method)
            message.append(*args, signature=signature)
            received = message.get_args_list()[2]
            writer_fd = received.take()
            self.inodes.append(os.readlink(f'/proc/self/fd/{writer_fd}'))
            try:
                self.writers.append(subprocess.Popen(
                    [sys.executable, '-I', '-c', '''import os, sys, time
fd = int(sys.argv[1])
payload = memoryview(bytes(int(sys.argv[2])))
while payload:
    payload = payload[os.write(fd, payload):]
if sys.argv[3] == 'hold':
    time.sleep(5)
os.close(fd)
''', str(writer_fd), str(byte_count), 'close' if eof else 'hold'], pass_fds=(writer_fd,),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            finally:
                os.close(writer_fd)
                message = None
            GLib.idle_add(lambda: reply(metadata))
            return self.pending
        self.bus.call_async.side_effect = call

    def run_capture(self):
        with patch.object(dbus.bus, 'BusConnection', return_value=self.bus) as connection, \
                patch.dict(os.environ, self.env):
            result = capture.capture_child(self.config_path)
        connection.assert_called_once_with(self.config['bus_address'])
        self.bus.close.assert_called_once_with()
        for inode in self.inodes:
            self.assertEqual(capture.pipe_inventory(inode), [])
        return result, json.loads((self.folder / 'result.json').read_text())


class ConfigTests(CaptureFixture):
    def test_valid_configuration_is_anchored_and_private(self):
        config, fd = capture.load_config(self.config_path, self.env)
        try:
            self.assertEqual(config, self.config)
            self.assertEqual(os.fstat(fd).st_ino, self.folder.stat().st_ino)
        finally:
            os.close(fd)

    def test_unsafe_or_ambiguous_inputs_rejected_before_bus_connect(self):
        cases = [('generation', '../old'), ('request_id', 'wrong'), ('screen', ''),
                 ('deadline', True), ('deadline', float('nan')), ('deadline', time.monotonic() - 1),
                 ('deadline', time.monotonic() + 99), ('output_dir', '/tmp/elsewhere'),
                 ('runtime_dir', str(self.root)), ('bus_address', 'unix:path=/tmp/host'), ('fault', 'stall-pipe')]
        original = dict(self.config)
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.config = original | {key: value}
                self.write_config()
                with self.assertRaises((capture.Failure, FileNotFoundError)):
                    capture.load_config(self.config_path, self.env)

    def test_private_endpoint_cannot_come_from_ambient_host_environment(self):
        with self.assertRaises(capture.Failure):
            capture.load_config(self.config_path, self.env | {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/0/bus'})

    def test_rejects_symlink_config_world_readable_file_and_symlink_ancestor(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(capture.Failure):
            capture.load_config(self.config_path, self.env)
        self.config_path.chmod(0o600)
        actual = self.folder / 'actual.json'
        self.config_path.rename(actual)
        self.config_path.symlink_to(actual)
        with self.assertRaises(OSError):
            capture.load_config(self.config_path, self.env)
        self.config_path.unlink()
        actual.rename(self.config_path)
        alias = self.root / 'alias'
        alias.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(OSError):
            capture.load_config(alias / 'request.json', self.env)

    def test_refuses_existing_publication_and_duplicate_json_fields(self):
        (self.folder / 'image.png').write_bytes(b'preserve')
        with self.assertRaises(capture.Failure):
            capture.load_config(self.config_path, self.env)
        self.assertEqual((self.folder / 'image.png').read_bytes(), b'preserve')
        (self.folder / 'image.png').unlink()
        self.config_path.write_text('{"generation":"one","generation":"two"}')
        with self.assertRaises(capture.Failure):
            capture.load_config(self.config_path, self.env)


class TransportTests(CaptureFixture):
    def test_call_setup_failure_closes_original_wrapper_reader_and_bus(self):
        self.bus.call_async.side_effect = RuntimeError('injected queue failure')
        original_pipe = os.pipe2
        def pipe(flags):
            fds = original_pipe(flags)
            self.inodes.append(os.readlink(f'/proc/self/fd/{fds[0]}'))
            return fds
        with patch.object(capture.os, 'pipe2', side_effect=pipe):
            result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertFalse(receipt['ok'])
        self.assertIn('queue failure', receipt['message'])
        self.assertTrue(receipt['session_stop_required'])
        self.assertFalse((self.folder / 'image.png').exists())

    def test_full_raw_bytes_without_eof_cannot_satisfy_readiness(self):
        self.config['deadline'] = time.monotonic() + .6
        self.write_config()
        self.fake_call(eof=False)
        result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertEqual(receipt['code'], 'capture_timeout')
        self.assertEqual(receipt['raw_bytes'], RAW_BYTES)
        self.assertFalse(receipt['eof'])
        self.pending.cancel.assert_called_once_with()
        self.assertFalse((self.folder / 'image.png').exists())

    def test_valid_complete_png_receipt_has_identity_hash_and_private_permissions(self):
        self.fake_call()
        result, receipt = self.run_capture()
        self.assertEqual(result, 0)
        self.assertTrue(receipt['ok'])
        self.assertTrue(receipt['eof'])
        self.assertEqual(receipt['generation'], self.config['generation'])
        self.assertEqual(receipt['request_id'], self.config['request_id'])
        self.assertLess(receipt['completed_at'], self.config['deadline'])
        final = self.folder / 'image.png'
        self.assertEqual(receipt['path'], str(final))
        self.assertEqual(receipt['png_sha256'], capture.hashlib.sha256(final.read_bytes()).hexdigest())
        self.assertEqual(receipt['png_bytes'], final.stat().st_size)
        with Image.open(final) as image:
            image.load()
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.getpixel((1279, 719)), (0, 0, 0, 0))
        for name in ('request.json', 'image.png', 'result.json'):
            self.assertEqual((self.folder / name).stat().st_mode & 0o777, 0o600)
        self.assertLess((self.folder / 'result.json').stat().st_size, capture.MAX_RECEIPT)

    def test_eof_with_short_image_is_rejected(self):
        self.fake_call(byte_count=128)
        result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertTrue(receipt['eof'])
        self.assertFalse(receipt['ok'])
        self.assertFalse((self.folder / 'image.png').exists())

    def test_wrong_metadata_rejects_capture(self):
        self.fake_call(metadata=METADATA | {'screen': 'Other-1'})
        result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertFalse(receipt['ok'])
        self.assertTrue(receipt['session_stop_required'])

    def test_disposal_failure_attempts_other_releases_and_cannot_publish_success(self):
        self.fake_call()
        self.pending.cancel.side_effect = RuntimeError('native cancellation failed')
        result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertEqual(receipt['code'], 'capture_cleanup')
        self.assertTrue(receipt['session_stop_required'])
        self.assertFalse((self.folder / 'image.png').exists())

    def test_png_encode_output_must_fully_decode_before_publication(self):
        self.fake_call()
        def corrupt_save(image, stream, **kwargs):
            stream.write(b'not a complete PNG')
        with patch.object(Image.Image, 'save', corrupt_save):
            result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertFalse(receipt['ok'])
        self.assertFalse((self.folder / 'image.png').exists())
        self.assertFalse((self.folder / 'image.partial').exists())

    def test_late_receipt_publication_retracts_success_and_png(self):
        self.fake_call()
        real_publish = capture._publish_receipt
        def publish(fd, value):
            real_publish(fd, value)
            if value['ok']:
                # Clock injection targets publication acceptance without sleep.
                clock.return_value = self.config['deadline'] + .01
        with patch.object(capture.time, 'monotonic', wraps=time.monotonic) as clock, \
                patch.object(capture, '_publish_receipt', side_effect=publish):
            result, receipt = self.run_capture()
        self.assertEqual(result, 1)
        self.assertEqual(receipt['code'], 'capture_timeout')
        self.assertFalse(receipt['ok'])
        self.assertNotIn('path', receipt)
        self.assertFalse((self.folder / 'image.png').exists())


class CodecTests(unittest.TestCase):
    def test_packaged_codec_matches_m1_for_premultiplied_channels_and_endianness(self):
        spec = importlib.util.spec_from_file_location('historical_codec', Path(__file__).resolve().parents[1] / 'tools/capture_codec.py')
        historical = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(historical)
        for order in ('little', 'big'):
            raw = b''.join(word.to_bytes(4, order) for word in (0x80402010, 0x00102030, 0xff010203, 0xff204080))
            image = codec._decode_argb32(raw, (2, 2), 8, order)
            expected = historical._decode_argb32(raw, (2, 2), 8, order)
            self.assertEqual(image.tobytes(), expected.tobytes())
            self.assertEqual(image.getpixel((0, 0)), (127, 63, 31, 128))
            self.assertEqual(image.getpixel((1, 0)), (0, 0, 0, 0))
            self.assertEqual(image.getpixel((0, 1)), (1, 2, 3, 255))

    def test_strict_metadata_does_not_coerce_types_or_dimensions(self):
        for key, value in (('scale', True), ('scale', 1.5), ('width', 1280.0), ('height', 721),
                           ('stride', 5124), ('format', 5), ('type', 'png'), ('screen', 'other')):
            with self.subTest(key=key), self.assertRaises(codec.Failure):
                codec.validate_metadata(METADATA | {key: value}, 'Virtual-1')
        for raw in (bytes(RAW_BYTES - 1), bytes(RAW_BYTES + 1), 'x' * RAW_BYTES):
            with self.assertRaises(codec.Failure):
                codec.decode(raw, METADATA, 'Virtual-1')


if __name__ == '__main__':
    unittest.main()
