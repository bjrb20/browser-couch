"""Optional online cover art and game details, off unless the host turns a provider on.

--metadata (or SOFA_METADATA) names the providers, tried in order:
- libretro: box art from the libretro-thumbnails repositories, by the game's
  original file or folder name. No account.
- screenscraper: box art, description, year, genre and players from
  ScreenScraper. Needs its developer and user credentials in the environment.

Nothing is fetched until an admin asks, per game or for every game with no
cover and no earlier attempt. Requests go out one at a time in the
background, paced to each provider's limits. Only the game's name, its serial
and its console are sent: nothing about the players or this host.
What a provider returns is cached in the covers folder by game id and
fetched again only when an admin asks for that game.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import datetime
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit

import covers as cover_art

log = logging.getLogger('metadata')

AGENT = 'SofaLink/1.0 (game cover art for a self-hosted game host)'
SOFTNAME = 'SofaLink'
MAX_DOWNLOAD = cover_art.MAX_UPLOAD*4
MAX_TEXT = 1500  # characters of description kept
SERIAL = re.compile(r'\b([A-Z]{4})[-_ ]?(\d{3})\.?(\d{2})\b')


class ProviderError(Exception):
    """A provider failed. The message is safe to show an admin. `stop` turns it off until `until`."""

    def __init__(self, message, stop=False, until=None):
        super().__init__(message)
        self.stop = stop
        self.until = until


@dataclass
class Found:
    art: bytes = None  # the image as downloaded; the caller re-encodes it
    about: dict = field(default_factory=dict)  # description, year, genre, players


class Http:
    """The one place requests leave the host. Tests replace get()."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.session = None

    async def get(self, url, params=None, limit=MAX_DOWNLOAD):
        """(status, body) with at most `limit` bytes of body, or raise ProviderError."""
        import aiohttp
        if not self.session:
            self.session = aiohttp.ClientSession(headers={'User-Agent': AGENT},
                                                 timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            async with self.session.get(url, params=params, allow_redirects=True) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(64*1024):
                    body += chunk
                    if len(body) > limit:
                        raise ProviderError('The answer was too large')
                return response.status, bytes(body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # The exception text can hold the URL, and ScreenScraper's URLs hold credentials.
            raise ProviderError(f'Could not reach {urlsplit(url).hostname}') from None

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


class Provider:
    name = ''
    interval = 1.0  # seconds between requests

    def __init__(self, http):
        self.http = http
        self.last = 0
        self.stopped = None  # (message, until or None)

    async def pace(self):
        wait = self.last+self.interval-time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.monotonic()

    def available(self):
        if self.stopped and self.stopped[1] and time.time() >= self.stopped[1]:
            self.stopped = None
        return not self.stopped

    async def lookup(self, game, art=True):
        """A Found, or None when the provider has nothing for this game."""
        raise NotImplementedError


def libretro_name(name):
    """A name as the libretro-thumbnails files spell it."""
    return re.sub(r'[&*/:`<>?\\|]', '_', name)


LANGUAGES = re.compile(r'\((?:[A-Z][a-z][,+ ]*)+\)')  # (En,Fr,De) or (EnFrDe), not (Europe) or (Rev 1)


def loose_name(name):
    """A name for matching when the exact one fails.

    No language list, no [...] tags, no accents, case, spaces or punctuation:
    "Name (Europe) (EnFrDe) [SLES-00001]" and "Na-me (Europe) (En,Fr)" agree.
    The region, disc and edition tags stay, so a different release does not.
    """
    name = LANGUAGES.sub('', re.sub(r'\[[^\[\]]*\]', '', name))
    name = unicodedata.normalize('NFKD', name)
    return ''.join(char for char in name.casefold() if char.isalnum())


class Libretro(Provider):
    name = 'libretro'
    interval = 0.5
    BASE = 'https://raw.githubusercontent.com/libretro-thumbnails'
    SYSTEMS = {'ps1': 'Sony - PlayStation', 'ps2': 'Sony - PlayStation 2'}
    KINDS = ('Named_Boxarts', 'Named_Titles', 'Named_Snaps')
    TREES = 'https://api.github.com/repos/libretro-thumbnails'
    INDEX_AGE = 24*3600  # seconds a file list is kept

    def __init__(self, http):
        super().__init__(http)
        self.indexes = {}  # (repo, kind or None) -> (time, {loose name: file name} or {folder: tree sha})

    async def tree(self, repo, sha):
        """One folder of the repository from GitHub's API: {name: (type, sha)}."""
        await self.pace()
        status, body = await self.http.get(f'{self.TREES}/{quote(repo)}/git/trees/{quote(sha)}')
        if status in (403, 429) or status >= 500:
            raise ProviderError("GitHub is limiting requests for libretro's file list: try again later")
        if status != 200:
            return {}
        try:
            return {item['path']: (item['type'], item['sha']) for item in json.loads(body)['tree']}
        except (ValueError, KeyError, TypeError):
            raise ProviderError("libretro's file list could not be read") from None

    async def index(self, repo, kind):
        """{loose name: file name} for one kind of image, fetched once a day: two requests per console."""
        cached = self.indexes.get((repo, kind))
        if cached and time.time()-cached[0] < self.INDEX_AGE:
            return cached[1]
        root = self.indexes.get((repo, None))
        if not root or time.time()-root[0] >= self.INDEX_AGE:
            root = self.indexes[(repo, None)] = (time.time(), await self.tree(repo, 'master'))
        folder = root[1].get(kind)
        files = await self.tree(repo, folder[1]) if folder and folder[0] == 'tree' else {}
        index = {}
        for path in sorted(files):
            if path.lower().endswith('.png'):
                index.setdefault(loose_name(path[:-4]), path)
        self.indexes[(repo, kind)] = (time.time(), index)
        return index

    async def download(self, repo, kind, file):
        await self.pace()
        status, body = await self.http.get(f'{self.BASE}/{quote(repo)}/master/{kind}/{quote(file)}')
        if status == 200 and body:
            return body
        if status == 429 or status >= 500:
            raise ProviderError('libretro-thumbnails is busy: try again later')
        return None

    @staticmethod
    def names(game):
        """The original Redump/No-Intro style name first, then without any [...] tags added to it."""
        names = [game.title]
        bare = re.sub(r'\s*\[[^\[\]]*\]', '', game.title).strip()
        if bare and bare not in names:
            names.append(bare)
        return names

    async def lookup(self, game, art=True):
        system = self.SYSTEMS.get(game.console)
        if not system or not art:
            return None
        repo = system.replace(' ', '_')
        for kind in self.KINDS:
            for name in self.names(game):
                body = await self.download(repo, kind, libretro_name(name)+'.png')
                if body:
                    return Found(art=body)
            # Not under its exact name: look for the same name, spelt a little differently, in the file list.
            file = (await self.index(repo, kind)).get(loose_name(game.title))
            if file:
                body = await self.download(repo, kind, file)
                if body:
                    return Found(art=body)
        return None


def serial(game):
    """A disc serial from the name's tags, for example SLUS-00001, or None."""
    for tag in re.findall(r'[\[(]([^\[\]()]*)[\])]', game.title):
        match = SERIAL.search(tag.upper())
        if match:
            return f'{match[1]}-{match[2]}{match[3]}'
    return None


def tomorrow():
    """ScreenScraper's daily quotas: wait until the next UTC midnight."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now+datetime.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).timestamp()


def pick_text(items, key='text'):
    """The English entry of a [{langue|region, text}] list, else the first."""
    if not isinstance(items, list):
        return None
    items = [item for item in items if isinstance(item, dict) and isinstance(item.get(key), str)]
    for item in items:
        if item.get('langue') == 'en' or item.get('region') in ('us', 'wor', 'en'):
            return item[key]
    return items[0][key] if items else None


class ScreenScraper(Provider):
    name = 'screenscraper'
    interval = 1.2  # until the account's own limits are known
    BASE = 'https://api.screenscraper.fr/api2/'
    SYSTEMS = {'ps1': 57, 'ps2': 58}
    MEDIA = ('box-2D', 'sstitle', 'ss')
    REGIONS = ('us', 'wor', 'eu', 'ss', 'uk', 'jp')
    ERRORS = {
        400: ('ScreenScraper refused the request', False),
        401: ('ScreenScraper is closed to non-members right now', True),
        403: ('ScreenScraper refused the developer credentials', True),
        423: ('ScreenScraper is closed right now', True),
        426: ('ScreenScraper has blocked this software version', True),
        430: ("ScreenScraper's daily quota is used up", True),
        431: ('Too many games ScreenScraper did not know today', True),
    }

    def __init__(self, http, devid, devpassword, user=None, password=None):
        super().__init__(http)
        self.credentials = dict(devid=devid, devpassword=devpassword, softname=SOFTNAME)
        if user:
            self.credentials.update(ssid=user, sspassword=password or '')

    @classmethod
    def from_env(cls, http, env=os.environ):
        devid, devpassword = env.get('SOFA_SCREENSCRAPER_DEVID'), env.get('SOFA_SCREENSCRAPER_DEVPASSWORD')
        if not devid or not devpassword:
            return None
        return cls(http, devid, devpassword, env.get('SOFA_SCREENSCRAPER_USER'),
                   env.get('SOFA_SCREENSCRAPER_PASSWORD'))

    def limits(self, user):
        """Follow the account's own limits from each answer's ssuser block."""
        if not isinstance(user, dict):
            return

        def number(key):
            try:
                return int(user.get(key))
            except (TypeError, ValueError):
                return None
        per_minute = [n for n in (number('maxrequestspermin'), number('maxdownloadspermin')) if n]
        if per_minute:
            self.interval = max(60/min(per_minute), 1.0)
        today, daily = number('requeststoday'), number('maxrequestsperday')
        if daily and today is not None and today >= daily-1:
            self.stopped = ("ScreenScraper's daily quota is used up", tomorrow())
        bad, bad_daily = number('requestskotoday'), number('maxrequestskoperday')
        if bad_daily and bad is not None and bad >= bad_daily-1:
            self.stopped = ('Too many games ScreenScraper did not know today', tomorrow())

    async def request(self, url, params, limit=MAX_DOWNLOAD, retries=2):
        for attempt in range(retries+1):
            await self.pace()
            status, body = await self.http.get(url, params, limit)
            if status != 429:
                break
            # Over the account's thread or per-minute limit: slow down.
            self.interval = min(self.interval*2, 30)
            if attempt == retries:
                raise ProviderError('ScreenScraper is busy: try again later')
        if status in self.ERRORS:
            message, stop = self.ERRORS[status]
            raise ProviderError(message, stop, tomorrow() if status in (430, 431) else None)
        return status, body

    async def lookup(self, game, art=True):
        system = self.SYSTEMS.get(game.console)
        if not system:
            return None
        params = dict(self.credentials, output='json', systemeid=system, romtype='rom',
                      romnom=game.title+game.path.suffix)
        number = serial(game)
        if number:
            params['serialnum'] = number
        status, body = await self.request(self.BASE+'jeuInfos.php', params, 2*1024*1024)
        if status == 404:
            return None
        if status != 200:
            raise ProviderError(f'ScreenScraper answered {status}')
        try:
            response = json.loads(body)['response']
            self.limits(response.get('ssuser'))
            jeu = response.get('jeu')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('ScreenScraper sent an answer this host could not read') from None
        if not isinstance(jeu, dict):
            return None
        found = Found(about=self.about(jeu))
        url = self.media(jeu) if art else None
        if url:
            status, image = await self.request(url, None)
            if status == 200 and image:
                found.art = image
        return found

    def about(self, jeu):
        about = {}
        text = pick_text(jeu.get('synopsis'))
        if text:
            text = ' '.join(text.split())
            about['description'] = text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rsplit(' ', 1)[0]+'…'
        dates = [item.get('text', '') for item in jeu.get('dates') or [] if isinstance(item, dict)]
        years = sorted(match[0] for match in (re.match(r'(19|20)\d\d', str(date)) for date in dates) if match)
        if years:
            about['year'] = years[0]
        genres = []
        for genre in jeu.get('genres') or []:
            name = pick_text(genre.get('noms')) if isinstance(genre, dict) else None
            if name and name not in genres:
                genres.append(name)
        if genres:
            about['genre'] = ', '.join(genres[:2])
        players = jeu.get('joueurs')
        if isinstance(players, dict) and isinstance(players.get('text'), str):
            about['players'] = players['text'][:16]
        return {key: value[:MAX_TEXT+1] for key, value in about.items()}

    def media(self, jeu):
        """The best box art URL, only on ScreenScraper's own hosts."""
        medias = [item for item in jeu.get('medias') or [] if isinstance(item, dict)
                  and isinstance(item.get('url'), str) and item.get('format', 'png') in ('png', 'jpg', 'jpeg', 'webp')]
        for kind in self.MEDIA:
            matches = [item for item in medias if item.get('type') == kind]
            matches.sort(key=lambda item: self.REGIONS.index(item.get('region'))
                         if item.get('region') in self.REGIONS else len(self.REGIONS))
            for item in matches:
                parts = urlsplit(item['url'])
                if parts.scheme == 'https' and (parts.hostname or '').endswith('.screenscraper.fr'):
                    return item['url']
        return None


PROVIDERS = ('libretro', 'screenscraper')


def providers(names, http, env=os.environ):
    """The providers named in --metadata, and a warning for each that cannot run."""
    chosen, warnings = [], []
    for name in [part.strip().lower() for part in (names or '').split(',') if part.strip()]:
        if name in ('off', 'none', 'no'):
            return [], []
        if name == 'libretro':
            chosen.append(Libretro(http))
        elif name == 'screenscraper':
            provider = ScreenScraper.from_env(http, env)
            if provider:
                chosen.append(provider)
            else:
                warnings.append('ScreenScraper needs SOFA_SCREENSCRAPER_DEVID and SOFA_SCREENSCRAPER_DEVPASSWORD')
        else:
            warnings.append(f'Unknown metadata provider {name}: use {", ".join(PROVIDERS)}')
    return chosen, warnings


class Fetcher:
    """Fetches in the background, one game at a time, and keeps what it finds in the covers folder."""

    def __init__(self, covers, providers, http, lock=None):
        self.covers = covers
        self.providers = providers
        self.http = http
        self.lock = lock or asyncio.Lock()  # the covers' decode lock: one image at a time
        self.queue = deque()
        self.state = {}  # game id -> 'queued' or 'fetching'
        self.notes = {}  # game id -> the last failure, until the next attempt
        self.task = None

    def status(self, game_id):
        return dict(state=self.state.get(game_id), note=self.notes.get(game_id))

    def request(self, games):
        """Queue games that are not queued already. Returns how many were added."""
        added = 0
        for game in games:
            if game.id not in self.state:
                self.state[game.id] = 'queued'
                self.notes.pop(game.id, None)
                self.queue.append(game)
                added += 1
        if added and (not self.task or self.task.done()):
            self.task = asyncio.create_task(self.work())
        return added

    def missing(self, games):
        """Games with no cover of any kind and no earlier attempt."""
        return [game for game in games if not self.covers.info(game) and not self.covers.record(game.id)]

    async def work(self):
        while self.queue:
            game = self.queue.popleft()
            self.state[game.id] = 'fetching'
            try:
                await self.fetch(game)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Fetching metadata failed')
                self.notes[game.id] = 'Fetching failed'
            finally:
                self.state.pop(game.id, None)

    async def fetch(self, game):
        art, about, sources, errors = None, {}, [], []
        for provider in self.providers:
            if art and about.get('description'):
                break
            if not provider.available():
                errors.append(provider.stopped[0])
                continue
            try:
                found = await provider.lookup(game, art=not art)
            except ProviderError as error:
                errors.append(str(error))
                if error.stop:
                    provider.stopped = (str(error), error.until)
                continue
            if not found:
                continue
            if found.art and not art:
                try:
                    async with self.lock:
                        art = await asyncio.to_thread(cover_art.encode, found.art, MAX_DOWNLOAD)
                    sources.append(provider.name)
                except cover_art.BadImage:
                    errors.append(f'{provider.name} sent an image this host could not read')
            for key, value in found.about.items():
                about.setdefault(key, value)
            if found.about and provider.name not in sources:
                sources.append(provider.name)
        if errors and not art and not about:
            # Not cached: an admin can try again once the provider is back.
            self.notes[game.id] = errors[0]
            return
        note = None if art or self.covers.fetched(game.id).is_file() else (errors[0] if errors else 'No art found')
        await asyncio.to_thread(self.covers.store_fetched, game.id, art,
                                dict(sources=sources, about=about, note=note,
                                     fetched=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')))

    async def close(self):
        if self.task:
            self.task.cancel()
        await self.http.close()

