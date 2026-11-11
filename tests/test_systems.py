from pathlib import Path
import tempfile
import unittest

from emulator import Emulator
from inputs import InputHub
from systems import PROFILES


class ProfileTests(unittest.TestCase):
    def test_ps1_keeps_its_core_options_and_multitap(self):
        options = PROFILES['ps1'].options('port 2')
        self.assertEqual(options['pcsx_rearmed_multitap'], b'port 2')
        self.assertEqual(options['pcsx_rearmed_bios'], b'auto')
        self.assertEqual(PROFILES['ps1'].options()['pcsx_rearmed_multitap'], b'port 1')

    def test_ps2_defaults_to_the_cpu_renderer_and_accepts_overrides(self):
        profile = PROFILES['ps2']
        self.assertEqual(profile.options()['pcsx2_renderer'], b'Software (SW)')
        options = profile.options(None, ['pcsx2_renderer=OpenGL', 'pcsx2_fastboot=disabled'])
        self.assertEqual(options['pcsx2_renderer'], b'OpenGL')
        self.assertEqual(options['pcsx2_fastboot'], b'disabled')
        self.assertNotIn('pcsx_rearmed_multitap', options)
        self.assertEqual(options['pcsx2_multitap'], b'disabled', 'the multitap is off for two players')
        self.assertEqual(profile.options('port 1')['pcsx2_multitap'], b'port 1')
        self.assertEqual((profile.default_players, profile.max_players), (2, 4))

    def test_missing_bios_is_reported_until_a_dump_is_present(self):
        with tempfile.TemporaryDirectory() as system:
            profile = PROFILES['ps2']
            self.assertIn('pcsx2/bios', profile.missing_bios(system))
            bios = Path(system)/'pcsx2/bios'
            bios.mkdir(parents=True)
            (bios/'.DS_Store').write_bytes(b'')
            self.assertIsNotNone(profile.missing_bios(system))
            (bios/'console.bin').write_bytes(b'\0' * 16)
            self.assertIsNone(profile.missing_bios(system))
            self.assertIsNone(PROFILES['ps2-play'].missing_bios(system))

    def test_emulator_refuses_a_required_bios_before_loading_the_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('core.so', 'game.elf'):
                (root/name).write_bytes(b'')
            emulator = Emulator(root/'core.so', root/'game.elf', root/'system', root/'saves', InputHub(2),
                                profile=PROFILES['ps2'])
            with self.assertRaisesRegex(RuntimeError, 'No PS2 BIOS'):
                emulator._load()

    def test_emulator_names_missing_cue_tracks_before_loading_the_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'core.so').write_bytes(b'')
            (root/'game.cue').write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
            (root/'game.ape').write_bytes(b'')
            emulator = Emulator(root/'core.so', root/'game.cue', root/'system', root/'saves', InputHub(2))
            with self.assertRaisesRegex(RuntimeError, 'names 1 track file that is not there. Its folder has .ape'):
                emulator._load()

    def test_controller_info_limits_the_ports_and_the_players(self):
        import ctypes as C
        from emulator import ControllerInfo
        emulator = Emulator('core.so', 'game.elf', 'system', 'saves', InputHub(4), profile=PROFILES['ps2'])
        device = C.c_int(1)
        table = (ControllerInfo * 3)(ControllerInfo(C.addressof(device), 1), ControllerInfo(C.addressof(device), 1))
        self.assertTrue(emulator._environment(35, C.addressof(table)))
        self.assertEqual(emulator.ports, 2)

    def test_status_names_the_console(self):
        emulator = Emulator('core.so', 'game.elf', 'system', 'saves', InputHub(2), profile=PROFILES['ps2'])
        status = emulator.status()
        self.assertEqual((status['system'], status['console'], status['multitap']), ('ps2', 'PS2', 'none'))
        self.assertEqual(status['renderer'], 'software')


if __name__ == '__main__':
    unittest.main()
