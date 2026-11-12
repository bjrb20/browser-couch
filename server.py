#!/usr/bin/env python3
"""One original game session, streamed to authenticated browser player slots."""
import argparse
import asyncio
from collections import defaultdict, deque
import errno
import hmac
import io
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import time

from PIL import Image

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

import covers as cover_art
from covers import Covers
from emulator import Emulator
from fetch_core import core_filename
from inputs import InputHub
from library import Library
import metadata
from media import GameAudio, GameVideo
from session import Launcher, Session
from systems import PROFILES
import tracer

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('stream')


def port_range(text):
    """Parse --udp-ports, for example 40000-40199."""
    try:
        first, last = map(int, text.split('-'))
    except ValueError:
        raise argparse.ArgumentTypeError('Use FIRST-LAST, for example 40000-40199') from None
    if not 1024 <= first <= last <= 65535:
        raise argparse.ArgumentTypeError('Use ports from 1024 to 65535, with FIRST <= LAST')
    return range(first, last+1)


def bind_udp_in(ports):
    """Bind WebRTC host candidates inside `ports`, so a firewall can allow just that range.

    aioice binds every host candidate to port 0 and has no option for it. Only
    those binds use port 0 here; mDNS and TURN sockets keep their own ports.
    """
    loop = asyncio.get_running_loop()
    create = loop.create_datagram_endpoint
    async def ranged(factory, local_addr=None, **kwargs):
        if not local_addr or local_addr[1] != 0:
            return await create(factory, local_addr=local_addr, **kwargs)
        for port in ports:
            try:
                return await create(factory, local_addr=(local_addr[0], port), **kwargs)
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        # aioice skips the candidate; the client then uses the WebSocket stream.
        raise OSError(errno.EADDRINUSE, f'No free UDP port in {ports.start}-{ports.stop-1}')
    loop.create_datagram_endpoint = ranged


def host_ice_servers(servers):
    """The ICE servers for the host's own peer connections: STUN only.

    The browser needs TURN to reach the host. The host does not: a TURN
    allocation adds nothing on the relay host and waits 5 s when the relay is down.
    """
    host = []
    for server in servers:
        urls = [server['urls']] if isinstance(server['urls'], str) else server['urls']
        stun = [url for url in urls if url.startswith(('stun:', 'stuns:'))]
        if stun:
            host.append(dict(urls=stun))
    return host


class Room:
    def __init__(self, session, pin, ice_servers=None, udp_ports=None, library=None, launcher=None,
                 admin_pin=None, covers=None, providers=(), http=None):
        self.udp_ports = udp_ports
        self.covers = covers
        self.cover_lock = asyncio.Lock()
        self.fetcher = metadata.Fetcher(covers, list(providers), http or metadata.Http(), self.cover_lock) \
            if covers and covers.root and providers else None
        self.library = library
        self.launcher = launcher
        self.admin_pin = admin_pin
        self.scanned_at = None
        self.scan_lock = asyncio.Lock()
        self.session = session
        self.hub = session.hub
        self.pin = pin
        self.ice_servers = ice_servers or []
        self.host_ice_servers = [RTCIceServer(**item) for item in host_ice_servers(self.ice_servers)]
        self.relay = MediaRelay()
        self.video, self.audio = GameVideo(session), GameAudio(session)
        self.clients = {}
        self.attempts = defaultdict(deque)
        self.cleaner = None
        self.jpeg_lock = asyncio.Lock()
        self.jpeg_at = 0
        self.jpeg_data = b''
        self.sockets = set()

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if token not in self.clients:
            raise web.HTTPUnauthorized(text='Join the room first')
        self.clients[token]['seen'] = time.monotonic()
        return token, self.clients[token]

    def throttle(self, key, what):
        """The attempts from one address in the last minute; refuse a 13th. Callers add them."""
        now = time.monotonic()
        # Bounded rate-limit table; forwarding proxies can supply one shared IP.
        if len(self.attempts) > 2048:
            self.attempts.clear()
        attempts = self.attempts[key]
        while attempts and attempts[0] < now-60:
            attempts.popleft()
        if len(attempts) >= 12:
            raise web.HTTPTooManyRequests(text=f'Too many {what} attempts. Wait a minute.')
        return attempts

    def can_pick(self, client):
        return bool(self.library) and (not self.admin_pin or client['admin'])

    async def join(self, request):
        now = time.monotonic()
        attempts = self.throttle(request.remote, 'join')
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid join request')
        pin = body.get('pin', '')
        if not isinstance(pin, str) or not hmac.compare_digest(pin.encode(), self.pin.encode()):
            attempts.append(now)
            raise web.HTTPUnauthorized(text='The room code is incorrect')
        if len(self.clients) >= 12:
            raise web.HTTPServiceUnavailable(text='The room is full')
        name = body.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 24:
            raise web.HTTPBadRequest(text='Enter a name of 1–24 characters')
        token = secrets.token_urlsafe(32)
        self.clients[token] = dict(name=name.strip(), seen=now, pc=None, tracks=[], channel=None, ws=None,
                                  admin=False)
        return web.json_response(dict(token=token, iceServers=self.ice_servers))

    async def state(self, request):
        token, client = self.authenticate(request)
        slots = [dict(number=n+1, name=self.clients[owner]['name'] if owner in self.clients else None,
                      yours=owner == token) for n, owner in enumerate(self.hub.slots())]
        captured = getattr(self.session, 'frame_at', None)
        return web.json_response(dict(slots=slots, host=self.session.status(),
                                      inputPollMs=self.hub.timing(token),
                                      sourceFrameAgeMs=max(0, (time.monotonic()-captured)*1000) if captured else None,
                                      viewers=len(self.clients), canPick=self.can_pick(client),
                                      adminPin=bool(self.admin_pin) and bool(self.library)))

    async def games(self, request):
        """The game library: console names, titles and ids only."""
        self.authenticate(request)
        if not self.library:
            return web.json_response(dict(consoles=[]))
        async with self.scan_lock:
            await self.rescan()
            listing = await asyncio.to_thread(self.with_covers, self.library.public())
        return web.json_response(dict(listing, bootScreen=bool(self.launcher and self.launcher.boot_screen),
                                      covers=bool(self.covers and self.covers.root),
                                      metadata=bool(self.fetcher)))

    async def rescan(self):
        """Scan the library at most every 10 seconds. The caller holds scan_lock."""
        if self.scanned_at is None or time.monotonic()-self.scanned_at > 10:
            await asyncio.to_thread(self.library.scan)
            self.scanned_at = time.monotonic()

    def with_covers(self, listing):
        """The listing with each game's cover version, or None, for the page's cache, and any fetched details.

        No paths, and no provider names or notes except for players who may change covers.
        """
        games = self.library.games

        def item(entry):
            game = games.get(entry['id'])
            if not self.covers or not game:
                return dict(entry, cover=None)
            record = self.covers.record(game.id) or {}
            about = record.get('about')
            entry = dict(entry, cover=self.covers.info(game), about=about if isinstance(about, dict) and about else None)
            if self.fetcher:
                status = self.fetcher.status(game.id)
                entry['fetch'] = dict(state=status['state'], note=status['note'] or record.get('note'),
                                      fetched=bool(record))
            return entry
        return dict(listing, consoles=[dict(console, games=[item(entry) for entry in console['games']])
                                       for console in listing['consoles']])

    def cover_game(self, game_id):
        game = self.library.games.get(game_id) if self.library and isinstance(game_id, str) else None
        if not game:
            raise web.HTTPNotFound(text='That game is no longer in the library')
        return game

    async def cover(self, request):
        """A game's cover as a JPEG, by game id. Every player in the room may see covers."""
        self.authenticate(request)
        game = self.cover_game(request.query.get('id'))
        data = await asyncio.to_thread(self.covers.read, game) if self.covers else None
        if not data:
            raise web.HTTPNotFound(text='No cover for this game')
        return web.Response(body=data, content_type='image/jpeg')

    def cover_admin(self, request):
        """Changing covers takes the same right as choosing games: the server checks it, not only the page."""
        _, client = self.authenticate(request)
        if not self.library:
            raise web.HTTPNotFound(text='This host has no game library')
        if not self.can_pick(client):
            raise web.HTTPForbidden(text='Enter the PIN to change covers')
        if not self.covers or not self.covers.root:
            raise web.HTTPNotFound(text='This host has no covers folder')
        # Every change counts here, not only failures: 12 a minute is plenty for a person.
        self.throttle(('cover', request.remote), 'cover change').append(time.monotonic())

    async def cover_upload(self, request):
        """Store an uploaded cover: the request body is the image file itself."""
        self.cover_admin(request)
        game = self.cover_game(request.query.get('id'))
        if (request.content_length or 0) > cover_art.MAX_UPLOAD:
            raise web.HTTPRequestEntityTooLarge(max_size=cover_art.MAX_UPLOAD, actual_size=request.content_length,
                                                text='Use an image of at most 5 MB')
        data = bytearray()
        async for chunk in request.content.iter_chunked(64*1024):
            data += chunk
            if len(data) > cover_art.MAX_UPLOAD:
                raise web.HTTPRequestEntityTooLarge(max_size=cover_art.MAX_UPLOAD, actual_size=len(data),
                                                    text='Use an image of at most 5 MB')
        # One decode at a time, off the event loop: the emulator needs the CPU more.
        async with self.cover_lock:
            try:
                clean = await asyncio.to_thread(cover_art.encode, bytes(data))
            except cover_art.BadImage as error:
                raise web.HTTPUnsupportedMediaType(text=str(error)) from None
            await asyncio.to_thread(self.covers.save, game.id, clean)
        return web.json_response(dict(ok=True, cover=await asyncio.to_thread(self.covers.info, game)))

    async def cover_remove(self, request):
        """Remove an uploaded cover. A cover.jpg in the library shows again."""
        self.cover_admin(request)
        body = await request.json()
        game = self.cover_game(body.get('id') if isinstance(body, dict) else None)
        await asyncio.to_thread(self.covers.remove, game.id)
        return web.json_response(dict(ok=True, cover=await asyncio.to_thread(self.covers.info, game)))

    async def cover_fetch(self, request):
        """Queue online art and details: {id} for one game, again if it has them, or {missing: true}."""
        self.cover_admin(request)
        if not self.fetcher:
            raise web.HTTPNotFound(text='This host fetches no art: set SOFA_METADATA to turn it on')
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid fetch request')
        if body.get('missing') is True:
            async with self.scan_lock:
                await self.rescan()
            games = await asyncio.to_thread(self.fetcher.missing, list(self.library.games.values()))
        else:
            games = [self.cover_game(body.get('id'))]
        return web.json_response(dict(ok=True, queued=self.fetcher.request(games)))

    async def admin(self, request):
        """Unlock game picking for this player with the --admin-pin."""
        _, client = self.authenticate(request)
        attempts = self.throttle(('admin', request.remote), 'PIN')
        body = await request.json()
        pin = body.get('pin', '') if isinstance(body, dict) else ''
        if not self.admin_pin or not isinstance(pin, str) or \
                not hmac.compare_digest(pin.encode(), self.admin_pin.encode()):
            attempts.append(time.monotonic())
            raise web.HTTPForbidden(text='The PIN is incorrect')
        client['admin'] = True
        return web.json_response(dict(ok=True))

    def picker(self, request):
        _, client = self.authenticate(request)
        if not self.library:
            raise web.HTTPNotFound(text='This host has no game library')
        if not self.can_pick(client):
            raise web.HTTPForbidden(text='Enter the PIN to choose games')
        if self.session.lock.locked():
            raise web.HTTPConflict(text='A game is starting. Wait a moment.')

    async def pick(self, request):
        """Unload the running game, if any, and start a library game. Players keep their slots."""
        self.picker(request)
        body = await request.json()
        game = self.library.games.get(body.get('id')) if isinstance(body, dict) else None
        if not game:
            raise web.HTTPNotFound(text='That game is no longer in the library')
        async with self.session.lock:
            await self.session.unload()
            _, players, _ = self.launcher.plan(game)
            self.hub.resize(players)
            try:
                boot = body.get('bootScreen')
                emulator = self.launcher.emulator(game, self.hub, boot if isinstance(boot, bool) else None)
                await self.session.load(emulator, game.name, game.id)
            except RuntimeError as error:
                print(error, flush=True)
                raise web.HTTPInternalServerError(text=str(error)) from error
        return web.json_response(dict(ok=True))

    async def unpick(self, request):
        """Stop the game and go back to the picker. The memory card is saved as the game stops."""
        self.picker(request)
        async with self.session.lock:
            await self.session.unload()
        return web.json_response(dict(ok=True))

    async def claim(self, request):
        token, client = self.authenticate(request)
        if not ((client['channel'] and client['channel'].readyState == 'open') or
                (client['ws'] and not client['ws'].closed)):
            raise web.HTTPConflict(text='Wait for the stream to connect')
        body = await request.json()
        try:
            self.hub.claim(token, body.get('slot'))
        except (ValueError, AttributeError) as error:
            raise web.HTTPConflict(text=str(error)) from error
        return web.json_response(dict(ok=True))

    async def release(self, request):
        token, _ = self.authenticate(request)
        self.hub.release(token)
        return web.json_response(dict(ok=True))

    async def close_peer(self, client):
        pc, client['pc'] = client['pc'], None
        client['channel'] = None
        for track in client['tracks']:
            track.stop()
        client['tracks'] = []
        if pc:
            await pc.close()

    async def remove(self, token):
        client = self.clients.pop(token, None)
        self.hub.forget(token)
        if client:
            await self.close_peer(client)
            if client['ws']:
                await client['ws'].close()

    async def leave(self, request):
        token, _ = self.authenticate(request)
        await self.remove(token)
        return web.json_response(dict(ok=True))

    async def offer(self, request):
        token, client = self.authenticate(request)
        if client['pc'] or client['ws']:
            raise web.HTTPConflict(text='Already connected; leave before reconnecting')
        body = await request.json()
        if not isinstance(body, dict) or body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
            raise web.HTTPBadRequest(text='Invalid WebRTC offer')
        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.host_ice_servers))
        client['pc'] = pc
        tracks = [self.relay.subscribe(self.video, buffered=False),
                  self.relay.subscribe(self.audio, buffered=False)]
        client['tracks'] = tracks
        for track in tracks:
            # Separate stream ids stop browsers delaying video for lip sync.
            pc.addTrack(track)._stream_id = secrets.token_hex(8)

        @pc.on('datachannel')
        def datachannel(channel):
            if channel.label != 'controls' or client['channel']:
                channel.close()
                return
            client['channel'] = channel

            @channel.on('message')
            def message(raw):
                if not isinstance(raw, str) or len(raw) > 512 or token not in self.clients:
                    return
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        return
                    client['seen'] = time.monotonic()
                    if data.get('t') == 'input':
                        self.hub.update(token, data.get('seq'), data.get('mask'), data.get('axes'))
                    elif data.get('t') == 'ping' and type(data.get('sent')) in (int, float):
                        channel.send(json.dumps(dict(t='pong', sent=data['sent'])))
                except (ValueError, TypeError):
                    return

            @channel.on('close')
            def channel_close():
                self.hub.clear(token)

        @pc.on('connectionstatechange')
        async def statechange():
            if pc.connectionState == 'failed':
                self.hub.release(token)
                await self.close_peer(client)
            elif pc.connectionState == 'closed':
                self.hub.release(token)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=body['sdp'], type='offer'))
            await asyncio.wait_for(pc.setLocalDescription(await pc.createAnswer()), 25)
            return web.json_response(dict(sdp=pc.localDescription.sdp, type=pc.localDescription.type))
        except Exception:
            await self.close_peer(client)
            log.exception('Could not negotiate browser stream')
            raise web.HTTPBadRequest(text='Stream negotiation failed; check the host network and ICE settings')

    async def jpeg(self):
        async with self.jpeg_lock:
            if time.monotonic()-self.jpeg_at >= 1/20:
                pixels = self.session.video_frame()
                def encode():
                    image = Image.fromarray(pixels).resize((640, 480), Image.Resampling.NEAREST)
                    buffer = io.BytesIO()
                    image.save(buffer, format='JPEG', quality=68)
                    return b'\x01'+buffer.getvalue()
                self.jpeg_data = await asyncio.to_thread(encode)
                self.jpeg_at = time.monotonic()
            return self.jpeg_data

    async def socket(self, request):
        """HTTP-only fallback: bounded JPEG/PCM playback when UDP cannot connect."""
        if len(self.sockets) >= 16:
            raise web.HTTPServiceUnavailable(text='Too many connections')
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=4096)
        await ws.prepare(request)
        self.sockets.add(ws)
        token, client, audio, tasks = None, None, None, []
        try:
            first = await asyncio.wait_for(ws.receive_json(), 5)
            token = first.get('token') if isinstance(first, dict) else None
            client = self.clients.get(token)
            if not client or client['ws']:
                await ws.close(code=1008, message=b'Join the room first')
                return ws
            # A failed WebRTC negotiation may have left an unconnected peer.
            await self.close_peer(client)
            client['ws'] = ws
            client['seen'] = time.monotonic()
            audio = self.relay.subscribe(self.audio, buffered=False)
            await ws.send_json(dict(t='ready', sampleRate=self.audio.rate))

            async def video_loop():
                while not ws.closed:
                    if request.transport and request.transport.get_write_buffer_size() < 256*1024:
                        await ws.send_bytes(await self.jpeg())
                    await asyncio.sleep(1/20)

            async def audio_loop():
                while not ws.closed:
                    frame = await audio.recv()
                    if request.transport and request.transport.get_write_buffer_size() > 512*1024:
                        await ws.close(code=1013, message=b'Connection too slow for fallback stream')
                        return
                    await ws.send_bytes(b'\x02'+bytes(frame.planes[0])[:frame.samples*4])

            tasks = [asyncio.create_task(video_loop()), asyncio.create_task(audio_loop())]
            # An encoder or send failure must terminate the room connection.
            def completed(task):
                if not task.cancelled() and task.exception():
                    log.warning('Fallback stream stopped: %s', task.exception())
                    asyncio.create_task(ws.close(code=1011, message=b'Stream stopped'))
            for task in tasks:
                task.add_done_callback(completed)
            async for event in ws:
                if event.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(event.data)
                    if not isinstance(data, dict):
                        continue
                    client['seen'] = time.monotonic()
                    if data.get('t') == 'input':
                        self.hub.update(token, data.get('seq'), data.get('mask'), data.get('axes'))
                    elif data.get('t') == 'ping' and type(data.get('sent')) in (int, float):
                        await ws.send_json(dict(t='pong', sent=data['sent']))
                except (ValueError, TypeError):
                    pass
        except (asyncio.TimeoutError, ValueError, TypeError):
            await ws.close(code=1008, message=b'Authentication required')
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if audio:
                audio.stop()
            self.sockets.discard(ws)
            if client and client['ws'] is ws:
                client['ws'] = None
                self.hub.release(token)
        return ws

    async def sweep(self):
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for token, client in list(self.clients.items()):
                if now-client['seen'] > 30:
                    await self.remove(token)

    async def start(self, app):
        if self.udp_ports:
            bind_udp_in(self.udp_ports)
        self.cleaner = asyncio.create_task(self.sweep())

    async def stop(self, app):
        if self.cleaner:
            self.cleaner.cancel()
        if self.fetcher:
            await self.fetcher.close()
        await asyncio.gather(*(self.remove(token) for token in list(self.clients)))
        self.video.stop()
        self.audio.stop()
        async with self.session.lock:
            await self.session.unload()


LOOPBACK = (ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'))


def proxy_networks(text):
    """--trusted-proxy: comma-separated IPs or CIDRs, added to loopback."""
    networks = []
    for item in filter(None, (part.strip() for part in text.split(','))):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise argparse.ArgumentTypeError(f'Not an IP address or CIDR: {item}') from None
    return tuple(networks)


def trusted_peer(remote, networks):
    try:
        address = ipaddress.ip_address(remote or '')
    except ValueError:
        return False
    address = getattr(address, 'ipv4_mapped', None) or address
    return any(address in network for network in (*LOOPBACK, *networks))


def forwarded(networks=()):
    """Middleware that applies an HTTPS proxy's forwarded headers, from trusted peers only.

    A proxy (tailscale serve on this host, or one on the LAN) speaks plain HTTP
    to us. From any other peer the headers are ignored, so a browser cannot
    forge its scheme, host or address.
    """
    @web.middleware
    async def middleware(request, handler):
        if trusted_peer(request.remote, networks):
            changes = {}
            proto = request.headers.get('X-Forwarded-Proto', '').rsplit(',', 1)[-1].strip()
            if proto in ('http', 'https'):
                changes['scheme'] = proto
            host = request.headers.get('X-Forwarded-Host', '').rsplit(',', 1)[-1].strip()
            if host:
                changes['host'] = host
            peer = request.headers.get('X-Forwarded-For', '').rsplit(',', 1)[-1].strip()
            if peer:
                changes['remote'] = peer
            if changes:
                request = request.clone(**changes)
        return await handler(request)
    return middleware


local_proxy = forwarded()


@web.middleware
async def same_origin(request, handler):
    origin = request.headers.get('Origin')
    if (request.method == 'POST' or request.path == '/api/socket') and origin and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden(text='Use the host page to join')
    try:
        response = await handler(request)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text='Invalid JSON')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'sha256-vBOUmmOOBseWYF7umUPs8lyA/gWt60gVw1o2t5RUrQM='; style-src 'self'; media-src 'self' blob:; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'"
    return response


def make_app(room, trusted_proxies=()):
    app = web.Application(middlewares=[forwarded(trusted_proxies), same_origin], client_max_size=256*1024)
    for route, handler in [('join', room.join), ('offer', room.offer), ('claim', room.claim),
                           ('release', room.release), ('leave', room.leave), ('admin', room.admin),
                           ('game', room.pick), ('game/stop', room.unpick), ('cover', room.cover_upload),
                           ('cover/remove', room.cover_remove), ('cover/fetch', room.cover_fetch)]:
        app.router.add_post('/api/' + route, handler)
    app.router.add_get('/api/state', room.state)
    app.router.add_get('/api/library', room.games)
    app.router.add_get('/api/cover', room.cover)
    app.router.add_get('/api/socket', room.socket)
    async def index(request):
        return web.FileResponse(ROOT / 'web/index.html')
    app.router.add_get('/', index)
    app.router.add_static('/static/', ROOT / 'web', show_index=False)
    app.on_startup.append(room.start)
    app.on_cleanup.append(room.stop)
    return app


def main():
    tracer.supervise()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console', choices=sorted(PROFILES), default='ps1',
                        help='System profile: ps1, ps2 (PCSX2, needs a BIOS) or ps2-play (Play!, OpenGL, Linux)')
    parser.add_argument('--core', help="Libretro core; default: the console's core in runtime/cores")
    parser.add_argument('--disc', help='PS1: the original CUE with all referenced BIN tracks. '
                        'PS2: an ISO/CHD/CSO/CUE disc image or a homebrew ELF. '
                        'Omit it to start the room with no game')
    parser.add_argument('--library', type=Path, metavar='DIR',
                        help='Game folder with one subfolder per console: ps1, ps2')
    parser.add_argument('--system', default=str(ROOT/'runtime/system'))
    parser.add_argument('--saves', default=str(ROOT/'runtime/saves'))
    parser.add_argument('--covers', default=str(ROOT/'runtime/covers'),
                        help='Writable folder for the cover art players with the admin PIN upload')
    parser.add_argument('--metadata', default=os.environ.get('SOFA_METADATA') or None, metavar='PROVIDER,...',
                        help='Off by default. Online cover art and details for library games, fetched when an '
                             'admin asks: libretro, screenscraper or both, in the order to try them '
                             '(default: $SOFA_METADATA). ScreenScraper reads its credentials from the environment')
    parser.add_argument('--state', help='Optional host-side emulator snapshot to restore')
    parser.add_argument('--multitap', choices=PROFILES['ps1'].multitaps,
                        help='Default: port 1 on PS1; on PS2, port 1 for more than two players')
    parser.add_argument('--players', type=int, choices=range(2, 5), help='Default: 4 for PS1, 2 for PS2')
    parser.add_argument('--core-option', action='append', default=[], metavar='KEY=VALUE',
                        help='Override a core option, for example pcsx2_renderer=OpenGL')
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 to accept LAN browsers')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--pin', default=os.environ.get('SOFA_PIN') or None,
                        help='Room access code; default: $SOFA_PIN, else generated')
    parser.add_argument('--boot-screen', action=argparse.BooleanOptionalAction, default=True,
                        help="Library games: play the console's boot screen before the game, where the core "
                             'can (PS1 with a real BIOS, PS2 on PCSX2). Players can still untick it per game')
    parser.add_argument('--admin-pin', default=os.environ.get('SOFA_ADMIN_PIN') or None,
                        help='PIN a player enters before choosing games (default: $SOFA_ADMIN_PIN); '
                             'without it, every player in the room may choose')
    parser.add_argument('--ice-config', type=Path, help='JSON array of standard WebRTC ICE servers; default LAN-only host candidates')
    parser.add_argument('--udp-ports', type=port_range, metavar='FIRST-LAST',
                        help='Bind WebRTC media to this UDP port range, for a firewall or tailnet ACL rule')
    parser.add_argument('--trusted-proxy', type=proxy_networks, default=(), metavar='IP_OR_CIDR,...',
                        help='HTTPS reverse proxies whose X-Forwarded-Proto/-Host/-For headers the host '
                             'trusts, besides this host itself (loopback, always trusted)')
    parser.add_argument('--cert', help='HTTPS certificate PEM')
    parser.add_argument('--key', help='HTTPS private key PEM')
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error('--cert and --key must be supplied together')
    profile = PROFILES[args.console]
    if args.multitap and not profile.multitap:
        parser.error(f'{profile.key} has no multitap option')
    players = args.players or profile.default_players
    if players > profile.max_players:
        parser.error(f'{profile.key} supports at most {profile.max_players} players')
    if not args.multitap and profile.multitap and players > 2:
        args.multitap = 'port 1'
    if args.multitap == 'disabled' and players > 2:
        parser.error('More than two players require a multitap')
    if any('=' not in option for option in args.core_option):
        parser.error('Use --core-option KEY=VALUE')
    pin = args.pin or secrets.token_hex(4)
    if len(pin) < 6:
        parser.error('Use a room code of at least six characters')
    if args.state and not args.disc:
        parser.error('--state needs --disc')
    if args.library and not args.library.is_dir():
        parser.error(f'No library folder at {args.library}')
    if args.admin_pin is not None and not args.library:
        parser.error('--admin-pin needs --library')
    if args.admin_pin is not None and len(args.admin_pin) < 4:
        parser.error('Use an admin PIN of at least four characters')
    library = Library(args.library, args.system, ROOT/'runtime/cores').scan() if args.library else None
    http = metadata.Http()
    providers, warnings = metadata.providers(args.metadata, http) if library else ([], [])
    for warning in warnings:
        print(f'Metadata: {warning}', flush=True)
    ice = json.loads(args.ice_config.read_text()) if args.ice_config else []
    logging.basicConfig(level=logging.WARNING)
    context = None
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
    hub = InputHub(players)
    session = Session(hub, start_disc(parser, args, profile, hub) if args.disc else None,
                      Path(args.disc).stem if args.disc else None)
    if session.emulator:
        emulator = session.emulator
        print(f'Sofa Link host | {emulator.name} {emulator.version} | {emulator.renderer} video', flush=True)
    else:
        print('Sofa Link host | no game loaded', flush=True)
    print(f"Metadata: {', '.join(provider.name for provider in providers)}, when an admin asks"
          if providers else 'Metadata: off', flush=True)
    if library:
        for console in library.consoles:
            count = f"{len(console['games'])} games" if console['available'] else 'cannot run'
            print(f"Library: {console['name']}: {count}"
                  + (f" | {library.warnings[console['id']]}" if library.warnings[console['id']] else ''), flush=True)
    # A code from the environment stays out of the log, which others may read.
    print('Room code: set by SOFA_PIN' if pin == os.environ.get('SOFA_PIN') else f'Room code: {pin}', flush=True)
    print('One shared game. Ctrl+C stops the host and saves the memory card.', flush=True)
    web.run_app(make_app(Room(
        session, pin, ice, args.udp_ports, library,
        Launcher(args.system, args.saves, ROOT/'runtime/cores', args.players, tuple(args.core_option),
                 args.boot_screen),
        args.admin_pin, Covers(args.covers, args.library) if library else None, providers, http),
        args.trusted_proxy), host=args.host, port=args.port, ssl_context=context, access_log=None)


def start_disc(parser, args, profile, hub):
    """Check and start the --disc game; exit with a message on a missing core or BIOS."""
    missing = profile.missing_bios(args.system)
    if missing and profile.bios_required:
        parser.error(missing)
    elif missing:
        print(missing, flush=True)
    tapped = profile.multitap_core and args.multitap not in (None, 'disabled')
    core = args.core or ROOT/'runtime/cores'/core_filename(core=profile.multitap_core if tapped else profile.core)
    if not Path(core).is_file() and tapped and not args.core:
        parser.error(f'No core at {core}. A PS2 multitap needs it. Build it with: ./tools/build-pcsx2-multitap.sh')
    if not Path(core).is_file():
        parser.error(f'No core at {core}. Download it with: ./setup.sh --console {profile.key}')
    emulator = Emulator(core, args.disc, args.system, args.saves, hub, args.multitap, args.state,
                        profile, args.core_option)
    emulator.start()
    return emulator


if __name__ == '__main__':
    main()
