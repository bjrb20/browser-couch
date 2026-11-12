import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'docker'))

import start

RELAY = dict(SOFA_TURN_PUBLIC_HOST='turn.example.org', SOFA_RELAY_IP='198.51.100.3', SOFA_HOST_IP='198.51.100.2')


class ContainerStartTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_refuses_missing_short_or_shared_pins(self):
        for env in ({}, dict(SOFA_PIN='short', SOFA_ADMIN_PIN='admin-pin'),
                    dict(SOFA_PIN='long-enough', SOFA_ADMIN_PIN=''),
                    dict(SOFA_PIN='long-enough', SOFA_ADMIN_PIN='12345'),
                    dict(SOFA_PIN=' padded-code', SOFA_ADMIN_PIN='admin-pin'),
                    dict(SOFA_PIN='same-code', SOFA_ADMIN_PIN='same-code')):
            with self.subTest(env=env), self.assertRaises(start.Refused):
                start.check_pins(env)
        start.check_pins(dict(SOFA_PIN='long-enough', SOFA_ADMIN_PIN='admin-pin'))

    def test_links_bios_read_only_and_copies_the_files_the_core_writes(self):
        bios, system = self.root/'bios', self.root/'system'
        (bios/'pcsx2/bios').mkdir(parents=True)
        (bios/'ps1-bios.bin').write_bytes(b'1')
        (bios/'pcsx2/bios/ps2-bios.bin').write_bytes(b'2')
        (bios/'pcsx2/bios/ps2-bios.NVM').write_bytes(b'n')
        (bios/'.hidden').write_bytes(b'h')
        self.assertEqual(start.link_bios(bios, system), 2)
        self.assertEqual(os.readlink(system/'ps1-bios.bin'), str(bios/'ps1-bios.bin'))
        self.assertEqual((system/'pcsx2/bios/ps2-bios.bin').read_bytes(), b'2')
        nvm = system/'pcsx2/bios/ps2-bios.NVM'
        self.assertFalse(nvm.is_symlink())
        nvm.write_bytes(b'written by the core')
        self.assertFalse((system/'.hidden').exists())
        (bios/'ps1-bios.bin').unlink()
        self.assertEqual(start.link_bios(bios, system), 1)
        self.assertFalse((system/'ps1-bios.bin').is_symlink())
        self.assertEqual(nvm.read_bytes(), b'written by the core')

    def test_relay_files_relay_only_to_the_game_host(self):
        turn = self.root/'turn'
        ice = start.relay_files(RELAY, turn)
        config = (turn/'turnserver.conf').read_text()
        servers = json.loads(ice.read_text())
        secret = (turn/'password').read_text().strip()
        self.assertEqual(servers, [dict(urls=['turns:turn.example.org:5349?transport=tcp'],
                                        username='sofa', credential=secret)])
        for line in ('listening-ip=198.51.100.3', 'relay-ip=198.51.100.3', 'listening-port=3478',
                     f'user=sofa:{secret}', 'allowed-peer-ip=198.51.100.2', 'denied-peer-ip=0.0.0.0-255.255.255.255',
                     'no-udp', 'no-tls', 'no-tcp-relay', 'total-quota=24'):
            self.assertIn(line+'\n', config)
        self.assertEqual(config.count('user='), 1)
        self.assertEqual(config.count('allowed-peer-ip='), 1)
        for name in ('password', 'turnserver.conf', 'ice.json'):
            self.assertEqual(stat.S_IMODE((turn/name).stat().st_mode), 0o600)

    def test_password_is_made_once_and_kept(self):
        turn = self.root/'turn'
        start.relay_files(RELAY, turn)
        first = (turn/'password').read_text()
        self.assertGreaterEqual(len(first.strip()), 48)
        start.relay_files(RELAY, turn)
        self.assertEqual((turn/'password').read_text(), first)
        start.relay_files(dict(RELAY, SOFA_TURN_NEW_PASSWORD='1'), turn)
        self.assertNotEqual((turn/'password').read_text(), first)

    def test_lan_relay_and_other_ports(self):
        env = dict(RELAY, SOFA_TURN_PUBLIC_PORT='443', SOFA_TURN_LAN_HOST='192.0.2.10', SOFA_TURN_PORT='13478')
        servers = json.loads(start.relay_files(env, self.root/'turn').read_text())
        self.assertEqual(servers[0]['urls'], ['turns:turn.example.org:443?transport=tcp',
                                              'turn:192.0.2.10:13478?transport=tcp'])

    def test_refuses_bad_relay_settings(self):
        for change in (dict(SOFA_TURN_PUBLIC_HOST='bad name'), dict(SOFA_TURN_PUBLIC_HOST='turn.example.org\nno-auth'),
                       dict(SOFA_TURN_PUBLIC_PORT='0'), dict(SOFA_TURN_PUBLIC_PORT='5349x'),
                       dict(SOFA_HOST_IP=''), dict(SOFA_RELAY_IP='198.51.100.0/24'), dict(SOFA_TURN_LAN_HOST='1.2.3')):
            with self.subTest(change=change), self.assertRaises(start.Refused):
                start.relay_files(dict(RELAY, **change), self.root/'turn')

    def test_relay_runs_but_is_not_offered_without_a_name(self):
        env = dict(RELAY, SOFA_TURN_PUBLIC_HOST='')
        self.assertIsNone(start.relay_files(env, self.root/'turn'))
        self.assertIn('allowed-peer-ip=198.51.100.2\n', (self.root/'turn/turnserver.conf').read_text())
        self.assertEqual(json.loads((self.root/'turn/ice.json').read_text()), [])
        lan = json.loads(start.relay_files(dict(env, SOFA_TURN_LAN_HOST='192.0.2.10'), self.root/'turn').read_text())
        self.assertEqual(lan[0]['urls'], ['turn:192.0.2.10:3478?transport=tcp'])

    def test_command_keeps_the_pins_off_the_command_line(self):
        env = dict(SOFA_PIN='long-enough', SOFA_ADMIN_PIN='admin-pin', SOFA_TRUSTED_PROXY='192.0.2.1',
                   SOFA_BOOT_SCREEN='no')
        args = start.command(env, Path('/turn/ice.json'))
        self.assertNotIn('long-enough', args)
        self.assertNotIn('admin-pin', args)
        self.assertEqual(args[args.index('--trusted-proxy')+1], '192.0.2.1')
        self.assertEqual(args[args.index('--ice-config')+1], '/turn/ice.json')
        self.assertIn('--no-boot-screen', args)
        self.assertEqual(args[args.index('--covers')+1], '/data/covers')
        self.assertNotIn('--trusted-proxy', start.command({}, None))
        self.assertNotIn('--ice-config', start.command({}, None))

    def test_main_refuses_without_starting(self):
        env = dict(SOFA_PIN='long-enough', SOFA_ADMIN_PIN='admin-pin', SOFA_LIBRARY=str(self.root/'missing'))
        self.assertEqual(start.main(env), 2)
        self.assertEqual(start.main({}), 2)


class EnvironmentPinTests(unittest.TestCase):
    def test_server_reads_the_room_code_from_the_environment(self):
        env = dict(os.environ, SOFA_PIN='short')
        result = subprocess.run([sys.executable, str(ROOT/'server.py')], env=env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn('at least six characters', result.stderr)


if __name__ == '__main__':
    unittest.main()
