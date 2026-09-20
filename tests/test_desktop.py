"""Private desktop construction and environment failure boundaries."""
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_desktop.artifacts import Store
from agent_desktop.contracts import ContractError
from agent_desktop.desktop import Desktop, dispose
from agent_desktop.environment import DEFAULTS, DISABLED, FIXED, PROTECTED, compose


class Child:
    returncode = None


class Children:
    def __init__(self):
        self.calls = []
        self.exits = []

    def start(self, argv, **kwargs):
        child = Child()
        self.calls.append((argv, kwargs, child))
        return child

    def poll(self, now=None):
        for child, code in self.exits:
            child.returncode = code
        self.exits.clear()


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='add-')
        self.root = Path(self.temp.name)
        self.generation = self.root / 'g'
        self.generation.mkdir(mode=0o700)
        self.store = Store(str(self.root / 'a'), 'default', 'a' * 32, create=True)
        self.children = Children()
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.store.close()
        self.temp.cleanup()

    def desktop(self):
        return Desktop(self.generation, self.children, self.store)

    def socket(self, path):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        self.sockets.append(sock)

    def construct(self, desktop):
        self.socket(desktop.root / 'bus')
        desktop.tick()
        self.assertEqual(desktop.phase, 'bus_probe')
        self.children.exits.append((desktop.probe, 0))
        desktop.tick()
        self.assertEqual(desktop.phase, 'compositor')
        self.socket(desktop.root / desktop.env['WAYLAND_DISPLAY'])
        desktop.tick()
        self.assertEqual(desktop.phase, 'constructed')

    def test_order_modes_no_activation_and_compositor_only_permission(self):
        desktop = self.desktop()
        self.assertEqual(len(self.children.calls), 1)
        self.assertEqual(desktop.phase, 'bus')
        config = (desktop.root / 'bus.conf').read_text()
        self.assertNotIn('servicedir', config)
        self.assertNotIn('systemd', config)
        self.assertIn('<auth>EXTERNAL</auth>', config)
        self.construct(desktop)
        for path in desktop.root.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        for index, (argv, kwargs, _) in enumerate(self.children.calls):
            self.assertEqual(kwargs['cwd'], str(desktop.root))
            self.assertNotIn('DISPLAY', kwargs['env'])
            self.assertEqual(kwargs['env'].get('KWIN_SCREENSHOT_NO_PERMISSION_CHECKS'), '1' if index == 2 else None)
        compositor = self.children.calls[2][0]
        self.assertEqual(compositor[compositor.index('--width') + 1], '1280')
        self.assertEqual(compositor[compositor.index('--height') + 1], '720')
        self.assertEqual(compositor[compositor.index('--scale') + 1], '1')
        self.assertEqual(compositor[compositor.index('--output-count') + 1], '1')

    def test_contamination_and_protected_override_fail_before_launch(self):
        desktop = self.desktop()
        contaminated = {key: 'HOST' for key in PROTECTED} | {'KWIN_SCREENSHOT_NO_PERMISSION_CHECKS': '1'}
        env = compose(contaminated, {'APP_FLAG': 'ok', 'PATH': '/custom'}, desktop.private)
        self.assertNotIn('HOST', env.values())
        self.assertNotIn('KWIN_SCREENSHOT_NO_PERMISSION_CHECKS', env)
        for key, value in (DISABLED | FIXED).items():
            self.assertEqual(env[key], value)
        count = len(self.children.calls)
        for key in [*PROTECTED, 'KWIN_SCREENSHOT_NO_PERMISSION_CHECKS', 'KWIN_EIS_NO_PERMISSION_CHECKS']:
            with self.subTest(key=key), self.assertRaises(ContractError) as caught:
                desktop.launch(['not-an-executable'], '/', {key: 'bad'}, stdout=None, stderr=None)
            self.assertEqual(caught.exception.code, 'invalid_arguments')
            self.assertEqual(len(self.children.calls), count)

    def test_selected_executable_argv_cwd_and_clean_defaults(self):
        desktop = self.desktop()
        self.construct(desktop)
        binary = self.root / 'chosen'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        with patch.dict(os.environ, DISPLAY='host', SSH_AUTH_SOCK='/host-agent', SECRET='secret'):
            desktop.launch(['chosen', 'two words'], str(self.root), {'PATH': '', 'APP_FLAG': 'yes'}, stdout=None, stderr=None)
        argv, kwargs, _ = self.children.calls[-1]
        self.assertEqual(argv, ['chosen', 'two words'])
        self.assertEqual(kwargs['executable'], str(binary))
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertEqual(kwargs['env']['PATH'], '')
        self.assertEqual(kwargs['env']['APP_FLAG'], 'yes')
        self.assertEqual(kwargs['env']['LANG'], DEFAULTS['LANG'])
        self.assertNotIn('SSH_AUTH_SOCK', kwargs['env'])
        self.assertNotIn('SECRET', kwargs['env'])

    def test_shared_deadline_and_success_acceptance_recheck(self):
        desktop = self.desktop()
        with self.assertRaises(ContractError) as caught:
            desktop.tick(desktop.deadline)
        self.assertEqual(caught.exception.code, 'timeout')
        self.socket(desktop.root / 'bus')
        desktop.tick()
        self.children.exits.append((desktop.probe, 0))
        desktop.tick()
        self.socket(desktop.root / desktop.env['WAYLAND_DISPLAY'])
        with patch('agent_desktop.desktop.time.monotonic', return_value=desktop.deadline):
            with self.assertRaises(ContractError):
                desktop.tick(desktop.deadline - .1)
        self.assertEqual(desktop.phase, 'compositor')

    def test_probe_and_essential_failures_are_reaped_before_observation(self):
        desktop = self.desktop()
        self.socket(desktop.root / 'bus')
        desktop.tick()
        self.children.exits.append((desktop.probe, 1))
        with self.assertRaises(ContractError):
            desktop.tick()
        self.assertIsNone(desktop.compositor)
        self.children.exits.append((desktop.probe, 0))
        desktop.tick()
        self.socket(desktop.root / desktop.env['WAYLAND_DISPLAY'])
        desktop.tick()
        self.children.exits.append((desktop.bus, 0))
        with self.assertRaises(ContractError):
            desktop.launch(['/usr/bin/true'], '/', {}, stdout=None, stderr=None)
        self.assertEqual(len(self.children.calls), 3)

    def test_unsafe_socket_and_existing_or_linked_roots_fail_closed(self):
        desktop = self.desktop()
        (desktop.root / 'bus').write_text('not a socket')
        with self.assertRaises(ContractError):
            desktop.tick()
        with self.assertRaises(FileExistsError):
            self.desktop()
        dispose(self.generation)
        desktop.root.symlink_to(self.root / 'a', target_is_directory=True)
        with self.assertRaises(ContractError):
            dispose(self.generation)
        self.assertTrue(self.store.path.exists())

    def test_disposal_does_not_follow_inner_links_or_remove_durable_records(self):
        desktop = self.desktop()
        (desktop.root / 'external').symlink_to(self.root / 'a', target_is_directory=True)
        dispose(self.generation)
        dispose(self.generation)
        self.assertFalse(desktop.root.exists())
        self.assertTrue(self.generation.exists())
        self.assertEqual(self.store.read()['generation'], 'a' * 32)

    def test_path_length_and_symlink_ancestor_rejected_before_spawn(self):
        long = self.root / ('x' * 90)
        long.mkdir(mode=0o700)
        with self.assertRaises(ContractError):
            Desktop(long, self.children, self.store)
        self.assertEqual(self.children.calls, [])
        linked = self.root / 'linked'
        linked.symlink_to(self.generation, target_is_directory=True)
        with self.assertRaises(ContractError):
            Desktop(linked, self.children, self.store)
        self.assertEqual(self.children.calls, [])


if __name__ == '__main__':
    unittest.main()
