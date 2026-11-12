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
from metadata import Fetcher, Igdb, Libretro, ProviderError, ScreenScraper, libretro_name, loose_name, serial
from server import Room, make_app
from session import Session

from test_covers import image
from test_library import Host

DEV = dict(SOFA_SCREENSCRAPER_DEVID='dev-id', SOFA_SCREENSCRAPER_DEVPASSWORD='dev-secret',
           SOFA_SCREENSCRAPER_USER='player', SOFA_SCREENSCRAPER_PASSWORD='user-secret')
TWITCH = dict(SOFA_IGDB_CLIENT_ID='client-id', SOFA_IGDB_CLIENT_SECRET='client-secret')


class FakeHttp:
    """Answers by the first matching rule: (text in the URL, status, body). Everything else is a 404."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.requests = []
        self.posts = []
        self.closed = False

    async def get(self, url, params=None, limit=metadata.MAX_DOWNLOAD):
        self.requests.append((url, dict(params or {})))
        for text, status, body in self.rules:
            if text in unquote(url):
                if isinstance(status, Exception):
                    raise status
                return status, body
        return 404, b''

    async def post(self, url, data, headers=None, limit=metadata.MAX_DOWNLOAD):
        """Rules match the URL followed by the body, so a rule can name the search."""
        self.posts.append((url, data, dict(headers or {})))
        text = data.decode() if isinstance(data, bytes) else json.dumps(data)
        for match, status, body in self.rules:
            if match in url+' '+text:
                if isinstance(status, Exception):
                    raise status
                return (status(text) if callable(status) else status), body
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
        editeur=dict(id='1', text='  Made Up\nSoft '),
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
        chosen, warnings = metadata.providers('libretro,igdb', http, {})
        self.assertEqual(([provider.name for provider in chosen], warnings),
                         (['libretro'], ['IGDB needs SOFA_IGDB_CLIENT_ID and SOFA_IGDB_CLIENT_SECRET']))
        chosen, _ = metadata.providers('libretro,igdb', http, TWITCH)
        self.assertEqual([provider.name for provider in chosen], ['libretro', 'igdb'])


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
        kinds = [url.split('/')[6] for url, _ in http.requests if url.startswith(Libretro.BASE)]
        self.assertEqual(kinds, ['Named_Boxarts']*2 + ['Named_Titles']*2 + ['Named_Snaps']*2)

    async def test_ps2_repository_sanitised_names_and_nothing_found(self):
        http = FakeHttp()
        self.assertIsNone(await quick(Libretro(http)).lookup(game('Fight: Club (USA)', 'ps2')))
        raw = [url for url, _ in http.requests if url.startswith(Libretro.BASE)]
        self.assertTrue(raw and all('/Sony_-_PlayStation_2/' in url and 'Fight_%20Club' in url for url in raw))
        self.assertTrue(all('/Sony_-_PlayStation_2/' in url for url, _ in http.requests))
        self.assertTrue(all(params == {} for _, params in http.requests))
        self.assertIsNone(await Libretro(FakeHttp()).lookup(game(console='other')))

    async def test_busy_server_is_an_error(self):
        with self.assertRaises(ProviderError):
            await quick(Libretro(FakeHttp(('git/trees', 403, b'rate limited')))).lookup(game())
        with self.assertRaises(ProviderError):
            await quick(Libretro(FakeHttp(('', 503, b'')))).lookup(game())


def trees(*files, kind='Named_Boxarts'):
    """GitHub API answers for a repository with these image files in one folder."""
    root = dict(tree=[dict(path=name, type='tree', sha=name.lower()) for name in Libretro.KINDS]
                + [dict(path='.gitignore', type='blob', sha='ignore')])
    folder = dict(tree=[dict(path=name, type='blob', sha=str(n)) for n, name in enumerate(files)], truncated=False)
    return (('git/trees/master', 200, json.dumps(root).encode()),
            (f'git/trees/{kind.lower()}', 200, json.dumps(folder).encode()))


class LooseMatchTests(IsolatedAsyncioTestCase):
    def test_loose_names_ignore_languages_spacing_punctuation_and_tags(self):
        for ours, theirs in (('Canyon Racer (Europe) (EnFrDeEsIt)', 'Canyon Racer (Europe) (En,Fr,De,Es,It)'),
                             ('SkyBall Z - Hyper Cup (Europe, Australia) (En,Ja,Fr)',
                              'Sky Ball Z - Hyper Cup (Europe, Australia) (En,Fr)'),
                             ('Moon Saga - Idainaru Densetsu (Japan) [SLPS-00001]', 'Moon Saga - Idai naru Densetsu (Japan)'),
                             ('Cafe: Story (USA)', 'Café_ Story (USA)')):
            with self.subTest(ours=ours):
                self.assertEqual(loose_name(ours), loose_name(theirs))
        for ours, theirs in (('Canyon Racer (Europe)', 'Canyon Racer (USA)'),
                             ('Canyon Racer (Europe) (En,Fr)', 'Canyon Racer (Europe) (En,Fr) (Demo)'),
                             ('Canyon Racer (Europe) (Disc 1)', 'Canyon Racer (Europe) (Disc 2)'),
                             ('Canyon Racer 2 (Europe)', 'Canyon Racer (Europe)')):
            with self.subTest(ours=ours):
                self.assertNotEqual(loose_name(ours), loose_name(theirs))

    async def test_falls_back_to_the_file_list(self):
        theirs = 'Sky Ball Z - Hyper Cup (Europe, Australia) (En,Fr).png'
        http = FakeHttp(*trees('Sky Ball Z - Hyper Cup (USA).png', theirs, 'Other (Europe).png'),
                        ('Named_Boxarts/'+theirs, 200, b'box'), ('Named_Boxarts/Other (Europe).png', 200, b'other'))
        provider = quick(Libretro(http))
        found = await provider.lookup(game('SkyBall Z - Hyper Cup (Europe, Australia) (En,Ja,Fr)', 'ps2'))
        self.assertEqual(found.art, b'box')
        urls = [url for url, _ in http.requests]
        self.assertEqual(urls[-3:], [
            'https://api.github.com/repos/libretro-thumbnails/Sony_-_PlayStation_2/git/trees/master',
            'https://api.github.com/repos/libretro-thumbnails/Sony_-_PlayStation_2/git/trees/named_boxarts',
            'https://raw.githubusercontent.com/libretro-thumbnails/Sony_-_PlayStation_2/master/Named_Boxarts/'
            'Sky%20Ball%20Z%20-%20Hyper%20Cup%20%28Europe%2C%20Australia%29%20%28En%2CFr%29.png'])
        # The file list is kept: a second game costs no more list requests.
        self.assertEqual((await provider.lookup(game('Other (Europe) (EnFr)', 'ps2'))).art, b'other')
        self.assertEqual(sum('api.github.com' in url for url, _ in http.requests), 2)

    async def test_box_art_from_the_list_before_an_exact_title_screen(self):
        http = FakeHttp(*trees('Canyon Racer (Europe) (En,Fr).png'),
                        ('Named_Titles/Canyon Racer (Europe) (EnFr).png', 200, b'title'),
                        ('Named_Boxarts/Canyon Racer (Europe) (En,Fr).png', 200, b'box'))
        self.assertEqual((await quick(Libretro(http)).lookup(game('Canyon Racer (Europe) (EnFr)'))).art, b'box')

    async def test_another_region_is_not_a_match(self):
        http = FakeHttp(*trees('Canyon Racer (USA).png'))
        self.assertIsNone(await quick(Libretro(http)).lookup(game('Canyon Racer (Europe)')))
        self.assertFalse(any('Racer%20%28USA' in url for url, _ in http.requests))


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
                                           players='1-2', publisher='Made Up Soft'))

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


TOKEN = ('id.twitch.tv', 200, json.dumps(dict(access_token='the-token', expires_in=5_000_000, token_type='bearer')).encode())


def entry(id, name, **extra):
    """One IGDB game as its API returns it, with every field the host asks for."""
    return dict(dict(
        id=id, name=name, game_type=dict(id=1, type='Main Game'), first_release_date=901_843_200,  # 1998-07-31
        summary='A  racing\ngame on made-up roads.', cover=dict(id=5, image_id='co1abc'),
        genres=[dict(id=1, name='Racing'), dict(id=2, name='Arcade'), dict(id=3, name='Sport')],
        game_modes=[dict(id=1, name='Single player'), dict(id=2, name='Multiplayer')],
        multiplayer_modes=[dict(id=1, platform=7, offlinemax=4), dict(id=2, platform=8, offlinemax=2)],
        involved_companies=[dict(id=1, company=dict(id=1, name='Made Up Soft'), publisher=True),
                            dict(id=2, company=dict(id=2, name='Pretend Studio'), publisher=False)],
        platforms=[7], url=f'https://www.igdb.com/games/game-{id}'), **extra)


def igdb(*rules, env=TWITCH):
    http = FakeHttp(*rules, TOKEN, ('images.igdb.com', 200, b'jpeg'), ('api.igdb.com', 200, b'[]'))
    return quick(Igdb.from_env(http, env)), http


def games(*entries):
    return json.dumps(list(entries)).encode()


class IgdbTests(IsolatedAsyncioTestCase):
    async def test_signs_in_once_and_keeps_the_secret_out_of_urls(self):
        provider, http = igdb(('search "Canyon Racer"', 200, games(entry(11, 'Canyon Racer'))))
        found = await provider.lookup(game('Canyon Racer (Europe) [SLES-00001]'))
        await provider.lookup(game('Canyon Racer (Europe)'))
        token_posts = [post for post in http.posts if 'twitch' in post[0]]
        self.assertEqual(len(token_posts), 1)
        url, data, _ = token_posts[0]
        self.assertEqual(url, 'https://id.twitch.tv/oauth2/token')  # the secret goes in the body, not the URL
        self.assertEqual(data, dict(client_id='client-id', client_secret='client-secret', grant_type='client_credentials'))
        url, body, headers = http.posts[1]
        self.assertEqual(url, 'https://api.igdb.com/v4/games')
        self.assertEqual(headers, {'Client-ID': 'client-id', 'Authorization': 'Bearer the-token', 'Accept': 'application/json'})
        # The clean title only, on its own console.
        self.assertTrue(body.decode().startswith('search "Canyon Racer"; fields '))
        self.assertIn('where platforms = (7);', body.decode())
        self.assertNotIn('secret', repr(provider))
        self.assertEqual(found.art, b'jpeg')
        self.assertEqual(http.requests[0][0], 'https://images.igdb.com/igdb/image/upload/t_cover_big_2x/co1abc.jpg')
        self.assertEqual(found.match, dict(id=11, name='Canyon Racer', year='1998', url='https://www.igdb.com/games/game-11'))

    async def test_details_from_the_entry(self):
        provider, _ = igdb(('games', 200, games(entry(11, 'Canyon Racer'))))
        found = await provider.lookup(game('Canyon Racer', console='ps1'))
        self.assertEqual(found.about, dict(description='A racing game on made-up roads.', year='1998',
                                           genre='Racing, Arcade', publisher='Made Up Soft', players='1-4'))
        found = await provider.lookup(game('Canyon Racer', console='ps2'), art=False)
        self.assertEqual(found.about['players'], '1-2')  # the console's own multiplayer modes
        self.assertIsNone(found.art)
        bare = entry(11, 'Canyon Racer', multiplayer_modes=[], game_modes=[dict(id=1, name='Single player')],
                     summary=None, genres=[], involved_companies=[], first_release_date=None, cover=None)
        provider, _ = igdb(('games', 200, games(bare)))
        found = await provider.lookup(game('Canyon Racer'))
        self.assertEqual((found.about, found.art), (dict(players='1'), None))

    async def test_matches_a_colon_an_alternative_name_or_a_series_prefix(self):
        provider, http = igdb(('search "Sky Ball Z: Densetsu"', 200, games(
            entry(21, 'Sky Ball Z: The Legend', alternative_names=[dict(id=1, name='Sky Ball Z: Densetsu')]))))
        found = await provider.lookup(game('Sky Ball Z - Densetsu (Japan)'))
        self.assertEqual(found.match['id'], 21)
        # A spaced romanisation still matches the joined one, as letters only.
        provider, _ = igdb(('search "Sky Ball Z - Den Setsu"', 200, games(
            entry(21, 'Sky Ball Z: The Legend', alternative_names=[dict(id=1, name='Sky Ball Z: Densetsu')]))))
        self.assertEqual((await provider.lookup(game('Sky Ball Z - Den Setsu (Japan)'))).match['id'], 21)
        provider, _ = igdb(('games', 200, games(entry(31, 'Jack Hawk 042: Night Agent'))))
        self.assertEqual((await provider.lookup(game('042 - Night Agent (USA)'))).match['id'], 31)

    async def test_tries_the_subtitle_when_the_full_name_finds_nothing(self):
        provider, http = igdb(('search "Den Setsu"', 200, games(
            entry(21, 'Sky Ball Z: The Legend', alternative_names=[dict(id=1, name='Sky Ball Z: Den Setsu')]))))
        found = await provider.lookup(game('Sky Ball Z - Den Setsu (Japan)'))
        self.assertEqual(found.match['id'], 21)
        searches = [post[1].decode().split(';')[0] for post in http.posts if post[0].endswith('/games')]
        self.assertEqual(searches, ['search "Sky Ball Z - Den Setsu"', 'search "Sky Ball Z: Den Setsu"', 'search "Den Setsu"'])

    async def test_shows_nothing_rather_than_a_wrong_match(self):
        provider, _ = igdb(('games', 200, games(entry(41, 'Canyon Racer 2'), entry(42, 'Super Canyon Racer Deluxe'),
                                                entry(43, 'Racer'))))
        found = await provider.lookup(game('Canyon Racer (USA)'))
        self.assertIsNone(found.match)
        self.assertEqual((found.art, found.about), (None, {}))
        self.assertEqual([item['id'] for item in found.candidates], [41, 42, 43])
        # A short name inside a longer one is not enough either.
        provider, _ = igdb(('games', 200, games(entry(44, 'The Big Racer'))))
        self.assertIsNone((await provider.lookup(game('Racer'))).match)
        provider, _ = igdb()
        self.assertIsNone(await provider.lookup(game('Racer')))
        self.assertIsNone(await provider.lookup(game('Racer', console='other')))

    async def test_two_of_the_same_name_take_the_main_game_or_ask(self):
        edition = entry(52, 'Canyon Racer', game_type=dict(id=2, type='Port'), version_parent=51)
        provider, _ = igdb(('games', 200, games(edition, entry(51, 'Canyon Racer'))))
        self.assertEqual((await provider.lookup(game('Canyon Racer'))).match['id'], 51)
        provider, _ = igdb(('games', 200, games(entry(51, 'Canyon Racer'), entry(53, 'Canyon Racer'))))
        found = await provider.lookup(game('Canyon Racer'))
        self.assertIsNone(found.match)
        self.assertEqual([item['id'] for item in found.candidates], [51, 53])

    async def test_an_admin_pick_is_fetched_by_id(self):
        provider, http = igdb(('where id = 77;', 200, games(entry(77, 'Something Else Entirely', platforms=[32]))))
        found = await provider.lookup(game('Canyon Racer'), choice=77)
        self.assertEqual(found.match['id'], 77)
        self.assertNotIn('search', str(http.posts))
        provider, http = igdb()
        self.assertIsNone(await provider.lookup(game('Canyon Racer'), choice=0))
        self.assertEqual(http.posts, [])  # "no match" asks nothing at all

    async def test_only_igdb_image_ids_and_links(self):
        provider, http = igdb(('games', 200, games(entry(11, 'Canyon Racer', cover=dict(image_id='../x?y'),
                                                         url='https://elsewhere.example/games/1'))))
        found = await provider.lookup(game('Canyon Racer'))
        self.assertEqual((found.art, found.match['url'], http.requests), (None, None, []))

    async def test_errors(self):
        provider, _ = igdb(('id.twitch.tv', 403, b'{"message":"invalid client secret"}'))
        with self.assertRaises(ProviderError) as caught:
            await provider.lookup(game('Canyon Racer'))
        self.assertTrue(caught.exception.stop)
        self.assertNotIn('client-secret', str(caught.exception))
        provider, http = igdb(('api.igdb.com', 429, b''))
        with self.assertRaisesRegex(ProviderError, 'busy'):
            await provider.lookup(game('Canyon Racer'))
        self.assertEqual(len([post for post in http.posts if 'igdb.com' in post[0]]), 3)
        self.assertFalse(provider.stopped)
        provider, _ = igdb(('api.igdb.com', 500, b''))
        with self.assertRaisesRegex(ProviderError, 'IGDB answered 500'):
            await provider.lookup(game('Canyon Racer'))
        provider, _ = igdb(('api.igdb.com', 200, b'<html>'))
        with self.assertRaisesRegex(ProviderError, 'could not read'):
            await provider.lookup(game('Canyon Racer'))

    async def test_signs_in_again_when_the_token_is_refused(self):
        answers = iter([401, 200])
        provider, http = igdb(('api.igdb.com', lambda body: next(answers), games(entry(11, 'Canyon Racer'))))
        self.assertEqual((await provider.lookup(game('Canyon Racer'))).match['id'], 11)
        self.assertEqual(len([post for post in http.posts if 'twitch' in post[0]]), 2)


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

    async def test_unsure_matches_keep_candidates_and_show_nothing(self):
        provider, _ = igdb(('games', 200, games(entry(41, 'Puzzle Quest 2'), entry(42, 'Puzzle Quest Deluxe'))))
        fetcher = await self.run_fetch(provider)
        record = self.covers.record(self.game.id)
        self.assertEqual((record['about'], record['match']), ({}, None))
        self.assertEqual([(item['id'], item['provider']) for item in record['candidates']], [(41, 'igdb'), (42, 'igdb')])
        self.assertEqual(record['note'], 'Not sure which game this is: choose the match')
        self.assertIsNone(self.covers.info(self.game))
        self.assertEqual(fetcher.missing([self.game]), [])

    async def test_libretro_art_and_igdb_details_with_the_admin_pick(self):
        art = FakeHttp(('Named_Boxarts', 200, image(kind='PNG')))
        provider, http = igdb(('where id = 9;', 200, games(entry(9, 'Puzzle Quest: The Return'))))
        self.covers.save_match(self.game.id, 'igdb', 9)
        await self.run_fetch(Libretro(art), provider)
        record = self.covers.record(self.game.id)
        self.assertEqual(record['sources'], ['libretro', 'igdb'])
        self.assertEqual(record['match']['id'], 9)
        self.assertEqual(record['about']['publisher'], 'Made Up Soft')
        self.assertEqual(http.requests, [])  # libretro had the art: no IGDB image

    async def test_a_new_match_drops_fetched_art_but_never_uploads_or_edits(self):
        await self.run_fetch(Libretro(FakeHttp(('Named_Boxarts', 200, image(kind='PNG')))))
        uploaded = image(kind='JPEG', colour=(1, 2, 3))
        self.covers.save(self.game.id, uploaded)
        self.covers.save_edits(self.game.id, dict(genre='Mine'))
        self.covers.save_match(self.game.id, 'igdb', 0)
        self.assertFalse(self.covers.fetched(self.game.id).exists())
        self.assertIsNone(self.covers.record(self.game.id))
        self.assertEqual((self.covers.read(self.game), self.covers.about(self.game.id)), (uploaded, (dict(genre='Mine'), ['genre'])))
        self.assertEqual(self.covers.matches(self.game.id), dict(igdb=0))
        self.covers.save_match(self.game.id, 'igdb', None)
        self.assertEqual(self.covers.matches(self.game.id), {})

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


    async def test_admin_picks_the_match_and_players_never_see_candidates(self):
        provider, http = igdb(('search "Puzzle Quest"', 200, games(entry(41, 'Puzzle Quest 2'), entry(42, 'Puzzle Quest III'))),
                              ('where id = 42;', 200, games(entry(42, 'Puzzle Quest III'))),
                              ('images.igdb.com', 200, image(kind='JPEG')))
        await self.start([provider])
        player, admin = await self.join(), await self.join(admin=True)
        puzzle = (await self.games(admin))[0]['Puzzle Quest']
        await self.client.post('/api/cover/fetch', json=dict(id=puzzle['id']), headers=admin)
        await self.room.fetcher.task
        games_, body = await self.games(admin)
        self.assertEqual(body['matching'], 'igdb')
        fetch = games_['Puzzle Quest']['fetch']
        self.assertEqual(([item['id'] for item in fetch['candidates']], fetch['match'], fetch['picked']), ([41, 42], None, {}))
        self.assertNotIn('candidates', (await self.games(player))[0]['Puzzle Quest']['fetch'])

        def pick(auth, **body):
            return self.client.post('/api/game/match', json=dict(dict(id=puzzle['id'], provider='igdb'), **body), headers=auth)
        self.assertEqual((await pick(player, entry=42)).status, 403)
        for bad in ('42', -1, 1.5, True, 2**31):
            self.assertEqual((await pick(admin, entry=bad)).status, 400, bad)
        self.assertEqual((await pick(admin, entry=42, provider='libretro')).status, 404)
        self.assertEqual(await (await pick(admin, entry=42)).json(), dict(ok=True, queued=1))
        await self.room.fetcher.task
        fetch = (await self.games(admin))[0]['Puzzle Quest']['fetch']
        self.assertEqual((fetch['match']['id'], fetch['candidates'], fetch['picked']), (42, [], dict(igdb=42)))
        self.assertTrue((await self.games(player))[0]['Puzzle Quest']['cover']['fetched'])
        # No match: what was fetched goes, and nothing is asked again.
        asked = len(http.posts)
        self.assertEqual((await pick(admin, entry=0)).status, 200)
        await self.room.fetcher.task
        puzzle = (await self.games(admin))[0]['Puzzle Quest']
        self.assertEqual((puzzle['cover'], puzzle['about'], puzzle['fetch']['picked']), (None, None, dict(igdb=0)))
        self.assertEqual(len(http.posts), asked)
        # Back to matching by name.
        self.assertEqual((await pick(admin, entry=None)).status, 200)
        await self.room.fetcher.task
        self.assertEqual((await self.games(admin))[0]['Puzzle Quest']['fetch']['picked'], {})
