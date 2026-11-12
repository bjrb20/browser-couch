"""A page reconnecting after a drop (a phone that slept or switched apps) keeps its connection and slot."""
import asyncio
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection
import numpy as np

from inputs import InputHub
from server import Room, make_app
from session import Session
from sessions import Sessions


def session():
    fake = Mock(thread=None, sample_rate=44100)
    fake.video_frame.return_value = np.zeros((480, 640, 3), np.uint8)
    return Session(InputHub(4), fake, 'Demo')


async def until(check, timeout=5):
    for _ in range(int(timeout*20)):
        if check():
            return
        await asyncio.sleep(.05)
    raise AssertionError('timed out')


class ReconnectTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        sessions = Sessions(Path(self.temporary.name)/'remembered-devices.json', 'qa-canyon', '4321')
        self.room = Room(session(), 'qa-canyon', admin_pin='4321', sessions=sessions)
        self.client = TestClient(TestServer(make_app(self.room)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        response = await self.client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))
        self.token = (await response.json())['token']
        self.auth = {'Authorization': 'Bearer '+self.token}

    async def offer(self):
        """One browser stream: a real peer connection's offer, as the page sends it."""
        browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.addAsyncCleanup(browser.close)
        browser.addTransceiver('video', direction='recvonly')
        browser.addTransceiver('audio', direction='recvonly')
        browser.createDataChannel('controls')
        await browser.setLocalDescription(await browser.createOffer())
        response = await self.client.post('/api/offer', headers=self.auth,
                                          json=dict(type='offer', sdp=browser.localDescription.sdp))
        return response.status

    async def test_a_new_offer_replaces_the_old_stream_and_keeps_the_slot(self):
        self.assertEqual(await self.offer(), 200)
        client = self.room.clients[self.token]
        first = client['pc']
        self.room.hub.claim(self.token, 2)
        self.room.hub.update(self.token, 1, 1)

        self.assertEqual(await self.offer(), 200, 'the same page may reconnect')
        self.assertIsNot(client['pc'], first)
        await until(lambda: first.connectionState == 'closed')
        await asyncio.sleep(.1)  # the old peer's close events have run
        self.assertEqual(self.room.hub.slot_of(self.token), 2)
        self.assertEqual(self.room.hub.snapshot()[2], 0, 'a held button is let go across the gap')
        self.assertIn(self.token, self.room.clients)

    async def test_a_new_socket_replaces_an_old_one_and_keeps_the_slot(self):
        old = await self.client.ws_connect('/api/socket')
        await old.send_json(dict(token=self.token))
        self.assertEqual((await old.receive_json())['t'], 'ready')
        self.room.hub.claim(self.token, 1)

        new = await self.client.ws_connect('/api/socket')
        self.addAsyncCleanup(new.close)
        await new.send_json(dict(token=self.token))
        self.assertEqual((await new.receive_json())['t'], 'ready')
        async def closing():
            while (await old.receive()).type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                pass  # frames sent before the swap
        await asyncio.wait_for(closing(), 5)
        self.assertEqual(self.room.hub.slot_of(self.token), 1)
        self.assertIsNotNone(self.room.clients[self.token]['ws'])

    async def test_a_socket_after_a_peer_keeps_the_slot(self):
        self.assertEqual(await self.offer(), 200)
        first = self.room.clients[self.token]['pc']
        self.room.hub.claim(self.token, 0)
        ws = await self.client.ws_connect('/api/socket')
        self.addAsyncCleanup(ws.close)
        await ws.send_json(dict(token=self.token))
        self.assertEqual((await ws.receive_json())['t'], 'ready')
        await until(lambda: first.connectionState == 'closed')
        await asyncio.sleep(.1)
        self.assertEqual(self.room.hub.slot_of(self.token), 0)

    async def test_a_dropped_connection_may_reconnect_but_an_ended_one_may_not(self):
        await self.room.remove(self.token)  # as when a phone is away too long
        response = await self.client.get('/api/state', headers=self.auth)
        self.assertEqual(response.status, 401)
        self.assertNotIn('X-Sofa-Ended', response.headers, 'the page tries to rejoin by itself')

        response = await self.client.post('/api/join', json=dict(pin='qa-canyon', name='Bo'))
        bo = (await response.json())['token']
        self.room.note_gone(bo, 'The host removed you from the room.')
        await self.room.remove(bo)
        response = await self.client.get('/api/state', headers={'Authorization': 'Bearer '+bo})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers.get('X-Sofa-Ended'), 'yes', 'the page stays out')
        self.assertEqual(await response.text(), 'The host removed you from the room.')

    async def test_a_remembered_device_away_too_long_gets_its_slot_back(self):
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as phone:
            url = self.client.make_url
            async with phone.post(url('/api/join'), json=dict(pin='qa-canyon', name='Cy')) as response:
                token = (await response.json())['token']
            self.room.clients[token]['channel'] = Mock(readyState='open')
            async with phone.post(url('/api/claim'), json=dict(slot=3),
                                  headers={'Authorization': 'Bearer '+token}) as response:
                self.assertEqual(response.status, 200, await response.text())
            await self.room.remove(token)  # the sweep, 30 s after the phone went quiet
            self.assertIsNone(self.room.hub.slots()[3])
            async with phone.post(url('/api/resume'), json=dict(rejoin=True)) as response:
                body = await response.json()
            self.assertEqual((body['name'], body['slot']), ('Cy', 3))
            self.assertEqual(self.room.hub.slots()[3], body['token'])
