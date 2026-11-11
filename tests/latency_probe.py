"""Measure input-to-decoded/rendered-frame delay using a synthetic 50Hz core.

This exercises real room transports/codecs and the actual browser controls. It
does not measure original-game response, hardware display latency or a WAN.
"""
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
import numpy as np
from playwright.async_api import async_playwright

from inputs import InputHub
from server import Room, make_app
from session import Session


class ProbeCore:
    def __init__(self):
        self.hub = InputHub()
        self.sample_rate = 44100
        self.value = 0
        self.frames = 0
        self.task = None
        self.thread = None  # Session.unload checks the emulator thread
        self.audio_read = time.monotonic()  # silence accrues at the real sample rate

    async def run(self):
        while True:
            self.value = 255 if any(self.hub.snapshot()) else 0
            self.frames += 1
            await asyncio.sleep(.02)

    def video_frame(self):
        return np.full((240, 320, 3), self.value, dtype=np.uint8)

    def audio_queued(self):
        return int((time.monotonic()-self.audio_read)*self.sample_rate)

    def audio_frame(self, count):
        self.audio_read += count/self.sample_rate
        return bytes(count*4)

    def status(self):
        return dict(core='Latency probe', fps=50, frames=self.frames, error=None, multitap='probe')

    def stop(self):
        pass


def summary(values):
    return dict(samples=len(values), medianMs=round(statistics.median(values), 1),
                p95Ms=round(sorted(values)[int((len(values)-1)*.95)], 1),
                maxMs=round(max(values), 1))


async def main():
    core = ProbeCore()
    core.task = asyncio.create_task(core.run())
    ice = json.loads(Path(sys.argv[sys.argv.index('--ice')+1]).read_text()) if '--ice' in sys.argv else []
    runner = web.AppRunner(make_app(Room(Session(core.hub, core, 'Latency probe'), 'probe-latency', ice)))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url+'/api/join', json=dict(name='RTC probe', pin='probe-latency')) as response:
                token = (await response.json())['token']
            headers = {'Authorization':'Bearer '+token}
            tracks = {}
            peer.on('track', lambda track: tracks.update({track.kind:track}))
            channel = peer.createDataChannel('controls', ordered=False, maxRetransmits=0)
            ready = asyncio.Event()
            channel.on('open', ready.set)
            peer.addTransceiver('video', direction='recvonly')
            peer.addTransceiver('audio', direction='recvonly')
            await peer.setLocalDescription(await peer.createOffer())
            async with session.post(url+'/api/offer', headers=headers, json=dict(type='offer', sdp=peer.localDescription.sdp)) as response:
                answer = await response.json()
            await peer.setRemoteDescription(RTCSessionDescription(**answer))
            await asyncio.wait_for(ready.wait(), 10)
            async with session.post(url+'/api/claim', headers=headers, json=dict(slot=0)) as response:
                assert response.status == 200
            latest = {'value':None, 'at':0}
            async def receive_video():
                while True:
                    frame = await tracks['video'].recv()
                    latest.update(value=int(frame.to_ndarray(format='rgb24')[100,100,0]), at=time.monotonic())
            consumer = asyncio.create_task(receive_video())
            await asyncio.sleep(.6)
            values = []
            for n in range(24):
                desired = n % 2 == 0
                start = time.monotonic()
                channel.send(json.dumps(dict(t='input', seq=n+1, mask=int(desired))))
                while (latest['value'] is None or (latest['value'] > 127) != desired or latest['at'] < start):
                    assert time.monotonic()-start < 3, 'WebRTC frame did not reflect input'
                    await asyncio.sleep(.001)
                values.append((latest['at']-start)*1000)
                await asyncio.sleep(.04)
            result['nativeWebRTCInputToDecodedFrame'] = summary(values)
            consumer.cancel()
            await peer.close()
            async with session.post(url+'/api/leave', headers=headers, json={}) as response:
                assert response.status == 200

        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.environ.get('QA_CHROMIUM', '/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None), headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                await page.add_init_script('''
                    window.inputSends=[];
                    const original=WebSocket.prototype.send;
                    WebSocket.prototype.send=function(raw){try{const d=JSON.parse(raw);if(d.t==='input')inputSends.push({mask:d.mask,at:performance.now()});}catch{}return original.call(this,raw);};
                    const rtcSend=RTCDataChannel.prototype.send;
                    RTCDataChannel.prototype.send=function(raw){try{const d=JSON.parse(raw);if(d.t==='input')inputSends.push({mask:d.mask,at:performance.now()});}catch{}return rtcSend.call(this,raw);};
                ''')
                if '--client' in sys.argv:
                    source = Path(sys.argv[sys.argv.index('--client')+1]).read_text()
                    await page.route('**/static/client.mjs', lambda route: route.fulfill(body=source, content_type='text/javascript'))
                await page.goto(url)
                await page.locator('#name').fill('Browser probe')
                await page.locator('#pin').fill('probe-latency')
                await page.locator('#join').click()
                await page.wait_for_function('() => streamRoom.player === 1 && streamRoom.frames > 5', timeout=20000)
                expected = os.environ.get('QA_TRANSPORT', 'webrtc' if ice else 'websocket')
                assert await page.evaluate('streamRoom.transport') == expected
                if '--sound' in sys.argv:
                    await page.locator('#sound').click()
                    await page.wait_for_function("() => document.querySelector('#sound').textContent==='Mute sound'", timeout=5000)
                await page.evaluate('''() => {
                    window.probe={events:[],draws:[]};
                    const c=document.querySelector('#fallback'),ctx=c.getContext('2d'),v=document.querySelector('#video');
                    const probeCanvas=document.createElement('canvas');probeCanvas.width=probeCanvas.height=1;const probeContext=probeCanvas.getContext('2d');
                    let old=-1;
                    function sample(){if(streamRoom.transport==='webrtc'&&v.videoWidth)probeContext.drawImage(v,0,0,1,1);const pixels=streamRoom.transport==='webrtc'?probeContext.getImageData(0,0,1,1):ctx.getImageData(320,240,1,1);const value=pixels.data[0]>127?1:0;if(value!==old){probe.draws.push({value,at:performance.now()});old=value;}requestAnimationFrame(sample);}sample();
                    addEventListener('keydown',e=>{if(e.code==='KeyK')probe.events.push({value:1,at:performance.now()});},true);
                    addEventListener('keyup',e=>{if(e.code==='KeyK')probe.events.push({value:0,at:performance.now()});},true);
                }''')
                await page.evaluate('() => document.activeElement?.blur()')  # keys reach the game, not the form
                for n in range(24):
                    desired = int(n % 2 == 0)
                    if desired:
                        await page.keyboard.down('KeyK')
                    else:
                        await page.keyboard.up('KeyK')
                    await page.wait_for_function('''value => {
                        const e=probe.events.at(-1),d=probe.draws.at(-1);
                        return e&&d&&d.value===value&&d.at>=e.at;
                    }''', arg=desired, timeout=4000)
                    await asyncio.sleep(.04)
                values = await page.evaluate('''() => probe.events.map(e=>probe.draws.find(d=>d.at>=e.at&&d.value===e.value).at-e.at)''')
                result['browser'+('WebRTC' if ice else 'Fallback')+'KeyEventToImage'] = summary(values)
                sent = await page.evaluate('''() => probe.events.map(e=>inputSends.find(s=>s.at>=e.at&&s.mask===e.value).at-e.at)''')
                result['browserKeyEventToInputSend'] = summary(sent)
                result['soundEnabled'] = '--sound' in sys.argv
                result['browserDiagnostics'] = await page.evaluate('streamRoom.diagnostics || null')
            finally:
                await browser.close()
        result['scope'] = 'Local synthetic 50Hz input response; not original game or physical display latency'
        print(json.dumps(result), flush=True)
    finally:
        await peer.close()
        core.task.cancel()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
