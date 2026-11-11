import json
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase, TestCase

from aiohttp.test_utils import TestClient, TestServer

from fetch_core import core_filename
from inputs import InputHub
from library import Library, game_id, missing_tracks
from server import Room, make_app
from session import Session


class Host:
    """A temporary library, system and cores folder. Every file is a tiny stand-in."""
    def __init__(self, root):
        self.root = Path(root)
        self.library, self.system, self.cores = self.root/'games', self.root/'system', self.root/'cores'
        for folder in (self.library, self.system, self.cores):
            folder.mkdir()

    def file(self, relative, text=''):
        path = self.library/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def core(self, name, system='Linux'):
        (self.cores/core_filename(system, name)).write_bytes(b'')

    def bios(self, relative):
        path = self.system/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\0'*16)

    def scan(self, system='Linux'):
        return Library(self.library, self.system, self.cores, system).scan()


def console(library, key):
    return next(item for item in library.consoles if item['id'] == key)


class ScanTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.host = Host(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_groups_games_by_console_folder_and_lists_cue_not_its_tracks(self):
        self.host.core('pcsx_rearmed')
        self.host.file('ps1/Canyon Racer/GAME.cue', 'FILE "GAME (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n'
                       'FILE "GAME (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n')
        self.host.file('ps1/Canyon Racer/GAME (Track 1).bin')
        self.host.file('ps1/Canyon Racer/GAME (Track 2).bin')
        self.host.file('ps1/Saga.m3u', 'Saga (Disc 1).chd\n# comment\nSaga (Disc 2).chd\n')
        self.host.file('ps1/Saga (Disc 1).chd')
        self.host.file('ps1/Saga (Disc 2).chd')
        self.host.file('ps1/Puzzle.pbp')
        self.host.file('ps1/notes.txt')
        self.host.file('ps1/.trash/Old.cue')
        library = self.host.scan()
        self.assertEqual([item['title'] for item in console(library, 'ps1')['games']], ['Canyon Racer', 'Puzzle', 'Saga'])
        game = library.games[game_id('ps1', Path('Canyon Racer/GAME.cue'))]
        self.assertEqual((game.console, game.profile, game.path.name), ('ps1', 'ps1', 'GAME.cue'))
        self.assertEqual([item['id'] for item in library.consoles], ['ps1'], 'a console without a folder is not listed')

    def test_ids_are_stable_across_scans(self):
        self.host.core('pcsx_rearmed')
        self.host.file('ps1/Puzzle.pbp')
        self.assertEqual(list(self.host.scan().games), list(self.host.scan().games))

    def test_ps1_without_a_bios_runs_on_hle_with_a_warning(self):
        self.host.core('pcsx_rearmed')
        self.host.file('ps1/Puzzle.pbp')
        ps1 = console(self.host.scan(), 'ps1')
        self.assertTrue(ps1['available'])
        self.assertIn('No PS1 BIOS', ps1['warning'])
        self.host.bios('scph1001.bin')
        self.assertIsNone(console(self.host.scan(), 'ps1')['warning'])

    def test_ps2_without_a_bios_or_play_is_hidden_with_a_warning(self):
        self.host.core('pcsx2')
        self.host.file('ps2/Demo.elf')
        ps2 = console(self.host.scan(), 'ps2')
        self.assertFalse(ps2['available'])
        self.assertEqual(ps2['games'], [])
        self.assertIn('No PS2 BIOS', ps2['warning'])

    def test_ps2_prefers_pcsx2_with_its_bios_else_play(self):
        self.host.core('pcsx2')
        self.host.core('play')
        self.host.file('ps2/Demo.elf')
        library = self.host.scan()
        self.assertEqual({game.profile for game in library.games.values()}, {'ps2-play'})
        self.assertIn('No PS2 BIOS', console(library, 'ps2')['warning'])
        self.host.bios('pcsx2/bios/console.bin')
        library = self.host.scan()
        self.assertEqual({game.profile for game in library.games.values()}, {'ps2'})
        self.assertIsNone(console(library, 'ps2')['warning'])

    def test_a_core_for_another_platform_is_hidden(self):
        self.host.core('play', 'Darwin')
        self.host.file('ps2/Demo.elf')
        ps2 = console(self.host.scan('Darwin'), 'ps2')
        self.assertFalse(ps2['available'], 'Play! is Linux only and PCSX2 is missing')
        self.assertIn('pcsx2', ps2['warning'])

    def test_missing_core_is_a_warning(self):
        self.host.file('ps1/Puzzle.pbp')
        ps1 = console(self.host.scan(), 'ps1')
        self.assertFalse(ps1['available'])
        self.assertIn('setup.sh --console ps1', ps1['warning'])

    def test_browsers_see_no_paths_or_file_names(self):
        self.host.core('pcsx2')
        self.host.core('pcsx_rearmed')
        self.host.file('ps1/Canyon Racer/GAME.cue')
        self.host.file('ps2/Demo.elf')
        library = self.host.scan()
        text = json.dumps(library.public())
        for secret in (self.temporary.name, 'GAME.cue', '.elf', 'system'):
            self.assertNotIn(secret, text)
        self.assertIn(str(self.host.system), library.warnings['ps2'], 'the terminal still gets the folder')

    def test_missing_tracks_are_named_by_count_and_kind_not_by_file(self):
        cue = self.host.file('ps1/Fighter/Fighter.cue', 'FILE "Fighter (Track 01).bin" BINARY\r\n  TRACK 01 MODE2/2352\r\n'
                             'FILE "Fighter (Track 02).bin" BINARY\r\n  TRACK 02 AUDIO\r\n')
        self.host.file('ps1/Fighter/Fighter (Track 01).7z')
        self.host.file('ps1/Fighter/Track 01.bin.ecm')
        self.host.file('ps1/Fighter/Fighter (Track 02).ape')
        self.host.file('ps1/Fighter/.DS_Store')
        text = missing_tracks(cue)
        self.assertIn('names 2 track files that are not there', text)
        self.assertIn('Its folder has .7z, .ape, .ecm files instead', text)
        self.assertNotIn('Fighter', text)
        self.host.file('ps1/Fighter/Fighter (Track 01).bin')
        self.assertIn('names 1 track file that is not there.', missing_tracks(cue))
        self.host.file('ps1/Fighter/Fighter (Track 02).bin')
        self.assertIsNone(missing_tracks(cue))
        saga = self.host.file('ps1/Saga.m3u', 'Saga (Disc 1).chd\nSaga (Disc 2).chd\n')
        self.host.file('ps1/Saga (Disc 1).chd')
        self.assertTrue(missing_tracks(saga).startswith('The M3U names 1 disc file that is not there.'))
        self.assertIsNone(missing_tracks(self.host.file('ps1/Puzzle.pbp')))
        self.host.core('pcsx_rearmed')
        titles = [item['title'] for item in console(self.host.scan(), 'ps1')['games']]
        self.assertEqual(titles, ['Fighter', 'Puzzle', 'Saga'], 'the library still lists it')


class LibraryApiTests(IsolatedAsyncioTestCase):
    async def test_library_needs_a_joined_player(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = Host(temporary)
            host.core('pcsx_rearmed')
            host.file('ps1/Puzzle.pbp')
            room = Room(Session(InputHub(4)), 'qa-canyon',
                        library=Library(host.library, host.system, host.cores, 'Linux'))
            async with TestClient(TestServer(make_app(room))) as client:
                self.assertEqual((await client.get('/api/library')).status, 401)
                joined = await (await client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))).json()
                response = await client.get('/api/library', headers=dict(Authorization='Bearer '+joined['token']))
                body = await response.json()
                self.assertEqual([game['title'] for game in body['consoles'][0]['games']], ['Puzzle'])
                host.file('ps1/Later.pbp')
                room.scanned_at -= 11  # a later visit rescans
                body = await (await client.get('/api/library', headers=dict(Authorization='Bearer '+joined['token']))).json()
                self.assertEqual(len(body['consoles'][0]['games']), 2)

    async def test_no_library_is_an_empty_list(self):
        room = Room(Session(InputHub(4)), 'qa-canyon')
        async with TestClient(TestServer(make_app(room))) as client:
            joined = await (await client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))).json()
            body = await (await client.get('/api/library', headers=dict(Authorization='Bearer '+joined['token']))).json()
            self.assertEqual(body, dict(consoles=[]))

    def test_boot_screen_is_offered_where_this_host_can_show_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = Host(temporary)
            host.core('pcsx_rearmed')
            host.core('pcsx2')
            host.core('play')
            host.file('ps1/Puzzle.pbp')
            host.file('ps2/Demo.elf')
            library = host.scan()
            self.assertFalse(console(library, 'ps1')['bootScreen'], 'the HLE BIOS has no boot screen')
            self.assertFalse(console(library, 'ps2')['bootScreen'], 'Play! has none')
            host.bios('scph1001.bin')
            host.bios('pcsx2/bios/console.bin')
            library = host.scan()
            self.assertTrue(console(library, 'ps1')['bootScreen'])
            self.assertTrue(console(library, 'ps2')['bootScreen'])
