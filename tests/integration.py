"""Exercise actual emulator media and independent controls through both transports.

Run from the project root: .venv/bin/python tests/integration.py
Requires the supplied disc in runtime/disc plus development-only Playwright.
Optional: QA_DISC (CUE path), QA_CHROMIUM (browser binary; default /usr/bin/chromium
if present, else Playwright's own), QA_TRANSPORT (webrtc or websocket; default
webrtc with QA_ICE_CONFIG, else websocket) and QA_EVIDENCE (output directory).
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from playwright.async_api import async_playwright

from emulator import Emulator
from fetch_core import core_filename
from inputs import InputHub
from server import Room, make_app
from session import Session
import tracer


async def until(predicate, timeout=5):
    start = time.monotonic()
    while not predicate():
        if time.monotonic()-start > timeout:
            raise AssertionError('Condition timed out')
        await asyncio.sleep(.03)


async def main():
    evidence = Path(os.environ.get('QA_EVIDENCE', ROOT/'runtime/qa'))
    evidence.mkdir(parents=True, exist_ok=True)
    disc = Path(os.environ['QA_DISC']) if 'QA_DISC' in os.environ else next((ROOT/'runtime/disc').glob('*.cue'))
    chromium = os.environ.get('QA_CHROMIUM', '/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None)
    emulator = Emulator(ROOT/'runtime/cores'/core_filename(), disc,
                        ROOT/'runtime/system', ROOT/'runtime/test-saves', InputHub())
    emulator.start()
    ice_path = os.environ.get('QA_ICE_CONFIG')
    ice = json.loads(Path(ice_path).read_text()) if ice_path else []
    # A policy-managed Chromium can block direct UDP; an unmanaged browser does not.
    webrtc = os.environ.get('QA_TRANSPORT', 'webrtc' if ice else 'websocket') == 'webrtc'
    room = Room(Session(emulator.hub, emulator, disc.stem), 'qa-canyon', ice)
    runner = web.AppRunner(make_app(room))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    result = dict(passed=False, core=emulator.status(), browser_errors=[])
    peers, browsers = [], []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url+'/api/state') as response:
                assert response.status == 401
            async with session.post(url+'/api/join', json=dict(name='No', pin='wrong')) as response:
                assert response.status == 401
            async with session.post(url+'/api/join', json=dict(name='No', pin='qa-canyon'),
                                    headers={'Origin': 'https://another-origin.invalid'}) as response:
                assert response.status == 403
            async with session.get(url+'/runtime/disc/'+disc.name) as response:
                assert response.status == 404

            # Native WebRTC clients validate the real RTP encoder and input
            # channel without changing the managed browser's UDP policy.
            rtc_results = []
            for n in range(2):
                async with session.post(url+'/api/join', json=dict(name=f'RTC {n+1}', pin='qa-canyon')) as response:
                    token = (await response.json())['token']
                headers = {'Authorization': 'Bearer '+token}
                peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                peers.append(peer)
                video_ready, audio_ready, opened = asyncio.Event(), asyncio.Event(), asyncio.Event()
                tracks = {}
                def on_track(track, tracks=tracks, vr=video_ready, ar=audio_ready):
                    tracks[track.kind] = track
                    (vr if track.kind == 'video' else ar).set()
                peer.on('track', on_track)
                peer.addTransceiver('video', direction='recvonly')
                peer.addTransceiver('audio', direction='recvonly')
                channel = peer.createDataChannel('controls', ordered=False, maxRetransmits=0)
                channel.on('open', opened.set)
                await peer.setLocalDescription(await peer.createOffer())
                async with session.post(url+'/api/offer', headers=headers,
                                        json=dict(type='offer', sdp=peer.localDescription.sdp)) as response:
                    assert response.status == 200, await response.text()
                    answer = await response.json()
                await peer.setRemoteDescription(RTCSessionDescription(**answer))
                await asyncio.wait_for(opened.wait(), 10)
                await asyncio.wait_for(video_ready.wait(), 10)
                await asyncio.wait_for(audio_ready.wait(), 10)
                frame = await asyncio.wait_for(tracks['video'].recv(), 10)
                later = await asyncio.wait_for(tracks['video'].recv(), 10)
                audio = await asyncio.wait_for(tracks['audio'].recv(), 10)
                assert later.pts > frame.pts and frame.width == 640 and frame.height == 480
                assert audio.samples > 0
                async with session.post(url+'/api/claim', headers=headers, json=dict(slot=n)) as response:
                    assert response.status == 200, await response.text()
                channel.send(json.dumps(dict(t='input', seq=1, mask=1 << n)))
                await until(lambda n=n: emulator.hub.snapshot()[n] == 1 << n)
                await until(lambda n=n: emulator.hub.last_polled[n] == 1 << n)
                rtc_results.append(dict(peer=n+1, width=frame.width, height=frame.height,
                                        audioSamples=audio.samples, distinctController=True))
                async with session.post(url+'/api/leave', headers=headers, json={}) as response:
                    assert response.status == 200
                await peer.close()
            peers.clear()
            result['webrtc_native_peers'] = rtc_results

            async with async_playwright() as playwright:
                pages = []
                for n in range(2):
                    browser = await playwright.chromium.launch(executable_path=chromium, headless=True,
                                                               args=['--no-sandbox'])
                    browsers.append(browser)
                    page = await browser.new_page(viewport=dict(width=1440, height=1100))
                    pages.append(page)
                    page.on('pageerror', lambda error: result['browser_errors'].append(str(error)))
                    await page.goto(url)
                    await page.locator('#name').fill(f'Browser {n+1}')
                    await page.locator('#pin').fill('qa-canyon')
                    await page.locator('#join').click()
                    await page.wait_for_function('(n) => streamRoom.player===n', arg=n+1, timeout=25000)
                    await page.wait_for_function('() => streamRoom.frames>5', timeout=15000)
                    if not webrtc:
                        await page.wait_for_function('() => streamRoom.audioPackets>5', timeout=15000)
                    else:
                        await page.wait_for_function("() => document.querySelector('#rtc-audio').readyState>=2", timeout=15000)
                a, b = pages
                # Skip the original intro with real controller events, then
                # require actual non-black game imagery before taking screenshots.
                for key in ('Enter', 'KeyX'):
                    await a.keyboard.down(key)
                    await asyncio.sleep(.15)
                    await a.keyboard.up(key)
                    await asyncio.sleep(.5)
                await until(lambda: emulator.video_frame().std() > 10, timeout=40)
                await asyncio.sleep(.3)
                transport = await a.evaluate('streamRoom.transport')
                assert transport == ('webrtc' if webrtc else 'websocket')
                await a.keyboard.down('KeyX')
                await b.keyboard.down('KeyZ')
                await until(lambda: emulator.hub.snapshot()[:2] == [1, 2])
                await until(lambda: emulator.hub.last_polled[:2] == [1, 2])
                owner_b = emulator.hub.slots()[1]
                async with session.post(url+'/api/claim', headers={'Authorization':'Bearer '+owner_b},
                                        json=dict(slot=0)) as response:
                    assert response.status == 409
                assert emulator.hub.slots()[1] == owner_b
                await a.evaluate("window.dispatchEvent(new Event('blur'))")
                await until(lambda: emulator.hub.snapshot()[:2] == [0, 2])
                await a.keyboard.up('KeyX')
                await b.keyboard.up('KeyZ')
                await until(lambda: emulator.hub.snapshot()[:2] == [0, 0])
                await a.evaluate("window.dispatchEvent(new Event('focus'))")
                await a.locator('#sound').click()
                await a.wait_for_function("() => document.querySelector('#sound').textContent==='Mute sound'", timeout=5000)
                if webrtc:
                    assert await a.evaluate("document.querySelector('#video').srcObject.getAudioTracks().length===0 && document.querySelector('#rtc-audio').srcObject.getAudioTracks().length===1 && !document.querySelector('#rtc-audio').muted")
                    await a.wait_for_function("() => streamRoom.diagnostics.receiverBufferMs!==null", timeout=10000)
                await a.locator('#connection-details').evaluate('(element) => element.open=true')
                await a.screenshot(path=str(evidence/('two-browser-webrtc-qa.png' if webrtc else 'two-browser-qa.png')))
                result['browser'] = dict(transport=transport, simultaneousControllers=True, slotStealingRejected=True,
                                          blurClearsOnlyOwnInput=True, audioPackets=await a.evaluate('streamRoom.audioPackets'),
                                          soundToggle=True, decodedFrames=await a.evaluate('streamRoom.frames'))
                result['browser']['diagnostics'] = await a.evaluate('streamRoom.diagnostics')
                result['browser']['separateAudioPlayback'] = webrtc
                await b.locator('#leave').click()
                await until(lambda: emulator.hub.slots()[1] is None)
                # A real mobile touch event holds Cross and Left together.
                mobile_context = await browsers[1].new_context(viewport=dict(width=390, height=844),
                                                               is_mobile=True, has_touch=True)
                mobile = await mobile_context.new_page()
                mobile.on('pageerror', lambda error: result['browser_errors'].append(str(error)))
                await mobile.goto(url)
                await mobile.locator('#name').fill('Touch driver')
                await mobile.locator('#pin').fill('qa-canyon')
                await mobile.locator('#join').click()
                await mobile.wait_for_function('() => streamRoom.player===2', timeout=25000)
                await mobile.wait_for_function('() => streamRoom.frames>3', timeout=15000)
                cdp = await mobile_context.new_cdp_session(mobile)
                await mobile.locator('[data-button="0"]').scroll_into_view_if_needed()
                boxes = [await mobile.locator(f'[data-button="{n}"]').bounding_box() for n in (0, 6)]
                points = [dict(x=box['x']+box['width']/2, y=box['y']+box['height']/2, id=n+1) for n, box in enumerate(boxes)]
                await cdp.send('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=points))
                await until(lambda: emulator.hub.snapshot()[1] == 65)
                await cdp.send('Input.dispatchTouchEvent', dict(type='touchEnd', touchPoints=[]))
                await until(lambda: emulator.hub.snapshot()[1] == 0)
                assert await mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await mobile.locator('#connection-details').evaluate('(element) => element.open=true')
                assert await mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await mobile.screenshot(path=str(evidence/('mobile-webrtc-qa.png' if webrtc else 'mobile-qa.png')), full_page=True)
                result['mobile'] = dict(viewport='390x844', genuineMultitouch=True, noHorizontalOverflow=True)
                # Confirm release, reconnect and reclaim on an existing page.
                await mobile.locator('#leave').click()
                await until(lambda: emulator.hub.slots()[1] is None)
                await b.locator('#join').click()
                await b.wait_for_function('() => streamRoom.player===2', timeout=25000)
                result['reconnect'] = True
                assert not result['browser_errors'], result['browser_errors']
                for browser in browsers:
                    await browser.close()
                browsers.clear()
            result['access_checks'] = ['room code', 'bearer session', 'same origin', 'disc not served']
            result['final_host'] = emulator.status()
            result['nonblack_game_imagery'] = bool(emulator.video_frame().std() > 10)
            result['passed'] = True
    finally:
        for peer in peers:
            await peer.close()
        for browser in browsers:
            await browser.close()
        await runner.cleanup()
        (evidence/('integration-webrtc.json' if webrtc else 'integration.json')).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    tracer.supervise()
    asyncio.run(main())
