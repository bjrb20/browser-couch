"""Remembered devices (a refresh rejoins) and an admin moving players between slots."""
import json
import os
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase, TestCase

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from inputs import InputHub
from server import ADMIN_COOKIE, PLAYER_COOKIE, Room, make_app
from session import Session
from sessions import ADMIN_FOR, PLAYER_FOR, Sessions

from test_switching import FakeChannel, FakeLauncher, library
from test_library import Host

DAY = 24*3600


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Device:
    """One browser: its own cookie jar, like a phone or a laptop."""
    def __init__(self, client):
        self.client = client
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.token = None

    async def call(self, path, body=None, token=None):
        token = token or self.token
        headers = dict(Authorization='Bearer '+token) if token else {}
        method = self.http.get if body is None else self.http.post
        kwargs = {} if body is None else dict(json=body)
        async with method(self.client.make_url('/api/'+path), headers=headers, **kwargs) as response:
            text = await response.text()
            return response.status, (json.loads(text) if text.startswith('{') else text), response

    def cookie(self, name):
        found = [c for c in self.http.cookie_jar if c.key == name]
        return found[0].value if found else None

    async def join(self, name='Ann', pin='qa-canyon', **extra):
        status, body, _ = await self.call('join', dict(name=name, pin=pin, **extra))
        assert status == 200, body
        self.token = body['token']
        return body

    async def resume(self, **body):
        status, body, _ = await self.call('resume', body)
        if body.get('token'):
            self.token = body['token']
        return status, body


class Base(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = library(Host(self.temporary.name))
        self.file = Path(self.temporary.name)/'saves/remembered-devices.json'
        self.clock = Clock()

    async def room(self, pin='qa-canyon', admin_pin='4321'):
        sessions = Sessions(self.file, pin, admin_pin, self.clock)
        room = Room(Session(InputHub(4)), pin, library=self.library, launcher=FakeLauncher([]),
                    admin_pin=admin_pin, sessions=sessions)
        client = TestClient(TestServer(make_app(room)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return room, client

    async def device(self, client):
        device = Device(client)
        self.addAsyncCleanup(device.http.close)
        return device

    def connect(self, room, device):
        room.clients[device.token]['channel'] = FakeChannel()

    async def claim(self, room, device, slot):
        self.connect(room, device)
        status, body, _ = await device.call('claim', dict(slot=slot))
        self.assertEqual(status, 200, body)


class ResumeTests(Base):
    async def test_a_refresh_rejoins_with_the_same_name_and_slot(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        cookie = phone.cookie(PLAYER_COOKIE)
        self.assertTrue(cookie)
        await self.claim(room, phone, 2)
        old = phone.token

        # The page unloads, then loads again.
        await phone.call('leave', dict(unload=True))
        self.assertEqual(room.hub.slots(), [None]*4)
        status, body = await phone.resume()
        self.assertEqual(status, 200)
        self.assertEqual((body['name'], body['slot'], body['admin']), ('Ann', 2, False))
        self.assertNotEqual(phone.token, old)
        self.assertEqual(room.hub.slots()[2], phone.token)
        state = (await phone.call('state'))[1]
        self.assertTrue(state['slots'][2]['yours'])
        self.assertTrue(state['remembered'])

    async def test_the_cookie_is_httponly_and_strict_and_only_its_hash_is_kept(self):
        room, client = await self.room()
        phone = await self.device(client)
        _, _, response = await phone.call('join', dict(name='Ann', pin='qa-canyon'))
        morsel = response.cookies[PLAYER_COOKIE]
        self.assertTrue(morsel['httponly'])
        self.assertEqual(morsel['samesite'], 'Strict')
        self.assertEqual(morsel['path'], '/')
        self.assertEqual(int(morsel['max-age']), PLAYER_FOR)
        self.assertFalse(morsel['secure'], 'plain HTTP on the LAN; HTTPS pages get Secure')
        saved = self.file.read_text()
        self.assertNotIn(morsel.value, saved)
        self.assertNotIn('qa-canyon', saved)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertNotIn(morsel.value, json.dumps((await phone.call('state', token=(await phone.join())['token']))[1]))

    async def test_behind_an_https_proxy_the_cookie_is_secure(self):
        room, client = await self.room()
        response = await client.post('/api/join', json=dict(name='Ann', pin='qa-canyon'),
                                     headers={'X-Forwarded-Proto': 'https'})
        self.assertTrue(response.cookies[PLAYER_COOKIE]['secure'])

    async def test_a_page_that_never_said_goodbye_gives_way_slot_and_all(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await self.claim(room, phone, 1)
        old = phone.token
        await phone.resume()  # a second tab, or a reload whose goodbye never arrived
        self.assertEqual(room.hub.slots()[1], phone.token)
        self.assertEqual(len(room.clients), 1)
        status, text, _ = await phone.call('state', token=old)
        self.assertEqual(status, 401)
        self.assertIn('another tab', text)

    async def test_a_slot_someone_else_took_meanwhile_is_not_taken_back(self):
        room, client = await self.room()
        phone, laptop = await self.device(client), await self.device(client)
        await phone.join('Ann')
        await self.claim(room, phone, 0)
        await phone.call('leave', dict(unload=True))
        await laptop.join('Bob')
        await self.claim(room, laptop, 0)
        status, body = await phone.resume()
        self.assertEqual((body['slot'], body['wanted']), (None, 0))
        self.assertEqual(room.hub.slots()[0], laptop.token)

    async def test_leave_asks_before_rejoining(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await self.claim(room, phone, 3)
        status, body, _ = await phone.call('leave', {})
        self.assertEqual((body['remembered'], body['name']), (True, 'Ann'))
        status, body = await phone.resume()
        self.assertEqual(body, dict(remembered=True, name='Ann'))
        self.assertEqual(len(room.clients), 0, 'not let in by itself')
        status, body = await phone.resume(rejoin=True)
        self.assertEqual((body['name'], body['slot']), ('Ann', 3))

    async def test_a_device_not_remembered_gets_no_cookie(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann', remember=False)
        self.assertIsNone(phone.cookie(PLAYER_COOKIE))
        self.assertEqual((await phone.resume())[1], dict(remembered=False, expired=False))

    async def test_it_survives_a_restart_with_the_same_codes(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await self.claim(room, phone, 1)
        await phone.call('leave', dict(unload=True))
        await client.close()
        room, client = await self.room()
        phone.client = client
        status, body = await phone.resume()
        self.assertEqual((body['name'], body['slot']), ('Ann', 1))


class AdminResumeTests(Base):
    async def test_admin_rights_come_back_after_a_refresh(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        status, _, response = await phone.call('admin', dict(pin='4321'))
        self.assertEqual(status, 200)
        morsel = response.cookies[ADMIN_COOKIE]
        self.assertTrue(morsel['httponly'])
        self.assertEqual(int(morsel['max-age']), ADMIN_FOR)
        self.assertNotEqual(morsel.value, phone.cookie(PLAYER_COOKIE), 'two separate tokens')
        await phone.call('leave', dict(unload=True))
        status, body = await phone.resume()
        self.assertTrue(body['admin'])
        state = (await phone.call('state'))[1]
        self.assertTrue(state['canPick'] and state['admin'])

    async def test_the_admin_login_also_comes_back_with_the_room_code(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        await phone.call('leave', {})
        self.assertTrue((await phone.join('Ann'))['admin'])

    async def test_an_admin_login_alone_is_not_a_way_in(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        phone.http.cookie_jar.clear(lambda c: c.key == PLAYER_COOKIE)
        await phone.call('leave', dict(unload=True))
        self.assertFalse((await phone.resume())[1]['remembered'])
        self.assertEqual(len(room.clients), 0)

    async def test_lock_drops_the_admin_rights_and_keeps_the_player(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        await phone.call('admin/lock', {})
        self.assertIsNone(phone.cookie(ADMIN_COOKIE))
        self.assertFalse((await phone.call('state'))[1]['canPick'])
        await phone.call('leave', dict(unload=True))
        status, body = await phone.resume()
        self.assertEqual((body['name'], body['admin']), ('Ann', False))


class ExpiryTests(Base):
    async def test_a_player_login_lasts_30_days_from_its_last_use(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('leave', dict(unload=True))
        self.clock.now += 20*DAY
        self.assertIn('token', (await phone.resume())[1])
        await phone.call('leave', dict(unload=True))
        self.clock.now += 20*DAY  # 40 days after joining, 20 after the last use
        self.assertIn('token', (await phone.resume())[1])
        await phone.call('leave', dict(unload=True))
        self.clock.now += 30*DAY+1
        status, body = await phone.resume()
        self.assertEqual(body, dict(remembered=False, expired=True))
        self.assertIsNone(phone.cookie(PLAYER_COOKIE), 'the dead cookie is cleared')
        self.assertEqual(json.loads(self.file.read_text()), {}, 'and its record dropped')

    async def test_an_admin_login_lasts_7_days_however_often_it_is_used(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        for _ in range(3):
            await phone.call('leave', dict(unload=True))
            self.clock.now += 2*DAY
            self.assertTrue((await phone.resume())[1]['admin'])
        await phone.call('leave', dict(unload=True))
        self.clock.now += 1*DAY+1
        status, body = await phone.resume()
        self.assertEqual((body['name'], body['admin']), ('Ann', False), 'still a player, no longer an admin')


class WrongTokenTests(Base):
    async def test_a_made_up_or_altered_token_gets_nothing(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        real = phone.cookie(PLAYER_COOKIE)
        await phone.call('leave', dict(unload=True))
        for fake in ('x'*43, real[:-1]+('A' if real[-1] != 'A' else 'B'), 'short', 'a'*500):
            phone.http.cookie_jar.update_cookies({PLAYER_COOKIE: fake}, client.make_url('/'))
            status, body = await phone.resume()
            self.assertEqual(body, dict(remembered=False, expired=True), fake)
        self.assertEqual(len(room.clients), 0)

    async def test_an_admin_token_is_not_a_player_token(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        admin = phone.cookie(ADMIN_COOKIE)
        phone.http.cookie_jar.update_cookies({PLAYER_COOKIE: admin}, client.make_url('/'))
        await phone.call('leave', dict(unload=True))
        self.assertFalse((await phone.resume())[1]['remembered'])

    async def test_a_new_room_code_ends_every_player_login(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('leave', dict(unload=True))
        await client.close()
        room, client = await self.room(pin='new-code-99')
        phone.client = client
        self.assertEqual((await phone.resume())[1], dict(remembered=False, expired=True))

    async def test_a_new_admin_pin_ends_every_admin_login_but_not_the_player(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        await phone.call('leave', dict(unload=True))
        await client.close()
        room, client = await self.room(admin_pin='8765')
        phone.client = client
        status, body = await phone.resume()
        self.assertEqual((body['name'], body['admin']), ('Ann', False))

    async def test_bad_cookies_count_as_join_attempts(self):
        room, client = await self.room()
        phone = await self.device(client)
        for n in range(13):
            phone.http.cookie_jar.update_cookies({PLAYER_COOKIE: f'{n:x}'*40}, client.make_url('/'))
            status, _, _ = await phone.call('resume', {})
        self.assertEqual(status, 429)


class ForgetTests(Base):
    async def test_forget_ends_both_logins_and_leaves(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('admin', dict(pin='4321'))
        player, admin = phone.cookie(PLAYER_COOKIE), phone.cookie(ADMIN_COOKIE)
        status, _, _ = await phone.call('forget', {})
        self.assertEqual(status, 200)
        self.assertEqual(len(room.clients), 0)
        self.assertIsNone(phone.cookie(PLAYER_COOKIE))
        self.assertIsNone(phone.cookie(ADMIN_COOKIE))
        self.assertEqual(json.loads(self.file.read_text()), {})
        # A copy of the old cookies taken before is no good either.
        phone.http.cookie_jar.update_cookies({PLAYER_COOKIE: player, ADMIN_COOKIE: admin}, client.make_url('/'))
        self.assertFalse((await phone.resume())[1]['remembered'])
        self.assertFalse((await phone.join('Ann'))['admin'])

    async def test_forget_works_from_the_join_screen(self):
        room, client = await self.room()
        phone = await self.device(client)
        await phone.join('Ann')
        await phone.call('leave', {})
        phone.token = None
        await phone.call('forget', {})
        self.assertFalse((await phone.resume())[1]['remembered'])


class SeatTests(Base):
    async def room_of_three(self):
        room, client = await self.room()
        admin, ann, bob = [await self.device(client) for _ in range(3)]
        await admin.join('Host')
        await admin.call('admin', dict(pin='4321'))
        await ann.join('Ann')
        await bob.join('Bob')
        await self.claim(room, admin, 0)
        await self.claim(room, ann, 1)
        await self.claim(room, bob, 2)
        roster = {p['name']: p['id'] for p in (await admin.call('state'))[1]['roster']}
        return room, admin, ann, bob, roster

    async def test_only_an_admin_sees_the_roster_or_moves_anyone(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        state = (await ann.call('state'))[1]
        self.assertIsNone(state['roster'])
        self.assertFalse(state['admin'])
        before = room.hub.slots()
        for body in (dict(action='seat', player=ids['Bob'], slot=3), dict(action='swap', a=1, b=2),
                     dict(action='free', slot=0), dict(action='kick', player=ids['Host']),
                     dict(action='seat', player=ids['Ann'], slot=0)):
            status, text, _ = await ann.call('seats', body)
            self.assertEqual(status, 403, body)
        self.assertEqual(room.hub.slots(), before)
        self.assertEqual(len(room.clients), 3)
        self.assertNotIn('id', json.dumps(state['slots']))

    async def test_no_admin_pin_means_nobody_moves_anyone(self):
        room = Room(Session(InputHub(4)), 'qa-canyon', library=self.library, launcher=FakeLauncher([]))
        client = TestClient(TestServer(make_app(room)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        phone = await self.device(client)
        await phone.join('Ann')
        self.assertTrue((await phone.call('state'))[1]['canPick'], 'everyone may pick games here')
        self.assertEqual((await phone.call('seats', dict(action='free', slot=0)))[0], 403)

    async def test_move_swap_and_free(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        self.assertEqual((await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=3)))[0], 200)
        self.assertEqual(room.hub.slots(), [admin.token, ann.token, None, bob.token])
        status, text, _ = await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=1))
        self.assertEqual(status, 409, 'a taken slot needs a swap')
        self.assertEqual((await admin.call('seats', dict(action='swap', a=1, b=3)))[0], 200)
        self.assertEqual(room.hub.slots(), [admin.token, bob.token, None, ann.token])
        self.assertEqual((await admin.call('seats', dict(action='swap', a=3, b=2)))[0], 200, 'swap with an open slot')
        self.assertEqual(room.hub.slots(), [admin.token, bob.token, ann.token, None])
        self.assertEqual((await admin.call('seats', dict(action='free', slot=1)))[0], 200)
        self.assertEqual(room.hub.slots(), [admin.token, None, ann.token, None])
        self.assertEqual((await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=None)))[0], 200)
        self.assertEqual((await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=3)))[0], 200,
                         'a watcher can be seated')
        roster = {p['name']: p['slot'] for p in (await admin.call('state'))[1]['roster']}
        self.assertEqual(roster, dict(Host=0, Ann=2, Bob=3))
        await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=None))
        self.assertEqual((await admin.call('seats', dict(action='seat', player=ids['Bob'], slot=2, replace=True)))[0], 200)
        self.assertEqual(room.hub.slots(), [admin.token, None, bob.token, None], 'Ann now watches')
        await admin.call('seats', dict(action='seat', player=ids['Ann'], slot=3))
        await admin.call('seats', dict(action='swap', a=2, b=3))
        for body in (dict(action='swap', a=1, b=1), dict(action='swap', a=0, b=9), dict(action='free', slot='1'),
                     dict(action='seat', player=ids['Bob'], slot=4)):
            self.assertEqual((await admin.call('seats', body))[0], 409, body)
        self.assertEqual((await admin.call('seats', dict(action='seat', player='nobody', slot=1)))[0], 404)
        self.assertEqual((await admin.call('seats', dict(action='dance')))[0], 400)

    async def test_input_follows_the_slot_and_held_buttons_drop(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        room.hub.update(ann.token, 1, 0x10)
        self.assertEqual(room.hub.snapshot(), [0, 0x10, 0, 0])
        await admin.call('seats', dict(action='swap', a=1, b=3))
        self.assertEqual(room.hub.snapshot(), [0, 0, 0, 0], 'nothing held carries over')
        room.hub.update(ann.token, 2, 0x20)
        self.assertEqual(room.hub.snapshot(), [0, 0, 0, 0x20], 'Ann now drives port 4')

    async def test_everyone_is_told_at_once_and_the_page_sees_its_new_slot(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        for token in room.clients:
            room.clients[token]['channel'].sent.clear()
        await admin.call('seats', dict(action='swap', a=1, b=2))
        for token in room.clients:
            self.assertIn(dict(t='seats'), room.clients[token]['channel'].sent)
        state = (await ann.call('state'))[1]
        self.assertTrue(state['slots'][2]['yours'])

    async def test_kick_removes_the_player_and_forgets_their_device(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        channel = room.clients[bob.token]['channel']
        self.assertEqual((await admin.call('seats', dict(action='kick', player=ids['Bob'])))[0], 200)
        self.assertIn(dict(t='removed'), channel.sent)
        self.assertEqual(room.hub.slots(), [admin.token, ann.token, None, None])
        status, text, _ = await bob.call('state')
        self.assertEqual(status, 401)
        self.assertIn('removed you', text)
        self.assertFalse((await bob.resume())[1]['remembered'], 'a refresh does not bring them back')
        self.assertEqual((await admin.call('seats', dict(action='kick', player=ids['Host'])))[0], 400)

    async def test_a_move_is_kept_for_the_next_refresh(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        await admin.call('seats', dict(action='seat', player=ids['Ann'], slot=3))
        await ann.call('leave', dict(unload=True))
        self.assertEqual((await ann.resume())[1]['slot'], 3)

    async def test_moves_work_while_a_game_runs(self):
        room, admin, ann, bob, ids = await self.room_of_three()
        game = next(g for g in self.library.games.values() if g.console == 'ps1')
        self.assertEqual((await admin.call('game', dict(id=game.id)))[0], 200)
        self.assertEqual((await admin.call('seats', dict(action='swap', a=0, b=2)))[0], 200)
        self.assertEqual(room.hub.slots()[0], bob.token)
        self.assertEqual(room.session.title, game.name)


class SessionsFileTests(TestCase):
    def test_a_broken_or_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'devices.json'
            self.assertEqual(Sessions(path, 'qa-canyon').records, {})
            path.write_text('{not json')
            self.assertEqual(Sessions(path, 'qa-canyon').records, {})

    def test_records_are_capped(self):
        clock = Clock()
        sessions = Sessions(None, 'qa-canyon', clock=clock)
        for _ in range(510):
            clock.now += 1
            sessions.issue('player', name='x')
        self.assertEqual(len(sessions.records), 500)

    def test_no_admin_pin_issues_no_admin_login(self):
        sessions = Sessions(None, 'qa-canyon')
        with self.assertRaises(ValueError):
            sessions.issue('admin')
        self.assertIsNone(sessions.check('admin', 'x'*43))
