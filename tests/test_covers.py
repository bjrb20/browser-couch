"""Cover art: validation, storage and the admin-only routes. Every image here is generated."""
from io import BytesIO
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase, TestCase

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import covers
from covers import BadImage, Covers, encode
from inputs import InputHub
from library import Library
from server import Room, make_app
from session import Session

from test_library import Host


def image(size=(300, 400), mode='RGB', kind='PNG', colour=(200, 40, 40), **save):
    out = BytesIO()
    Image.new(mode, size, colour if mode == 'RGB' else colour+(0,)*(len(mode)-3)).save(out, kind, **save)
    return out.getvalue()


def exif_jpeg():
    picture = Image.new('RGB', (64, 32), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0110] = 'A camera model'   # Model
    exif[0x8825] = {2: (51, 30, 0)}  # GPS latitude
    exif[0x0112] = 6                 # Orientation: rotate 90°
    out = BytesIO()
    picture.save(out, 'JPEG', exif=exif, comment=b'a comment')
    return out.getvalue()


class EncodeTests(TestCase):
    def test_accepts_jpeg_png_and_webp_by_content(self):
        for kind in ('JPEG', 'PNG', 'WEBP'):
            with self.subTest(kind=kind):
                with Image.open(BytesIO(encode(image(kind=kind)))) as out:
                    self.assertEqual((out.format, out.size, out.mode), ('JPEG', (300, 400), 'RGB'))

    def test_refuses_other_formats_and_non_images_whatever_their_name(self):
        for data in (image(kind='GIF'), image(kind='BMP'), b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                     b'%PDF-1.4', b'', b'\xff\xd8\xff\xe0' + b'\0'*40, image(kind='PNG')[:60]):
            with self.subTest(data=data[:8]), self.assertRaises(BadImage):
                encode(data)

    def test_size_cap(self):
        with self.assertRaisesRegex(BadImage, '5 MB'):
            encode(b'\x89PNG' + b'\0'*covers.MAX_UPLOAD)

    def test_refuses_too_many_pixels(self):
        # A small file that would decode to 50 million pixels.
        with self.assertRaises(BadImage):
            encode(image(size=(10000, 5000), mode='L', colour=(0,), kind='PNG', optimize=True))

    def test_downscales_to_fit_and_keeps_the_shape(self):
        with Image.open(BytesIO(encode(image(size=(1200, 1800), kind='JPEG')))) as out:
            self.assertEqual(out.size, (600, 900))
        with Image.open(BytesIO(encode(image(size=(2000, 1000), kind='WEBP')))) as out:
            self.assertEqual(out.size, (600, 300))

    def test_strips_metadata_after_applying_the_orientation(self):
        with Image.open(BytesIO(encode(exif_jpeg()))) as out:
            self.assertEqual(out.size, (32, 64), 'rotated by its orientation tag')
            self.assertEqual(len(out.getexif()), 0)
            for key in ('exif', 'icc_profile', 'comment', 'xmp'):
                self.assertNotIn(key, out.info)
        self.assertNotIn(b'camera model', encode(exif_jpeg()))

    def test_flattens_transparency_onto_the_page_colour(self):
        with Image.open(BytesIO(encode(image(mode='RGBA', colour=(255, 255, 255), size=(20, 20))))) as out:
            red, green, blue = out.getpixel((10, 10))
            self.assertLess(max(red, green, blue), 40)


class StoreTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = Host(self.temporary.name)
        self.host.core('pcsx_rearmed')
        self.covers = Covers(self.host.root/'covers', self.host.library)

    def game(self, title):
        return next(game for game in self.host.scan().games.values() if game.name == title)

    def test_upload_is_keyed_by_game_id_and_can_be_replaced_and_removed(self):
        self.host.file('ps1/Puzzle/Puzzle.pbp')
        game = self.game('Puzzle')
        self.assertIsNone(self.covers.info(game))
        self.covers.save(game.id, encode(image()))
        self.assertEqual([path.name for path in (self.host.root/'covers').iterdir()], [f'{game.id}.jpg'])
        first = self.covers.info(game)
        self.assertTrue(first['uploaded'])
        self.covers.save(game.id, encode(image(colour=(0, 0, 255), size=(30, 40))))
        self.assertNotEqual(self.covers.info(game)['version'], first['version'])
        with Image.open(BytesIO(self.covers.read(game))) as out:
            self.assertEqual(out.size, (30, 40))
        self.assertTrue(self.covers.remove(game.id))
        self.assertFalse(self.covers.remove(game.id))
        self.assertIsNone(self.covers.info(game))

    def test_library_cover_in_a_game_folder_is_re_encoded(self):
        self.host.file('ps1/Puzzle/Puzzle.pbp')
        (self.host.library/'ps1/Puzzle/cover.png').write_bytes(image(size=(1000, 1500)))
        game = self.game('Puzzle')
        self.assertFalse(self.covers.info(game)['uploaded'])
        with Image.open(BytesIO(self.covers.read(game))) as out:
            self.assertEqual((out.format, out.size), ('JPEG', (600, 900)))
        self.covers.save(game.id, encode(image(size=(30, 40))))
        self.assertTrue(self.covers.info(game)['uploaded'], 'an upload wins over the library file')
        self.covers.remove(game.id)
        self.assertTrue((self.host.library/'ps1/Puzzle/cover.png').is_file(), 'the library file stays')
        self.assertFalse(self.covers.info(game)['uploaded'])

    def test_a_cover_the_console_folder_shares_is_not_any_game_cover(self):
        self.host.file('ps1/Puzzle.pbp')
        (self.host.library/'ps1/cover.jpg').write_bytes(image(kind='JPEG'))
        self.assertIsNone(self.covers.info(self.game('Puzzle')))

    def test_an_unreadable_library_cover_is_no_cover(self):
        self.host.file('ps1/Puzzle/Puzzle.pbp')
        (self.host.library/'ps1/Puzzle/cover.jpg').write_bytes(b'not an image')
        self.assertIsNone(self.covers.read(self.game('Puzzle')))

    def test_refuses_anything_but_a_game_id_as_a_file_name(self):
        for bad in ('../escape', '0123456789abcdeg', '0123456789ABCDEF', '', None, 'a'*17):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.covers.uploaded(bad)


class CoverApiTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = Host(self.temporary.name)
        self.host.core('pcsx_rearmed')
        self.host.file('ps1/Puzzle/Puzzle.pbp')
        self.host.file('ps1/Racer/Racer.pbp')
        (self.host.library/'ps1/Racer/cover.jpg').write_bytes(image(kind='JPEG'))
        library = Library(self.host.library, self.host.system, self.host.cores, 'Linux')
        self.room = Room(Session(InputHub(4)), 'qa-canyon', library=library, admin_pin='4321',
                         covers=Covers(self.host.root/'covers', self.host.library))
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

    async def test_library_lists_cover_versions_and_no_paths(self):
        auth = await self.join()
        games, body = await self.games(auth)
        self.assertTrue(body['covers'])
        self.assertIsNone(games['Puzzle']['cover'])
        self.assertEqual(set(games['Racer']['cover']), {'version', 'uploaded'})
        self.assertNotIn(self.temporary.name, str(body))
        self.assertNotIn('cover.jpg', str(body))

    async def test_every_player_sees_covers_but_must_join(self):
        auth = await self.join()
        games, _ = await self.games(auth)
        url = '/api/cover?id=' + games['Racer']['id']
        self.assertEqual((await self.client.get(url)).status, 401)
        response = await self.client.get(url, headers=auth)
        self.assertEqual((response.status, response.content_type), (200, 'image/jpeg'))
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        with Image.open(BytesIO(await response.read())) as out:
            self.assertEqual(out.format, 'JPEG')
        self.assertEqual((await self.client.get('/api/cover?id=' + games['Puzzle']['id'], headers=auth)).status, 404)
        for bad in ('../../etc/passwd', 'ps1/Racer/cover.jpg', ''):
            self.assertEqual((await self.client.get('/api/cover', params=dict(id=bad), headers=auth)).status, 404)

    async def test_only_admins_upload_and_remove(self):
        auth = await self.join()
        games, _ = await self.games(auth)
        puzzle = games['Puzzle']['id']
        response = await self.client.post('/api/cover?id='+puzzle, data=image(), headers=auth)
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.client.post('/api/cover/remove', json=dict(id=puzzle), headers=auth)).status, 403)
        self.assertFalse((self.host.root/'covers').exists())
        admin = await self.join(admin=True)
        response = await self.client.post('/api/cover?id='+puzzle, data=image(kind='WEBP'), headers=admin)
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await response.json())['cover']['uploaded'])
        games, _ = await self.games(auth)
        self.assertTrue(games['Puzzle']['cover']['uploaded'], 'every player sees the new cover')
        response = await self.client.post('/api/cover/remove', json=dict(id=puzzle), headers=admin)
        self.assertIsNone((await response.json())['cover'])

    async def test_without_an_admin_pin_every_player_may_change_covers(self):
        self.room.admin_pin = None
        auth = await self.join()
        games, _ = await self.games(auth)
        response = await self.client.post('/api/cover?id='+games['Puzzle']['id'], data=image(), headers=auth)
        self.assertEqual(response.status, 200)

    async def test_bad_uploads_are_refused(self):
        admin = await self.join(admin=True)
        games, _ = await self.games(admin)
        url = '/api/cover?id='+games['Puzzle']['id']
        for data in (b'GIF89a not really', image(kind='GIF'), b'<svg/>'):
            response = await self.client.post(url, data=data, headers=dict(admin, **{'Content-Type': 'image/png'}))
            self.assertEqual(response.status, 415)
        response = await self.client.post(url, data=BytesIO(b'\0'*(covers.MAX_UPLOAD+1)), headers=admin)
        self.assertEqual(response.status, 413)

        async def chunks():  # no Content-Length: the host stops reading at the cap
            for _ in range(6):
                yield b'\0'*(1024*1024)
        response = await self.client.post(url, data=chunks(), headers=admin)
        self.assertEqual(response.status, 413)
        missing = await self.client.post('/api/cover?id=0123456789abcdef', data=image(), headers=admin)
        self.assertEqual(missing.status, 404)
        self.assertEqual(list((self.host.root/'covers').glob('*')) if (self.host.root/'covers').exists() else [], [])

    async def test_cover_changes_are_rate_limited(self):
        admin = await self.join(admin=True)
        games, _ = await self.games(admin)
        url = '/api/cover?id='+games['Puzzle']['id']
        statuses = [(await self.client.post(url, data=b'x', headers=admin)).status for _ in range(13)]
        self.assertEqual(statuses[:12], [415]*12)
        self.assertEqual(statuses[12], 429)

    async def test_a_cross_site_upload_is_refused(self):
        admin = await self.join(admin=True)
        games, _ = await self.games(admin)
        response = await self.client.post('/api/cover?id='+games['Puzzle']['id'], data=image(),
                                          headers=dict(admin, Origin='https://elsewhere.example'))
        self.assertEqual(response.status, 403)

    async def test_page_may_show_blob_images(self):
        response = await self.client.get('/')
        self.assertIn("img-src 'self' data: blob:", response.headers['Content-Security-Policy'])
