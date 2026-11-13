"""Simulate a network that blocks UDP, with the synthetic core of latency_probe.py.

Chromium starts with --force-webrtc-ip-handling-policy=disable_non_proxied_udp,
so it can use UDP only through a TURN relay. The probe joins from the real page
on the LAN address and prints the transport and the selected candidate pair.

  tests/turn_probe.py --ice runtime/turn/ice.json --expect webrtc     # relay runs
  tests/turn_probe.py --ice runtime/turn/ice.json --expect websocket  # relay stopped

--host sets the LAN address. The default is the address in the turn: URL.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))

from aiohttp import web
from playwright.async_api import async_playwright

from latency_probe import ProbeCore
from server import Room, make_app
from session import Session

SELECTED_PAIR = '''async () => {
    const stats = await streamRoom.pc.getStats(), byId = new Map();
    stats.forEach(stat => byId.set(stat.id, stat));
    const transport = [...byId.values()].find(stat => stat.type === 'transport');
    const pair = byId.get(transport?.selectedCandidatePairId) ||
        [...byId.values()].find(stat => stat.type === 'candidate-pair' && stat.nominated && stat.state === 'succeeded');
    if (!pair) return null;
    const local = byId.get(pair.localCandidateId), remote = byId.get(pair.remoteCandidateId);
    return {local: {type: local.candidateType, protocol: local.protocol, relayProtocol: local.relayProtocol ?? null,
                    url: local.url ?? null},
            remote: {type: remote.candidateType, protocol: remote.protocol},
            bytesReceived: pair.bytesReceived};
}'''


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ice', type=Path, required=True)
    parser.add_argument('--expect', choices=('webrtc', 'websocket'), required=True)
    parser.add_argument('--host')
    args = parser.parse_args()
    ice = json.loads(args.ice.read_text())
    host = args.host or ice[0]['urls'][0].split(':')[1]
    core = ProbeCore()
    core.task = asyncio.create_task(core.run())
    runner = web.AppRunner(make_app(Room(Session(core.hub, core, 'Relay probe'), 'probe-office', ice)))
    await runner.setup()
    site = web.TCPSite(runner, host, 0)
    await site.start()
    url = f'http://{host}:{site._server.sockets[0].getsockname()[1]}'
    result = dict(expected=args.expect)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=os.environ.get('QA_CHROMIUM'), headless=True,
                args=['--no-sandbox', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            try:
                page = await browser.new_page()
                await page.goto(url)
                await page.locator('#name').fill('Office probe')
                await page.locator('#pin').fill('probe-office')
                await page.locator('#join').click()
                await page.wait_for_function('() => streamRoom.player === 1 && streamRoom.frames > 5', timeout=60000)
                result['transport'] = await page.evaluate('streamRoom.transport')
                if result['transport'] == 'webrtc':
                    await page.wait_for_function(f'({SELECTED_PAIR})', timeout=10000)
                    result['selectedPair'] = await page.evaluate(SELECTED_PAIR)
            finally:
                await browser.close()
    finally:
        core.task.cancel()
        await runner.cleanup()
    print(json.dumps(result), flush=True)
    passed = result['transport'] == args.expect
    if args.expect == 'webrtc':
        local = result.get('selectedPair', {}).get('local', {})
        passed = passed and local.get('type') == 'relay' and local.get('relayProtocol') in ('tcp', 'tls')
    print('PASS' if passed else 'FAIL', flush=True)
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    asyncio.run(main())
