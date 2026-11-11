from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection

from inputs import InputHub
import server
from server import Room, host_ice_servers, make_app
from session import Session

ICE = [{'urls': ['stun:192.0.2.10:3478',
                 'turn:192.0.2.10:3478?transport=tcp',
                 'turns:host.example.ts.net:5349?transport=tcp'],
        'username': 'sofa', 'credential': 'test-password'},
       {'urls': 'turn:192.0.2.11:3478', 'username': 'other', 'credential': 'other'}]


def session():
    fake = Mock(thread=None, sample_rate=44100)
    fake.video_frame.return_value = np.zeros((480, 640, 3), np.uint8)
    return Session(InputHub(2), fake, 'Demo')


class HostIceServerTests(IsolatedAsyncioTestCase):
    def test_host_keeps_stun_and_drops_turn(self):
        self.assertEqual(host_ice_servers(ICE), [{'urls': ['stun:192.0.2.10:3478']}])
        self.assertEqual(host_ice_servers([]), [])

    async def test_browser_gets_the_ice_servers_and_the_host_gets_no_turn(self):
        relay_only = ICE[1:]  # a STUN check to a test address waits 5 s
        room = Room(session(), 'qa-canyon', relay_only)
        async with TestClient(TestServer(make_app(room))) as client:
            response = await client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))
            self.assertEqual(response.status, 200)
            joined = await response.json()
            self.assertEqual(joined['iceServers'], relay_only)

            browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.addAsyncCleanup(browser.close)
            browser.addTransceiver('video', direction='recvonly')
            browser.addTransceiver('audio', direction='recvonly')
            browser.createDataChannel('controls')
            await browser.setLocalDescription(await browser.createOffer())
            configs = []
            def recording(configuration):
                configs.append(configuration)
                return RTCPeerConnection(configuration)
            with patch.object(server, 'RTCPeerConnection', recording):
                response = await client.post('/api/offer', headers={'Authorization': 'Bearer '+joined['token']},
                                             json=dict(type='offer', sdp=browser.localDescription.sdp))
            self.assertEqual(response.status, 200)
            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].iceServers, [])
