"""Online cover art and details. The network is mocked: nothing here leaves the machine."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from unittest import IsolatedAsyncioTestCase, TestCase
from urllib.parse import unquote

from aiohttp.test_utils import TestClient, TestServer

from covers import Covers
from inputs import InputHub
from library import Game, Library
import metadata
from metadata import Fetcher, Libretro, ProviderError, ScreenScraper, libretro_name, serial
from server import Room, make_app
from session import Session

from test_covers import image
from test_library import Host

DEV = dict(SOFA_SCREENSCRAPER_DEVID='dev-id', SOFA_SCREENSCRAPER_DEVPASSWORD='dev-secret',
           SOFA_SCREENSCRAPER_USER='player', SOFA_SCREENSCRAPER_PASSWORD='user-secret')


class FakeHttp:
    """Answers by the first matching rule: (text in the URL, status, body). Everything else is a 404."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.requests = []
        self.closed = False

    async def get(self, url, params=None, limit=metadata.MAX_DOWNLOAD):
        self.requests.append((url, dict(params or {})))
        for text, status, body in self.rules:
            if text in unquote(url):
                if isinstance(status, Exception):
                    raise status
                return status, body
        return 404, b''

    async def close(self):
        self.closed = True


def game(title='Puzzle Quest (Europe) [SLES-12345]', console='ps1', suffix='.cue'):
    return Game('0123456789abcdef', title, console, console, Path('/games')/console/title/('disc'+suffix))


def jeu(**extra):
    return dict(
        synopsis=[dict(langue='fr', text='Un jeu.'), dict(langue='en', text='A  puzzle\ngame.')],
        dates=[dict(region='jp', text='1999-03-01'), dict(region='us', text='1998-11-20')],
        genres=[dict(noms=[dict(langue='en', text='Puzzle')]), dict(noms=[dict(langue='en', text='Action')]),
                dict(noms=[dict(langue='en', text='Arcade')])],
        joueurs=dict(text='1-2'),
        medias=[dict(type='ss', region='wor', url='https://neoclone.screenscraper.fr/ss.png', format='png'),
                dict(type='box-2D', region='jp', url='https://neoclone.screenscraper.fr/box-jp.png', format='png'),
                dict(type='box-2D', region='us', url='https://neoclone.screenscraper.fr/box-us.png', format='png')],
        **extra)


def answer(game=None, **user):
    return json.dumps(dict(response=dict(jeu=game or jeu(), ssuser=dict(
        dict(maxthreads='1', maxrequestspermin='30', requeststoday='5', maxrequestsperday='20000'), **user)))).encode()


def quick(provider):
    provider.interval = 0
    provider.pace = lambda: asyncio.sleep(0)
    return provider


class NameTests(TestCase):
    def test_libretro_names_replace_the_characters_its_files_cannot_hold(self):
        self.assertEqual(libretro_name('A&B: C*D/E<F>G?H\\I|J`K'), 'A_B_ C_D_E_F_G_H_I_J_K')
        self.assertEqual(libretro_name('Plain Name (USA) (Rev 1)'), 'Plain Name (USA) (Rev 1)')

    def test_serial_from_the_name_tags(self):
        self.assertEqual(serial(game('Name (USA) [SLUS-00001]')), 'SLUS-00001')
        self.assertEqual(serial(game('Name (SCES_123.45)')), 'SCES-12345')
        self.assertIsNone(serial(game('Name (USA)')))
        self.assertIsNone(serial(game('ABCD 12345')))  # only inside tags

    def test_providers_are_off_unless_named(self):
        http = FakeHttp()
        self.assertEqual(metadata.providers(None, http, {}), ([], []))
        self.assertEqual(metadata.providers('off', http, DEV), ([], []))
        chosen, warnings = metadata.providers('libretro, screenscraper, other', http, {})
        self.assertEqual([provider.name for provider in chosen], ['libretro'])
        self.assertEqual(len(warnings), 2)
        self.assertIn('SOFA_SCREENSCRAPER_DEVID', warnings[0])
        chosen, _ = metadata.providers('screenscraper,libretro', http, DEV)
        self.assertEqual([provider.name for provider in chosen], ['screenscraper', 'libretro'])


class LibretroTests(IsolatedAsyncioTestCase):
    async def test_box_art_by_the_original_name(self):
        http = FakeHttp(('Named_Boxarts/Puzzle Quest (Europe) [SLES-12345].png', 200, b'png'))
        found = await quick(Libretro(http)).lookup(game())
        self.assertEqual(found.art, b'png')
        url = http.requests[0][0]
        self.assertTrue(url.startswith('https://raw.githubusercontent.com/libretro-thumbnails/Sony_-_PlayStation/'
                                       'master/Named_Boxarts/'))
        self.assertIn('Puzzle%20Quest%20%28Europe%29', url)

    async def test_falls_back_to_title_screens_then_snaps(self):
        http = FakeHttp(('Named_Snaps/Puzzle Quest (Europe).png', 200, b'snap'))
        found = await quick(Libretro(http)).lookup(game())
        self.assertEqual(found.art, b'snap')
        kinds = [url.split('/')[6] for url, _ in http.requests]
        self.assertEqual(kinds, ['Named_Boxarts']*2 + ['Named_Titles']*2 + ['Named_Snaps']*2)

    async def test_ps2_repository_sanitised_names_and_nothing_found(self):
        http = FakeHttp()
        self.assertIsNone(await quick(Libretro(http)).lookup(game('Fight: Club (USA)', 'ps2')))
        self.assertTrue(all('/Sony_-_PlayStation_2/' in url and 'Fight_%20Club' in url for url, _ in http.requests))
        self.assertTrue(all(params == {} for _, params in http.requests))
        self.assertIsNone(await Libretro(FakeHttp()).lookup(game(console='other')))

    async def test_busy_server_is_an_error(self):
        with self.assertRaises(ProviderError):
            await quick(Libretro(FakeHttp(('', 503, b'')))).lookup(game())


class ScreenScraperTests(IsolatedAsyncioTestCase):
    def provider(self, *rules):
        self.http = FakeHttp(*rules)
        return quick(ScreenScraper.from_env(self.http, DEV))

    async def test_sends_only_the_name_serial_and_console(self):
        provider = self.provider(('jeuInfos.php', 200, answer()), ('box-us.png', 200, b'box'))
        found = await provider.lookup(game())
        url, params = self.http.requests[0]
        self.assertEqual(url, 'https://api.screenscraper.fr/api2/jeuInfos.php')
        self.assertEqual(params, dict(devid='dev-id', devpassword='dev-secret', softname='SofaLink', ssid='player',
                                      sspassword='user-secret', output='json', systemeid=57, romtype='rom',
                                      romnom='Puzzle Quest (Europe) [SLES-12345].cue', serialnum='SLES-12345'))
        self.assertEqual(found.art, b'box')
        self.assertEqual(self.http.requests[1][0], 'https://neoclone.screenscraper.fr/box-us.png')
        self.assertEqual(found.about, dict(description='A puzzle game.', year='1998', genre='Puzzle, Action',
                                           players='1-2'))

    async def test_follows_the_accounts_limits(self):
        provider = self.provider(('jeuInfos.php', 200, answer(maxrequestspermin='20')))
        await provider.lookup(game(), art=False)
        self.assertEqual(provider.interval, 3.0)
        self.assertEqual(len(self.http.requests), 1)
        provider = self.provider(('jeuInfos.php', 200, answer(requeststoday='19999')))
        await provider.lookup(game(), art=False)
        self.assertFalse(provider.available())
        self.assertGreater(provider.stopped[1], time.time())

    async def test_quota_and_credential_errors_stop_it(self):
        for status, until in ((430, True), (431, True), (403, False), (401, False), (426, False)):
            with self.subTest(status=status):
                provider = self.provider(('jeuInfos.php', status, b'Error'))
                with self.assertRaises(ProviderError) as caught:
                    await provider.lookup(game())
                self.assertTrue(caught.exception.stop)
                self.assertEqual(bool(caught.exception.until), until)
                self.assertNotIn('secret', str(caught.exception))

    async def test_too_many_requests_slows_down_then_gives_up(self):
        provider = self.provider(('jeuInfos.php', 429, b''))
        with self.assertRaises(ProviderError) as caught:
            await provider.lookup(game())
        self.assertFalse(caught.exception.stop)
        self.assertEqual(len(self.http.requests), 3)

    async def test_unknown_game_and_odd_answers(self):
        self.assertIsNone(await self.provider(('jeuInfos.php', 404, b'not found')).lookup(game()))
        with self.assertRaises(ProviderError):
            await self.provider(('jeuInfos.php', 200, b'<html>')).lookup(game())

    async def test_art_only_from_screenscraper_hosts(self):
        medias = [dict(type='box-2D', region='us', url='https://elsewhere.example/box.png'),
                  dict(type='box-2D', region='us', url='http://neoclone.screenscraper.fr/box.png')]
        provider = self.provider(('jeuInfos.php', 200, answer(dict(jeu(), medias=medias))))
        found = await provider.lookup(game())
        self.assertIsNone(found.art)
        self.assertEqual(len(self.http.requests), 1)

    def test_needs_developer_credentials(self):
        self.assertIsNone(ScreenScraper.from_env(FakeHttp(), dict(SOFA_SCREENSCRAPER_USER='player')))


class FetcherTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.covers = Covers(Path(self.temporary.name)/'covers')
        self.game = game()

    async def run_fetch(self, *providers):
        fetcher = Fetcher(self.covers, [quick(provider) for provider in providers], FakeHttp())
        self.assertEqual(fetcher.request([self.game]), 1)
        self.assertEqual(fetcher.status(self.game.id)['state'], 'queued')
        await fetcher.task
        self.assertIsNone(fetcher.status(self.game.id)['state'])
        return fetcher

    async def test_keeps_libretro_art_and_screenscraper_details(self):
        art = FakeHttp(('Named_Boxarts', 200, image(kind='PNG')))
        details = FakeHttp(('jeuInfos.php', 200, answer()))
        await self.run_fetch(Libretro(art), ScreenScraper.from_env(details, DEV))
        info = self.covers.info(self.game)
        self.assertEqual((info['uploaded'], info['fetched']), (False, True))
        self.assertEqual(self.covers.read(self.game)[:2], b'\xff\xd8')  # re-encoded as a JPEG
        record = self.covers.record(self.game.id)
        self.assertEqual(record['sources'], ['libretro', 'screenscraper'])
        self.assertEqual(record['about']['year'], '1998')
        self.assertIsNone(record['note'])
        # ScreenScraper was asked for details only: libretro already had the art.
        self.assertEqual([url for url, _ in details.requests], ['https://api.screenscraper.fr/api2/jeuInfos.php'])

    async def test_an_upload_always_wins_and_is_never_overwritten(self):
        uploaded = image(kind='JPEG', colour=(1, 2, 3))
        self.covers.save(self.game.id, uploaded)
        await self.run_fetch(Libretro(FakeHttp(('Named_Boxarts', 200, image(kind='PNG')))))
        self.assertEqual(self.covers.uploaded(self.game.id).read_bytes(), uploaded)
        self.assertEqual(self.covers.read(self.game), uploaded)
        self.assertTrue(self.covers.info(self.game)['uploaded'])
        self.covers.remove(self.game.id)
        self.assertTrue(self.covers.info(self.game)['fetched'])

    async def test_nothing_found_is_remembered_with_a_note(self):
        fetcher = await self.run_fetch(Libretro(FakeHttp()))
        self.assertIsNone(self.covers.info(self.game))
        self.assertEqual(self.covers.record(self.game.id)['note'], 'No art found')
        self.assertEqual(fetcher.missing([self.game]), [])  # "all missing" does not ask again

    async def test_failures_are_not_cached(self):
        fetcher = await self.run_fetch(Libretro(FakeHttp(('', ProviderError('Could not reach a host'), None))))
        self.assertIsNone(self.covers.record(self.game.id))
        self.assertEqual(fetcher.status(self.game.id)['note'], 'Could not reach a host')
        self.assertEqual(fetcher.missing([self.game]), [self.game])

    async def test_a_bad_image_keeps_the_placeholder(self):
        fetcher = await self.run_fetch(Libretro(FakeHttp(('Named_Boxarts', 200, b'<svg/>'))))
        self.assertIsNone(self.covers.info(self.game))
        self.assertIn('could not read', fetcher.status(self.game.id)['note'])

    async def test_a_stopped_provider_is_skipped(self):
        provider = ScreenScraper.from_env(FakeHttp(('jeuInfos.php', 430, b'')), DEV)
        fetcher = await self.run_fetch(provider)
        self.assertFalse(provider.available())
        fetcher.request([self.game])
        await fetcher.task
        self.assertEqual(len(provider.http.requests), 1)
        self.assertIn('quota', fetcher.status(self.game.id)['note'])


class FetchApiTests(IsolatedAsyncioTestCase):
    async def start(self, providers):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        host = Host(self.temporary.name)
        host.core('pcsx_rearmed')
        host.file('ps1/Puzzle Quest (Europe)/disc.cue')
        host.file('ps1/Racer/Racer.pbp')
        (host.library/'ps1/Racer/cover.jpg').write_bytes(image(kind='JPEG'))
        library = Library(host.library, host.system, host.cores, 'Linux')
        self.http = FakeHttp()
        self.room = Room(Session(InputHub(4)), 'qa-canyon', library=library, admin_pin='4321',
                         covers=Covers(host.root/'covers', host.library), providers=providers, http=self.http)
        self.client = TestClient(TestServer(make_app(self.room)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def join(self, admin=False):
        joined = await (await self.client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))).json()
        auth = dict(Authorization='Bearer '+joined['token'])
        if admin:
            self.assertEqual((await self.client.post('/api/admin', json=dict(pin='4321'), headers=auth)).status, 200)
        return auth

    async def games(self, auth):
        body = await (await self.client.get('/api/library', headers=auth)).json()
        return {game['title']: game for game in body['consoles'][0]['games']}, body

    async def test_off_by_default(self):
        await self.start(())
        auth = await self.join(admin=True)
        games, body = await self.games(auth)
        self.assertFalse(body['metadata'])
        self.assertNotIn('fetch', games['Puzzle Quest'])
        response = await self.client.post('/api/cover/fetch', json=dict(missing=True), headers=auth)
        self.assertEqual(response.status, 404)
        self.assertIn('SOFA_METADATA', await response.text())

    async def test_admin_fetches_in_the_background_and_players_see_the_details(self):
        art = FakeHttp(('Named_Boxarts/Puzzle Quest (Europe).png', 200, image(kind='PNG')))
        details = FakeHttp(('jeuInfos.php', 200, answer()))
        await self.start([quick(Libretro(art)), quick(ScreenScraper.from_env(details, DEV))])
        player, admin = await self.join(), await self.join(admin=True)
        self.assertEqual((await self.client.post('/api/cover/fetch', json=dict(missing=True), headers=player)).status,
                         403)
        response = await self.client.post('/api/cover/fetch', json=dict(missing=True), headers=admin)
        # Racer has a cover.jpg, so only the other game is missing.
        self.assertEqual(await response.json(), dict(ok=True, queued=1))
        await self.room.fetcher.task
        games, body = await self.games(player)
        puzzle = games['Puzzle Quest']
        self.assertTrue(body['metadata'])
        self.assertTrue(puzzle['cover']['fetched'])
        self.assertEqual(puzzle['about']['genre'], 'Puzzle, Action')
        self.assertEqual(puzzle['fetch'], dict(state=None, note=None, fetched=True))
        self.assertNotIn(self.temporary.name, str(body))
        image_response = await self.client.get('/api/cover?id='+puzzle['id'], headers=player)
        self.assertEqual(image_response.status, 200)
        # Asking again for all missing sends nothing: everything was looked up.
        response = await self.client.post('/api/cover/fetch', json=dict(missing=True), headers=admin)
        self.assertEqual((await response.json())['queued'], 0)
        # One game on request, even with art: ScreenScraper sees only the name and console.
        response = await self.client.post('/api/cover/fetch', json=dict(id=games['Racer']['id']), headers=admin)
        self.assertEqual((await response.json())['queued'], 1)
        await self.room.fetcher.task
        sent = [params for url, params in details.requests if url.endswith('jeuInfos.php')][-1]
        self.assertEqual((sent['romnom'], sent['systemeid']), ('Racer.pbp', 57))
        self.assertNotIn('Ann', str(details.requests + art.requests))
        self.assertEqual((await self.client.post('/api/cover/fetch', json=dict(id='0'*16), headers=admin)).status, 404)
        await self.client.close()
        self.assertTrue(self.http.closed)
