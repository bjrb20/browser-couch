import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest import IsolatedAsyncioTestCase, TestCase
import urllib.request
from unittest.mock import Mock

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from inputs import InputHub
from media import GameAudio, GameVideo
from server import Room, make_app
from session import IDLE_RATE, Session

ROOT = Path(__file__).resolve().parents[1]


class IdleSessionTests(IsolatedAsyncioTestCase):
    def test_idle_session_streams_a_placeholder_and_silence(self):
        session = Session(InputHub(4))
        self.assertEqual(session.video_frame().shape, (480, 640, 3))
        self.assertEqual(session.sample_rate, IDLE_RATE)
        self.assertEqual(session.audio_queued(), 0)
        self.assertEqual(session.audio_frame(882), bytes(882*4))
        self.assertIsNone(session.frame_at)
        status = session.status()
        self.assertIsNone(status['game'])
        self.assertIsNone(status['error'])

    async def test_tracks_run_with_no_game(self):
        session = Session(InputHub(4))
        video, audio = GameVideo(session), GameAudio(session, stall=.02)
        self.assertEqual((await video.recv()).width, 640)
        frame = await audio.recv()
        self.assertEqual(frame.sample_rate, IDLE_RATE)
        self.assertEqual(bytes(frame.planes[0])[:882*4], bytes(882*4))

    def test_running_session_passes_the_emulator_through(self):
        emulator = Mock(sample_rate=48000, frame_at=3.)
        emulator.status.return_value = dict(core='PCSX-ReARMed', error=None)
        session = Session(InputHub(2), emulator, 'Demo')
        self.assertEqual(session.sample_rate, 48000)
        self.assertEqual(session.frame_at, 3.)
        self.assertEqual(session.status(), dict(core='PCSX-ReARMed', error=None, game=dict(title='Demo', id=None, runningS=0), loading=None))

    async def test_players_join_and_claim_slots_in_an_idle_room(self):
        room = Room(Session(InputHub(4)), 'qa-canyon')
        async with TestClient(TestServer(make_app(room))) as client:
            joined = await (await client.post('/api/join', json=dict(pin='qa-canyon', name='Ann'))).json()
            auth = dict(Authorization='Bearer '+joined['token'])
            state = await (await client.get('/api/state', headers=auth)).json()
            self.assertIsNone(state['host']['game'])
            self.assertEqual([slot['name'] for slot in state['slots']], [None]*4)
            room.hub.claim(joined['token'], 2)  # claim also needs a live stream; the hub is the same
            state = await (await client.get('/api/state', headers=auth)).json()
            self.assertEqual(state['slots'][2], dict(number=3, name='Ann', yours=True))


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class NoDiscStartTests(TestCase):
    def test_host_starts_without_a_disc(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as empty:
            # No core and no BIOS: with no game, the host must not need either.
            host = subprocess.Popen([sys.executable, str(ROOT/'server.py'), '--port', str(port), '--pin', 'qa-canyon',
                                     '--system', empty, '--saves', empty],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                self.assertIn('no game loaded', host.stdout.readline())
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api/join', method='POST',
                                                 data=json.dumps(dict(pin='qa-canyon', name='Ann')).encode(),
                                                 headers={'Content-Type': 'application/json'})
                for _ in range(100):
                    try:
                        joined = json.load(urllib.request.urlopen(request, timeout=2))
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.fail('The host did not accept a join')
                state = json.load(urllib.request.urlopen(urllib.request.Request(
                    f'http://127.0.0.1:{port}/api/state', headers=dict(Authorization='Bearer '+joined['token'])), timeout=2))
                self.assertIsNone(state['host']['game'])
            finally:
                host.terminate()
                host.wait(10)
                host.stdout.close()

    def test_state_still_needs_a_disc(self):
        result = subprocess.run([sys.executable, str(ROOT/'server.py'), '--state', 'snap.state'],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--state needs --disc', result.stderr)
