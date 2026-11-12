import asyncio
from pathlib import Path
import tempfile
import threading
from unittest import IsolatedAsyncioTestCase, TestCase

from aiohttp.test_utils import TestClient, TestServer
from aiortc.codecs.opus import OpusEncoder

from fetch_core import core_filename
from inputs import InputHub
from library import Game, Library
from media import GameAudio
from server import Room, make_app
from session import Launcher, Session

from test_library import Host


class FakeEmulator:
    """Stands in for a libretro core: records start and stop, and can fail or hang on start."""
    def __init__(self, log, title, rate=44100, fail=False, gate=None):
        self.log, self.title, self.sample_rate, self.fail, self.gate = log, title, rate, fail, gate
        self.thread = None
        self.frame_at = None

    def start(self):
        self.log.append(('start', self.title))
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError('No BIOS')

    def stop(self):
        self.log.append(('stop', self.title))

    def status(self):
        return dict(core='Fake', error=None)

    def audio_queued(self):
        return 0  # the track waits out its stall each chunk, as with a paused core

    def audio_frame(self, count):
        return b'\x10\x00\x20\x00'*count


class FakeLauncher(Launcher):
    def __init__(self, log, **kwargs):
        super().__init__('system', 'saves', 'cores')
        self.log, self.kwargs, self.built = log, kwargs, []

    def plan(self, game):
        return None, 2 if game.console == 'ps2' else 4, None

    def emulator(self, game, hub, boot_screen=None):
        self.built.append(hub.players)
        self.boot = boot_screen
        return FakeEmulator(self.log, game.title, **self.kwargs.get(game.title, {}))


def library(host):
    host.core('pcsx_rearmed')
    host.core('play')
    host.file('ps1/Canyon Racer.cue')
    host.file('ps2/Demo.elf')
    return Library(host.library, host.system, host.cores, 'Linux').scan()


def ids(library):
    return {game.title: game.id for game in library.games.values()}


class SwitchTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = library(Host(self.temporary.name))
        self.ids = ids(self.library)
        self.log = []

    async def room(self, admin_pin=None, **kwargs):
        self.launcher = FakeLauncher(self.log, **kwargs)
        room = Room(Session(InputHub(4)), 'qa-canyon', library=self.library, launcher=self.launcher,
                    admin_pin=admin_pin)
        client = TestClient(TestServer(make_app(room)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return room, client

    async def join(self, client, name='Ann'):
        joined = await (await client.post('/api/join', json=dict(pin='qa-canyon', name=name))).json()
        return joined['token'], dict(Authorization='Bearer '+joined['token'])

    async def test_pick_switch_and_stop_keep_players_and_their_slots(self):
        room, client = await self.room()
        ann, auth = await self.join(client)
        bob, _ = await self.join(client, 'Bob')
        room.hub.claim(ann, 0)
        room.hub.claim(bob, 3)
        response = await client.post('/api/game', json=dict(id=self.ids['Canyon Racer']), headers=auth)
        self.assertEqual(response.status, 200)
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertEqual(state['host']['game'], dict(title='Canyon Racer', id=self.ids['Canyon Racer'], runningS=0))
        self.assertTrue(state['canPick'])

        # PS2 reads two pads: Bob's slot 4 goes, Ann keeps slot 1.
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)).status, 200)
        self.assertEqual(self.log, [('start', 'Canyon Racer'), ('stop', 'Canyon Racer'), ('start', 'Demo')])
        self.assertEqual(room.hub.slots(), [ann, None])
        self.assertEqual(self.launcher.built, [4, 2], 'the hub is resized before the emulator is built')

        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Canyon Racer']), headers=auth)).status, 200)
        self.assertEqual(room.hub.slots(), [ann, None, None, None])
        self.assertEqual((await client.post('/api/game/stop', headers=auth)).status, 200)
        self.assertEqual(self.log[-1], ('stop', 'Canyon Racer'))
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertIsNone(state['host']['game'])
        self.assertEqual(len(room.clients), 2, 'nobody was dropped')

    async def test_a_game_that_fails_leaves_the_room_idle_with_the_error(self):
        room, client = await self.room(**{'Demo': dict(fail=True)})
        _, auth = await self.join(client)
        await client.post('/api/game', json=dict(id=self.ids['Canyon Racer']), headers=auth)
        response = await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)
        self.assertEqual(response.status, 500)
        self.assertIn('No BIOS', await response.text())
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertIsNone(state['host']['game'])
        self.assertIn('Demo could not start', state['host']['error'])
        self.assertEqual(self.log[-1], ('stop', 'Demo'), 'a failed start is cleaned up')

    async def test_a_second_pick_while_a_game_starts_is_refused(self):
        gate = threading.Event()
        room, client = await self.room(**{'Canyon Racer': dict(gate=gate)})
        _, auth = await self.join(client)
        first = asyncio.create_task(client.post('/api/game', json=dict(id=self.ids['Canyon Racer']), headers=auth))
        while not self.log:
            await asyncio.sleep(.01)
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertEqual(state['host']['loading'], 'Canyon Racer')
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)).status, 409)
        self.assertEqual((await client.post('/api/game/stop', headers=auth)).status, 409)
        gate.set()
        self.assertEqual((await first).status, 200)

    async def test_a_pick_can_skip_the_boot_screen(self):
        room, client = await self.room()
        _, auth = await self.join(client)
        body = await (await client.get('/api/library', headers=auth)).json()
        self.assertTrue(body['bootScreen'], 'on by default')
        await client.post('/api/game', json=dict(id=self.ids['Demo'], bootScreen=False), headers=auth)
        self.assertIs(self.launcher.boot, False)
        await client.post('/api/game', json=dict(id=self.ids['Demo'], bootScreen='yes'), headers=auth)
        self.assertIsNone(self.launcher.boot, 'anything else means the host default')

    async def test_the_fallback_stream_announces_the_rate_it_sends(self):
        room, client = await self.room()
        token, auth = await self.join(client)
        await client.post('/api/game', json=dict(id=self.ids['Canyon Racer']), headers=auth)  # a 44.1 kHz game
        async with client.ws_connect('/api/socket') as ws:
            await ws.send_json(dict(token=token))
            ready = await ws.receive_json()
            self.assertEqual(ready['sampleRate'], room.audio.rate)
            self.assertNotEqual(ready['sampleRate'], room.session.sample_rate, 'the room started idle')

    async def test_unknown_game_and_no_library(self):
        room, client = await self.room()
        _, auth = await self.join(client)
        self.assertEqual((await client.post('/api/game', json=dict(id='nope'), headers=auth)).status, 404)
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']))).status, 401)
        room.library = None
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)).status, 404)
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertFalse(state['canPick'])

    async def test_admin_pin_gates_picking(self):
        room, client = await self.room(admin_pin='4321')
        _, auth = await self.join(client)
        _, other = await self.join(client, 'Bob')
        state = await (await client.get('/api/state', headers=auth)).json()
        self.assertEqual((state['canPick'], state['adminPin']), (False, True))
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)).status, 403)
        self.assertEqual((await client.post('/api/admin', json=dict(pin='0000'), headers=auth)).status, 403)
        self.assertEqual((await client.post('/api/admin', json=dict(pin='4321'), headers=auth)).status, 200)
        self.assertTrue((await (await client.get('/api/state', headers=auth)).json())['canPick'])
        self.assertEqual((await client.post('/api/game', json=dict(id=self.ids['Demo']), headers=auth)).status, 200)
        self.assertEqual((await client.post('/api/game/stop', headers=other)).status, 403, 'unlocks one player only')

    async def test_admin_pin_guesses_are_rate_limited(self):
        room, client = await self.room(admin_pin='4321')
        _, auth = await self.join(client)
        for _ in range(12):
            await client.post('/api/admin', json=dict(pin='0000'), headers=auth)
        self.assertEqual((await client.post('/api/admin', json=dict(pin='4321'), headers=auth)).status, 429)


class HubResizeTests(TestCase):
    def test_players_keep_slots_that_still_exist(self):
        hub = InputHub(4)
        hub.claim('ann', 1)
        hub.claim('bob', 3)
        hub.update('bob', 1, 5)
        hub.resize(2)
        self.assertEqual(hub.slots(), [None, 'ann'])
        self.assertEqual(len(hub.snapshot()), 2)
        self.assertFalse(hub.update('bob', 2, 5), 'a player without a slot sends no input')
        hub.resize(4)
        self.assertEqual(hub.slots(), [None, 'ann', None, None])
        hub.claim('bob', 3)
        hub.polled(3, 1)


class LauncherTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cores = Path(self.temporary.name)

    def game(self, console, profile, title='Demo'):
        return Game('0123456789abcdef', title, console, profile, Path('game'))

    def test_ps1_has_four_players_on_the_multitap(self):
        core, players, multitap = Launcher('s', 'saves', self.cores).plan(self.game('ps1', 'ps1'))
        self.assertEqual((core.name, players, multitap), (core_filename(core='pcsx_rearmed'), 4, 'port 1'))
        self.assertEqual(Launcher('s', 'saves', self.cores, 2).plan(self.game('ps1', 'ps1'))[1:], (2, 'disabled'))

    def test_ps2_reads_four_pads_only_with_the_multitap_core(self):
        launcher = Launcher('s', 'saves', self.cores, 4)
        self.assertEqual(launcher.plan(self.game('ps2', 'ps2'))[1:], (2, 'disabled'))
        (self.cores/core_filename(core='pcsx2_multitap')).write_bytes(b'')
        core, players, multitap = launcher.plan(self.game('ps2', 'ps2'))
        self.assertEqual((core.name, players, multitap), (core_filename(core='pcsx2_multitap'), 4, 'port 1'))
        self.assertEqual(launcher.plan(self.game('ps2', 'ps2-play'))[1:], (2, None))

    def test_each_game_saves_in_its_own_folder_and_ps2_cards_follow(self):
        launcher = Launcher('s', 'saves', self.cores)
        self.assertEqual(launcher.save_folder(self.game('ps2', 'ps2')), Path('saves/ps2/Demo [01234567]'))
        emulator = launcher.emulator(self.game('ps2', 'ps2'), InputHub(2))
        self.assertEqual(emulator.variables['pcsx2_shared_memory_cards'], b'disabled')
        self.assertEqual(emulator.saves.name, 'Demo [01234567]')


class BootScreenTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.system = Path(self.temporary.name)

    def options(self, profile, console, **kwargs):
        launcher = Launcher(self.system, self.system/'saves', self.system, **kwargs)
        game = Game('0123456789abcdef', 'Demo', console, profile, Path('game'))
        return launcher.emulator(game, InputHub(2)).variables

    def test_ps1_shows_the_bios_licence_screen_only_with_a_real_bios(self):
        self.assertEqual(self.options('ps1', 'ps1').get('pcsx_rearmed_show_bios_bootlogo'), None)
        (self.system/'scph1001.bin').write_bytes(b'')
        self.assertEqual(self.options('ps1', 'ps1')['pcsx_rearmed_show_bios_bootlogo'], b'enabled_no_pcsx')

    def test_ps2_turns_fast_boot_off_unless_unticked(self):
        self.assertEqual(self.options('ps2', 'ps2')['pcsx2_fastboot'], b'disabled')
        self.assertEqual(self.options('ps2', 'ps2', boot_screen=False)['pcsx2_fastboot'], b'enabled')
        launcher = Launcher(self.system, self.system, self.system)
        game = Game('0123456789abcdef', 'Demo', 'ps2', 'ps2', Path('game'))
        self.assertEqual(launcher.emulator(game, InputHub(2), False).variables['pcsx2_fastboot'], b'enabled')
        self.assertEqual(Launcher(self.system, self.system, self.system, boot_screen=False)
                         .emulator(game, InputHub(2), True).variables['pcsx2_fastboot'], b'disabled')

    def test_host_core_options_still_win(self):
        variables = self.options('ps2', 'ps2', options=('pcsx2_fastboot=enabled',))
        self.assertEqual(variables['pcsx2_fastboot'], b'enabled')


class AudioRateSwitchTests(IsolatedAsyncioTestCase):
    async def test_the_track_keeps_its_rate_when_the_next_game_has_another(self):
        source = FakeEmulator([], 'Demo', 44100)
        audio, encoder = GameAudio(source, stall=.001), OpusEncoder()
        for _ in range(3):
            encoder.encode(await audio.recv())
        source.sample_rate = 48000
        for _ in range(20):
            frame = await audio.recv()
            self.assertEqual((frame.sample_rate, frame.samples), (44100, 882))
            encoder.encode(frame)
        self.assertTrue(any(bytes(frame.planes[0])))
