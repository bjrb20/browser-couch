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
import math
import logging
import os
from pathlib import Path
import re
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
from session import Launcher, Session, multitap_default
from sessions import ADMIN_FOR, PLAYER_FOR, Sessions
from systems import PROFILES
import tracer

ROOT = Path(__file__).resolve().parent
CHOOSING_FOR = 15  # seconds an open picker counts without news from it; the page reports every 5
# Roughly how long each console's boot screen plays before the game's own first screen.
BOOT_SECONDS = dict(ps1=12, ps2=16)
STARTED_FOR = 3  # seconds "Starting" stays after the game is up, when there is no boot screen to wait for
STOPPED_FOR = 6  # seconds everyone sees that a game was stopped
# The chooser's picker, shown read-only to everyone else.
MIRROR_FOR = 5  # seconds a mirror lasts without news; the page sends at least every 2
MIRROR_PICKED = 3  # seconds a pick stays picked out for viewers before "Starting" takes over
MIRROR_RATE = 25  # updates a second from the chooser, at most; the page sends at most 20
MIRROR_SEARCH = 40  # characters of search text
LAYOUT = re.compile(r'[pd][0-9]{1,2}')  # phone or desktop, and how many columns of cards
# Remembered devices (see sessions.py): HttpOnly, so no script on the page can read them.
PLAYER_COOKIE, ADMIN_COOKIE = 'sofa_link_player', 'sofa_link_admin'
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
                 admin_pin=None, covers=None, providers=(), http=None, sessions=None):
        self.udp_ports = udp_ports
        self.sessions = sessions or Sessions(None, pin, admin_pin)
        self.gone = {}  # token -> why it was ended, for the page's next request
        self.covers = covers
        self.cover_lock = asyncio.Lock()
        self.fetcher = metadata.Fetcher(covers, list(providers), http or metadata.Http(), self.cover_lock) \
            if covers and covers.root and providers else None
        self.library = library
        self.launcher = launcher
        self.admin_pin = admin_pin
        self.multitaps = {}  # game id -> multitap choice, when there is no covers folder to keep it in
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
        self.closing = set()  # old sockets of pages that reconnected, closing in the background
        self.choosers = {}  # token -> {until}: players with the game picker open
        # The chooser's picker, for others to watch. Its number only grows, even across restarts, so a page
        # can drop an update older than one it has.
        # Each choosing session has its own id, so a page can keep one dismissed until the next.
        self.mirror, self.mirror_n, self.sends = None, time.time_ns() // 1_000_000, set()
        self.change = None  # {kind: starting or stopped, game, by, until}: what everyone sees as games change

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if token not in self.clients:
            # A connection the host ended on purpose says so, and the page must not rejoin by itself.
            # Any other (idle too long, a restart) may: a remembered device comes straight back.
            why = self.gone.get(token)
            raise web.HTTPUnauthorized(text=why or 'Join the room first',
                                       headers={'X-Sofa-Ended': 'yes'} if why else None)
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
        admin = self.admin_cookie(request)
        token, client = self.add_client(name.strip(), admin=admin)
        response = web.json_response(dict(token=token, iceServers=self.ice_servers, name=client['name'],
                                          admin=bool(admin)))
        old = self.sessions.check('player', request.cookies.get(PLAYER_COOKIE))
        if body.get('remember', True) is not False:
            cookie, client['session'] = self.sessions.issue('player', name=client['name'], slot=None)
            set_cookie(request, response, PLAYER_COOKIE, cookie, PLAYER_FOR)
        else:
            response.del_cookie(PLAYER_COOKIE, path='/')
        self.sessions.revoke(old)
        return response

    def add_client(self, name, session=None, admin=None):
        """A connection to the room. `session` and `admin` are the device's remembered logins, if any."""
        token = secrets.token_urlsafe(32)
        self.clients[token] = dict(name=name, seen=time.monotonic(), pc=None, tracks=[], channel=None, ws=None,
                                   admin=bool(admin), id=secrets.token_hex(6), session=session, admin_session=admin)
        return token, self.clients[token]

    def admin_cookie(self, request):
        """The key of this device's live admin login, or None."""
        return self.sessions.check('admin', request.cookies.get(ADMIN_COOKIE)) if self.admin_pin else None

    def note_gone(self, token, why):
        if len(self.gone) > 256:
            self.gone.clear()
        self.gone[token] = why

    async def resume(self, request):
        """Rejoin with this device's remembered login, after a refresh or with a reopened tab: {rejoin}.

        A device that chose Leave is not taken back in until it asks to rejoin. Its old connection, if the
        page never said goodbye, gives way, and the new one gets the same player slot if it is free.
        """
        body = await request.json() if request.can_read_body else {}
        cookie = request.cookies.get(PLAYER_COOKIE)
        key = self.sessions.check('player', cookie)
        if not key:
            response = web.json_response(dict(remembered=False, expired=bool(cookie)))
            if cookie:
                self.throttle(request.remote, 'join').append(time.monotonic())
                response.del_cookie(PLAYER_COOKIE, path='/')
            return response
        record = self.sessions.get(key)
        if record.get('left') and not (isinstance(body, dict) and body.get('rejoin') is True):
            return web.json_response(dict(remembered=True, name=record['name']))
        wanted = record.get('slot')
        for old, client in list(self.clients.items()):
            if client['session'] == key:
                held = self.hub.slot_of(old)
                wanted = held if held is not None else wanted
                self.note_gone(old, 'This device rejoined the room from another tab.')
                await self.remove(old)
        if len(self.clients) >= 12:
            raise web.HTTPServiceUnavailable(text='The room is full')
        admin = self.admin_cookie(request)
        token, client = self.add_client(record['name'], session=key, admin=admin)
        slot = None
        if type(wanted) is int and wanted < self.hub.players:
            try:
                self.hub.seat(token, wanted)
                slot = wanted
            except ValueError:
                pass
        self.sessions.update(key, left=False, slot=slot if slot is not None else wanted)
        self.sessions.touch(key)
        response = web.json_response(dict(remembered=True, token=token, iceServers=self.ice_servers,
                                          name=client['name'], slot=slot, wanted=wanted, admin=bool(admin)))
        set_cookie(request, response, PLAYER_COOKIE, cookie, PLAYER_FOR)
        self.push_seats()
        return response

    async def forget(self, request):
        """Log out and forget this device: both remembered logins end, and its connection leaves."""
        keys = [self.sessions.check('player', request.cookies.get(PLAYER_COOKIE)), self.admin_cookie(request)]
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        if token in self.clients:
            keys += [self.clients[token]['session'], self.clients[token]['admin_session']]
            await self.remove(token)
        self.sessions.revoke(*keys)
        response = web.json_response(dict(ok=True))
        response.del_cookie(PLAYER_COOKIE, path='/')
        response.del_cookie(ADMIN_COOKIE, path='/')
        return response

    def sync_seats(self):
        """Each remembered player's slot, for their next rejoin."""
        for token, client in self.clients.items():
            if client['session']:
                self.sessions.update(client['session'], slot=self.hub.slot_of(token))

    def push(self, data, skip=None):
        """Send a small message to every page over the channel it already has."""
        text = json.dumps(data, separators=(',', ':'))
        for token, client in list(self.clients.items()):
            if token != skip:
                self.send(client, text)

    def send(self, client, text):
        channel, ws = client['channel'], client['ws']
        try:
            if channel and channel.readyState == 'open':
                channel.send(text)
            elif ws and not ws.closed:
                task = asyncio.get_running_loop().create_task(ws.send_str(text))
                self.sends.add(task)
                task.add_done_callback(self.sends.discard)
        except (ConnectionError, RuntimeError):
            pass

    def push_seats(self):
        """Slots changed: every page asks for the room state now, not at its next poll."""
        self.push(dict(t='seats'))

    def is_admin(self, client):
        return bool(self.admin_pin) and client['admin']

    async def seats(self, request):
        """An admin moves players between slots, live: {action: seat, player, slot} puts a player in a free
        slot (slot null: just watching; replace: true takes a taken slot, and its player watches); {action: swap, a, b} swaps two slots; {action: free, slot};
        {action: kick, player} removes a player and forgets their device.

        Only a player who entered the admin PIN may. Input follows the slot at the next poll.
        """
        token, client = self.authenticate(request)
        if not self.is_admin(client):
            raise web.HTTPForbidden(text='Enter the host PIN to move players')
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid request')
        action = body.get('action')
        players = {entry['id']: owner for owner, entry in self.clients.items()}
        target = players.get(body.get('player')) if isinstance(body.get('player'), str) else None
        if action in ('seat', 'kick') and not target:
            raise web.HTTPNotFound(text='That player has left the room')
        try:
            if action == 'seat':
                slot = body.get('slot')
                if body.get('replace') is True and type(slot) is int and 0 <= slot < self.hub.players:
                    occupant = self.hub.slots()[slot]
                    if occupant and occupant != target:
                        self.hub.seat(occupant, None)  # a watcher takes the slot; its player watches
                self.hub.seat(target, slot)
            elif action == 'swap':
                self.hub.swap(body.get('a'), body.get('b'))
            elif action == 'free':
                slot = body.get('slot')
                if type(slot) is not int or not 0 <= slot < self.hub.players:
                    raise ValueError('Choose a valid player slot')
                owner = self.hub.slots()[slot]
                if owner:
                    self.hub.seat(owner, None)
            elif action == 'kick':
                if target == token:
                    raise web.HTTPBadRequest(text='Use Leave to leave the room yourself')
                gone = self.clients[target]
                self.sessions.revoke(gone['session'], gone['admin_session'])
                self.note_gone(target, 'The host removed you from the room.')
                self.send(gone, json.dumps(dict(t='removed')))
                await self.remove(target)
            else:
                raise web.HTTPBadRequest(text='Unknown action')
        except ValueError as error:
            raise web.HTTPConflict(text=str(error)) from error
        self.sync_seats()
        self.push_seats()
        return web.json_response(dict(ok=True))

    async def state(self, request):
        token, client = self.authenticate(request)
        slots = [dict(number=n+1, name=self.clients[owner]['name'] if owner in self.clients else None,
                      yours=owner == token) for n, owner in enumerate(self.hub.slots())]
        captured = getattr(self.session, 'frame_at', None)
        admin = self.is_admin(client)
        roster = [dict(id=entry['id'], name=entry['name'], slot=self.hub.slot_of(owner), admin=self.is_admin(entry),
                       you=owner == token) for owner, entry in self.clients.items()] if admin else None
        return web.json_response(dict(slots=slots, host=self.session.status(), admin=admin, roster=roster,
                                      remembered=bool(client['session']),
                                      inputPollMs=self.hub.timing(token),
                                      sourceFrameAgeMs=max(0, (time.monotonic()-captured)*1000) if captured else None,
                                      viewers=len(self.clients), canPick=self.can_pick(client),
                                      adminPin=bool(self.admin_pin) and bool(self.library),
                                      choosing=self.choosing(token), change=self.changing(),
                                      mirror=self.mirror_view(token)))

    def choosing(self, token):
        """Who has the game picker open, by the names the room already shows."""
        now, shown = time.monotonic(), []
        for owner, entry in list(self.choosers.items()):
            client = self.clients.get(owner)
            if not client or entry['until'] < now or not self.can_pick(client):
                del self.choosers[owner]
                continue
            shown.append(dict(name=client['name'], yours=owner == token))
        return shown

    def changing(self):
        """The game that is starting, or the one that just stopped, until its moment has passed."""
        if self.change and self.change['until'] is not None and self.change['until'] < time.monotonic():
            self.change = None
        return {key: value for key, value in self.change.items() if key != 'until'} if self.change else None

    async def choose(self, request):
        """The picker opened or closed: {open}.

        Only a player who may choose games can say they are choosing. An open picker that stops
        reporting is forgotten after CHOOSING_FOR seconds, so it never sticks.
        """
        token, client = self.authenticate(request)
        calls = client.setdefault('picker_calls', deque())
        now = time.monotonic()
        while calls and calls[0] < now-10:
            calls.popleft()
        if len(calls) >= 30:
            raise web.HTTPTooManyRequests(text='Too many picker updates. Wait a moment.')
        calls.append(now)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get('open'), bool):
            raise web.HTTPBadRequest(text='Invalid picker update')
        if not body['open']:
            self.choosers.pop(token, None)
            return web.json_response(dict(ok=True))
        if not self.can_pick(client):
            raise web.HTTPForbidden(text='Enter the PIN to choose games')
        self.choosers[token] = dict(until=now+CHOOSING_FOR)
        return web.json_response(dict(ok=True))

    def mirror_update(self, token, client, data):
        """The chooser's picker, from the room channel, for everyone else to watch: {s: state or null}.

        Only a player who may choose is shown, and only one at a time: a second open picker is not.
        Only the fields mirror_state keeps are passed on, so nothing else a page sends (a PIN, any
        other field) reaches another player.
        """
        now = time.monotonic()
        calls = client.setdefault('mirror_calls', deque())
        while calls and calls[0] < now-1:
            calls.popleft()
        if len(calls) >= MIRROR_RATE:
            return
        calls.append(now)
        current = self.live_mirror()
        if current and (current['owner'] != token or current['s']['sel']) or self.session.lock.locked():
            return  # someone else is choosing, or a pick is giving way to "Starting" (a late update must not undo it)
        state = data.get('s')
        if state is None:
            if current:
                self.end_mirror()
            return
        if not isinstance(state, dict) or not self.can_pick(client):
            return
        self.mirror_n += 1
        self.mirror = dict(owner=token, name=client['name'], until=now+MIRROR_FOR, n=self.mirror_n,
                           id=current['id'] if current else self.mirror_n, s=self.mirror_state(state))
        self.broadcast_mirror()

    def mirror_state(self, state):
        """The search text, console filter, scroll (0-1), the game under the pointer or focus, the pointer
        within the panel (0-1), the pointer within that game's card (0-1), and the layout."""
        games = self.library.games if self.library else {}
        def fraction(value):
            return round(min(max(float(value), 0.0), 1.0), 3) \
                if type(value) in (int, float) and math.isfinite(value) else None
        def game(value):
            return value if isinstance(value, str) and value in games else None
        def point(value):
            value = [fraction(v) for v in value] if isinstance(value, list) and len(value) == 2 else None
            return value if value and None not in value else None
        search, console, layout = state.get('q'), state.get('c'), state.get('l')
        return dict(q=cover_art.plain(search[:MIRROR_SEARCH], MIRROR_SEARCH) if isinstance(search, str) else '',
                    c=console if isinstance(console, str) and any(g.console == console for g in games.values())
                    else 'all',
                    y=fraction(state.get('y')) or 0, h=game(state.get('h')),
                    p=point(state.get('p')), o=point(state.get('o')),
                    l=layout if isinstance(layout, str) and LAYOUT.fullmatch(layout) else None,
                    sel=None)  # the host sets the selection from the pick itself

    def live_mirror(self):
        """The mirror, or None once it has gone quiet or its chooser left or may no longer choose."""
        mirror = self.mirror
        if mirror and (mirror['until'] < time.monotonic() or mirror['owner'] not in self.clients
                       or not self.can_pick(self.clients[mirror['owner']])):
            if not mirror['s']['sel']:
                self.choosers.pop(mirror['owner'], None)  # an open list reports every 2 seconds: its page has gone
            self.end_mirror()
        return self.mirror

    def end_mirror(self):
        self.mirror = None
        self.mirror_n += 1
        self.broadcast_mirror()

    def end_picked_mirror(self, n):
        """Shortly after a pick, the mirror gives way to "Starting", unless the chooser has looked again."""
        if self.mirror and self.mirror['n'] == n:
            self.end_mirror()

    def connection_lost(self, token):
        """The chooser's connection dropped: everyone stops seeing them choose now, not when it goes quiet."""
        self.choosers.pop(token, None)
        if self.mirror and self.mirror['owner'] == token:
            self.end_mirror()

    def mirror_view(self, token):
        """What the room state says about the mirror; its number lets a page drop an older update."""
        mirror = self.live_mirror()
        yours = bool(mirror) and mirror['owner'] == token
        return dict(n=self.mirror_n, id=mirror['id'] if mirror else None,
                    name=mirror['name'] if mirror else None, yours=yours,
                    s=mirror['s'] if mirror and not yours else None)

    def broadcast_mirror(self):
        """Send the mirror to everyone but the chooser, over the channel each one already has."""
        mirror = self.mirror
        text = json.dumps(dict(t='mirror', n=self.mirror_n, id=mirror['id'] if mirror else None,
                               name=mirror['name'] if mirror else None,
                               s=mirror['s'] if mirror else None), separators=(',', ':'))
        for token, client in self.clients.items():
            if not (mirror and token == mirror['owner']):
                self.send(client, text)

    async def games(self, request):
        """The game library: console names, titles and ids only."""
        _, client = self.authenticate(request)
        if not self.library:
            return web.json_response(dict(consoles=[]))
        async with self.scan_lock:
            await self.rescan()
            listing = await asyncio.to_thread(self.with_covers, self.library.public(), self.can_pick(client))
        return web.json_response(dict(listing, bootScreen=bool(self.launcher and self.launcher.boot_screen),
                                      covers=bool(self.covers and self.covers.root),
                                      metadata=bool(self.fetcher),
                                      matching=self.fetcher.matching() if self.fetcher else None))

    async def rescan(self):
        """Scan the library at most every 10 seconds. The caller holds scan_lock."""
        if self.scanned_at is None or time.monotonic()-self.scanned_at > 10:
            await asyncio.to_thread(self.library.scan)
            self.scanned_at = time.monotonic()

    def with_covers(self, listing, admin=False):
        """The listing with each game's cover version, or None, for the page's cache, and any fetched details.

        No paths. The provider entry a game matched, and the candidates when unsure, only for
        players who may change covers.
        """
        games = self.library.games

        def item(entry):
            game = games.get(entry['id'])
            multitap = self.multitap(game) if game else None
            if multitap is not None:
                entry = dict(entry, multitap=multitap)
            if not self.covers or not game:
                return dict(entry, cover=None)
            record = self.covers.record(game.id) or {}
            about, _ = self.covers.about(game.id)
            entry = dict(entry, cover=self.covers.info(game), about=about or None)
            if self.fetcher:
                status = self.fetcher.status(game.id)
                entry['fetch'] = dict(state=status['state'], note=status['note'] or record.get('note'),
                                      fetched=bool(record))
                if admin and self.fetcher.matching():
                    entry['fetch'].update(match=record.get('match'), candidates=record.get('candidates') or [],
                                          picked=self.covers.matches(game.id))
            return entry
        return dict(listing, consoles=[dict(console, games=[item(entry) for entry in console['games']])
                                       for console in listing['consoles']])

    def multitap(self, game):
        """Whether the game starts on the multitap, or None for a console with no choice.

        The choice last made for the game, else on unless its details say two players at most.
        """
        if not PROFILES[game.profile].multitap_choice:
            return None
        stored = self.covers and self.covers.root
        chosen = self.covers.multitap(game.id) if stored else self.multitaps.get(game.id)
        if chosen is not None:
            return chosen
        about = self.covers.about(game.id)[0] if self.covers else {}
        return multitap_default(about.get('players'))

    async def multitap_choice(self, request):
        """Turn a game's multitap on or off for its next start. It takes the right to choose games."""
        self.picker_rights(request)
        body = await request.json()
        game = self.library.games.get(body.get('id')) if isinstance(body, dict) else None
        if not game:
            raise web.HTTPNotFound(text='That game is no longer in the library')
        if not PROFILES[game.profile].multitap_choice or not isinstance(body.get('on'), bool):
            raise web.HTTPBadRequest(text='This game has no multitap choice')
        if self.covers and self.covers.root:
            await asyncio.to_thread(self.covers.save_multitap, game.id, body['on'])
        else:
            self.multitaps[game.id] = body['on']
        return web.json_response(dict(ok=True, multitap=body['on']))

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

    async def playing(self, request):
        """The running game for the Now playing panel: title, console, cover version and details.

        Only what the library listing already shows players; the slots come from /api/state.
        """
        _, client = self.authenticate(request)
        game_id = getattr(self.session, 'game_id', None)
        game = self.library.games.get(game_id) if self.library and game_id else None
        if not game:
            return web.json_response(dict(game=None))
        console = next((entry['name'] for entry in self.library.consoles if entry['id'] == game.console),
                       game.console.upper())
        if self.covers:
            cover = await asyncio.to_thread(self.covers.info, game)
            about, edited = await asyncio.to_thread(self.covers.about, game.id)
        else:
            cover, about, edited = None, {}, []
        return web.json_response(dict(game=dict(id=game.id, title=game.name, console=console, consoleId=game.console,
                                                cover=cover, about=about, edited=edited),
                                      canEdit=bool(self.can_pick(client) and self.covers and self.covers.root)))

    async def about_edit(self, request):
        """An admin's own description, year or genre for a game. Empty text goes back to the fetched one."""
        self.cover_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid details')
        game = self.cover_game(body.pop('id', None))
        try:
            changes = cover_art.checked_edits(body)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        async with self.cover_lock:
            about, edited = await asyncio.to_thread(self.covers.save_edits, game.id, changes)
        return web.json_response(dict(ok=True, about=about, edited=edited))

    async def match(self, request):
        """An admin's pick of the entry a game is: {id, provider, entry: id, 0 for none, or null for by name}.

        Drops what was fetched for the game and fetches it again. Uploads and edits stay.
        """
        self.cover_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid match')
        game = self.cover_game(body.get('id'))
        provider = next((item for item in (self.fetcher.providers if self.fetcher else [])
                         if item.matches and item.name == body.get('provider')), None)
        if not provider:
            raise web.HTTPNotFound(text='No provider here matches games by name')
        entry = body.get('entry')
        if entry is not None and (type(entry) is not int or not 0 <= entry < 2**31):
            raise web.HTTPBadRequest(text='The match should be an entry number')
        if self.fetcher.status(game.id)['state']:
            raise web.HTTPConflict(text='This game is being fetched: try again in a moment')
        async with self.cover_lock:
            await asyncio.to_thread(self.covers.save_match, game.id, provider.name, entry)
        return web.json_response(dict(ok=True, queued=self.fetcher.request([game])))

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
        response = web.json_response(dict(ok=True))
        if client['session']:  # a remembered device keeps the admin login too, for a shorter time
            old = self.admin_cookie(request)
            cookie, client['admin_session'] = self.sessions.issue('admin')
            set_cookie(request, response, ADMIN_COOKIE, cookie, ADMIN_FOR)
            self.sessions.revoke(old)
        return response

    async def admin_lock(self, request):
        """Give up the admin rights on this device, and its remembered admin login."""
        _, client = self.authenticate(request)
        self.sessions.revoke(client['admin_session'], self.admin_cookie(request))
        client['admin'], client['admin_session'] = False, None
        response = web.json_response(dict(ok=True))
        response.del_cookie(ADMIN_COOKIE, path='/')
        return response

    def picker_rights(self, request):
        _, client = self.authenticate(request)
        if not self.library:
            raise web.HTTPNotFound(text='This host has no game library')
        if not self.can_pick(client):
            raise web.HTTPForbidden(text='Enter the PIN to choose games')

    def picker(self, request):
        self.picker_rights(request)
        if self.session.lock.locked():
            raise web.HTTPConflict(text='A game is starting. Wait a moment.')

    async def pick(self, request):
        """Unload the running game, if any, and start a library game. Players keep their slots."""
        self.picker(request)
        body = await request.json()
        game = self.library.games.get(body.get('id')) if isinstance(body, dict) else None
        if not game:
            raise web.HTTPNotFound(text='That game is no longer in the library')
        token, client = self.authenticate(request)
        async with self.session.lock:
            self.choosers.pop(token, None)
            if self.live_mirror() and self.mirror['owner'] == token:
                # Everyone watching sees the chosen game picked out, then the mirror gives way to "Starting".
                self.mirror_n += 1
                self.mirror.update(until=time.monotonic()+MIRROR_PICKED, n=self.mirror_n,
                                   s=dict(self.mirror['s'], h=game.id, sel=game.id))
                self.broadcast_mirror()
                asyncio.get_running_loop().call_later(MIRROR_PICKED, self.end_picked_mirror, self.mirror_n)
            cover = await asyncio.to_thread(self.covers.info, game) if self.covers else None
            self.change = dict(kind='starting', by=client['name'], until=None,
                               game=dict(id=game.id, title=game.name, console=game.console,
                                         cover=cover['version'] if cover else None))
            await self.session.unload()
            tap = body.get('multitap')
            tap = tap if isinstance(tap, bool) else await asyncio.to_thread(self.multitap, game)
            _, players, _ = self.launcher.plan(game, tap)
            self.hub.resize(players)
            self.sync_seats()
            self.push_seats()
            try:
                boot = body.get('bootScreen')
                boot = boot if isinstance(boot, bool) else None
                emulator = self.launcher.emulator(game, self.hub, boot, tap)
                await self.session.load(emulator, game.name, game.id)
            except RuntimeError as error:
                print(error, flush=True)
                self.change = None
                raise web.HTTPInternalServerError(text=str(error)) from error
            # Everyone keeps seeing "Starting" while the console's boot screen plays.
            booting = BOOT_SECONDS.get(game.console, 12) if self.launcher.boot_options(game, boot) else 0
            self.change['until'] = time.monotonic()+booting+STARTED_FOR
        return web.json_response(dict(ok=True))

    async def unpick(self, request):
        """Stop the game and go back to the picker. The memory card is saved as the game stops."""
        self.picker(request)
        token, client = self.authenticate(request)
        async with self.session.lock:
            title, game_id = self.session.title, self.session.game_id
            await self.session.unload()
            game = self.library.games.get(game_id) if game_id else None
            self.change = dict(kind='stopped', by=client['name'], until=time.monotonic()+STOPPED_FOR,
                               game=dict(id=game.id, title=game.name, console=game.console, cover=None)
                               if game else dict(id=None, title=title, console=None, cover=None)) if title else None
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
        self.sync_seats()
        self.push_seats()
        return web.json_response(dict(ok=True))

    async def release(self, request):
        token, _ = self.authenticate(request)
        self.hub.release(token)
        self.sync_seats()
        self.push_seats()
        return web.json_response(dict(ok=True))

    async def replace_stream(self, token, client):
        """The same page opens a new stream (after a phone sleeps, say): end the old one, keep the slot.

        Held buttons are let go, so nothing stays pressed across the gap.
        """
        if client['pc']:
            client['pc'].replaced = True
        old, client['ws'] = client['ws'], None
        await self.close_peer(client)
        self.hub.clear(token)
        if old:  # not awaited: a socket to a phone that went away may take seconds to close
            task = asyncio.create_task(old.close(code=1000, message=b'Reconnected'))
            self.closing.add(task)
            task.add_done_callback(self.closing.discard)

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
        self.choosers.pop(token, None)
        if self.mirror and self.mirror['owner'] == token:
            self.end_mirror()
        held = self.hub.slot_of(token) is not None
        self.hub.forget(token)
        if held:
            self.push_seats()
        if client:
            await self.close_peer(client)
            if client['ws']:
                await client['ws'].close()

    async def leave(self, request):
        """Leave the room: {unload: true} when the page is closing or reloading, which a rejoin undoes by itself.

        Otherwise the player chose to leave: the device stays remembered, but asks before rejoining.
        """
        token, client = self.authenticate(request)
        body = await request.json() if request.can_read_body else {}
        if not (isinstance(body, dict) and body.get('unload') is True):
            self.sessions.update(client['session'], left=True)
        await self.remove(token)
        return web.json_response(dict(ok=True, remembered=bool(client['session']), name=client['name']))

    async def offer(self, request):
        token, client = self.authenticate(request)
        body = await request.json()
        if not isinstance(body, dict) or body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
            raise web.HTTPBadRequest(text='Invalid WebRTC offer')
        await self.replace_stream(token, client)  # the page reconnecting: its old stream may not know it has gone
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
                    elif data.get('t') == 'mirror':
                        self.mirror_update(token, client, data)
                except (ValueError, TypeError):
                    return

            @channel.on('close')
            def channel_close():
                if getattr(pc, 'replaced', False):
                    return
                self.hub.clear(token)
                if client['channel'] in (channel, None):
                    self.connection_lost(token)

        @pc.on('connectionstatechange')
        async def statechange():
            if getattr(pc, 'replaced', False):
                return  # the same page reconnected: it keeps its slot
            if pc.connectionState == 'failed':
                self.hub.release(token)
                self.connection_lost(token)
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
            if not client:
                await ws.close(code=1008, message=b'Join the room first')
                return ws
            # A failed WebRTC negotiation may have left an unconnected peer; a reconnect, an old stream.
            await self.replace_stream(token, client)
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
                if event.type != web.WSMsgType.TEXT or len(event.data) > 512:
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
                    elif data.get('t') == 'mirror':
                        self.mirror_update(token, client, data)
                except (ValueError, TypeError):
                    pass
        except (asyncio.TimeoutError, ValueError, TypeError):
            await ws.close(code=1008, message=b'Authentication required')
        finally:
            # The bookkeeping first: the handler may itself be cancelled while it waits below.
            self.sockets.discard(ws)
            if client and client['ws'] is ws:
                client['ws'] = None
                self.hub.release(token)
                self.connection_lost(token)
            for task in tasks:
                task.cancel()
            if audio:
                audio.stop()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
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


def set_cookie(request, response, name, value, max_age):
    """A remembered login: HttpOnly, sent to this site only, and over HTTPS only when the page is."""
    response.set_cookie(name, value, max_age=max_age, path='/', httponly=True, samesite='Strict',
                        secure=request.scheme == 'https')


def make_app(room, trusted_proxies=()):
    app = web.Application(middlewares=[forwarded(trusted_proxies), same_origin], client_max_size=256*1024)
    for route, handler in [('join', room.join), ('offer', room.offer), ('claim', room.claim),
                           ('release', room.release), ('leave', room.leave), ('admin', room.admin),
                           ('game', room.pick), ('game/stop', room.unpick), ('cover', room.cover_upload),
                           ('cover/remove', room.cover_remove), ('cover/fetch', room.cover_fetch),
                           ('game/about', room.about_edit), ('game/match', room.match),
                           ('game/multitap', room.multitap_choice), ('picker', room.choose),
                           ('resume', room.resume), ('forget', room.forget), ('admin/lock', room.admin_lock),
                           ('seats', room.seats)]:
        app.router.add_post('/api/' + route, handler)
    app.router.add_get('/api/state', room.state)
    app.router.add_get('/api/library', room.games)
    app.router.add_get('/api/cover', room.cover)
    app.router.add_get('/api/playing', room.playing)
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
                        help='Default: port 1 for more than two players, else disabled')
    parser.add_argument('--players', type=int, choices=range(2, 5), help='Default: 4')
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
    if not args.multitap and profile.multitap:
        args.multitap = 'port 1' if players > 2 else 'disabled'
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
        args.admin_pin, Covers(args.covers, args.library) if library else None, providers, http,
        Sessions(Path(args.saves)/'remembered-devices.json', pin, args.admin_pin)),
        args.trusted_proxy), host=args.host, port=args.port, ssl_context=context, access_log=None)


def start_disc(parser, args, profile, hub):
    """Check and start the --disc game; exit with a message on a missing core or BIOS."""
    missing = profile.missing_bios(args.system)
    if missing and profile.bios_required:
        parser.error(missing)
    elif missing:
        print(missing, flush=True)
    core = args.core or ROOT/'runtime/cores'/core_filename(core=profile.core)
    if not Path(core).is_file():
        parser.error(f'No core at {core}. Download it with: ./setup.sh --console {profile.key}')
    emulator = Emulator(core, args.disc, args.system, args.saves, hub, args.multitap, args.state,
                        profile, args.core_option)
    emulator.start()
    return emulator


if __name__ == '__main__':
    main()
