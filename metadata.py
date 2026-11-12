"""Optional online cover art and game details, off unless the host turns a provider on.

--metadata (or SOFA_METADATA) names the providers, tried in order:
- libretro: box art from the libretro-thumbnails repositories, by the game's
  original file or folder name. No account.
- screenscraper: box art, description, year, genre and players from
  ScreenScraper. Needs its developer and user credentials in the environment.
- igdb: box art, description, year, genre, players and publisher from IGDB,
  matched by name and console. Needs a Twitch client id and secret in the
  environment.

Nothing is fetched until an admin asks, per game or for every game with no
cover and no earlier attempt. Requests go out one at a time in the
background, paced to each provider's limits. Only the game's name, its serial
and its console are sent (and a provider's own credentials): nothing about
the players or this host.
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
from library import clean_title

log = logging.getLogger('metadata')

AGENT = 'SofaLink/1.0 (game cover art for a self-hosted game host)'
SOFTNAME = 'SofaLink'
MAX_DOWNLOAD = cover_art.MAX_UPLOAD*4
MAX_TEXT = 1500  # characters of description kept
# The details a provider may find, all optional: libretro finds none, ScreenScraper most.
FIELDS = cover_art.FIELDS
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
    about: dict = field(default_factory=dict)  # any of FIELDS, as text
    match: dict = None  # the provider's own entry, {id, name, year, url}, where it matches by name
    candidates: list = field(default_factory=list)  # entries it was unsure between, for an admin to pick from


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

    async def post(self, url, data, headers=None, limit=MAX_DOWNLOAD):
        """POST `data` (a dict as a form, or text) and return (status, body), like get()."""
        import aiohttp
        if not self.session:
            self.session = aiohttp.ClientSession(headers={'User-Agent': AGENT},
                                                 timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            async with self.session.post(url, data=data, headers=headers, allow_redirects=False) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(64*1024):
                    body += chunk
                    if len(body) > limit:
                        raise ProviderError('The answer was too large')
                return response.status, bytes(body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # Credentials travel in the body and headers: say only which host failed.
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

    matches = False  # True for a provider that matches by name and lets an admin pick its entry

    def available(self):
        if self.stopped and self.stopped[1] and time.time() >= self.stopped[1]:
            self.stopped = None
        return not self.stopped

    async def lookup(self, game, art=True, choice=None):
        """A Found, or None when the provider has nothing for this game.

        `choice` is an admin's pick for a provider that `matches`: its entry id, or 0 for none.
        """
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

    async def lookup(self, game, art=True, choice=None):
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


def short_text(text):
    """A description on one line, cut at a word to MAX_TEXT characters, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = ' '.join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rsplit(' ', 1)[0]+'…'


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

    async def lookup(self, game, art=True, choice=None):
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
        text = short_text(pick_text(jeu.get('synopsis')))
        if text:
            about['description'] = text
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
        publisher = jeu.get('editeur')
        if isinstance(publisher, dict) and isinstance(publisher.get('text'), str) and publisher['text'].strip():
            about['publisher'] = ' '.join(publisher['text'].split())[:80]
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


class Igdb(Provider):
    """IGDB, through Twitch's client-credentials sign-in. Free for non-commercial use under Twitch's terms.

    Matches by the game's clean title on its console and takes only a match it is sure of:
    the same name, or an alternative name, once spacing, punctuation, case and accents are
    dropped, or the same name with a prefix such as a series name. Otherwise it keeps up to
    five candidates for an admin to choose from, and shows nothing.
    """
    name = 'igdb'
    matches = True
    interval = 0.3  # IGDB allows 4 requests a second; one at a time stays well under its 8 open requests
    TOKEN = 'https://id.twitch.tv/oauth2/token'
    BASE = 'https://api.igdb.com/v4/'
    IMAGE = 'https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{}.jpg'
    PLATFORMS = {'ps1': 7, 'ps2': 8}
    FIELDS = ('name,alternative_names.name,game_type.type,version_parent,first_release_date,summary,'
              'cover.image_id,genres.name,game_modes.name,multiplayer_modes.platform,'
              'multiplayer_modes.offlinemax,multiplayer_modes.offlinecoopmax,'
              'involved_companies.company.name,involved_companies.publisher,platforms,url')
    IMAGE_ID = re.compile(r'[a-z0-9]{1,40}')

    def __init__(self, http, client_id, client_secret):
        super().__init__(http)
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_until = 0

    def __repr__(self):
        return '<Igdb>'  # never the secret or the token

    @classmethod
    def from_env(cls, http, env=os.environ):
        client_id, client_secret = env.get('SOFA_IGDB_CLIENT_ID'), env.get('SOFA_IGDB_CLIENT_SECRET')
        if not client_id or not client_secret:
            return None
        return cls(http, client_id.strip(), client_secret.strip())

    async def sign_in(self):
        """An access token, kept until a minute before it expires (about two months)."""
        if self.token and time.monotonic() < self.token_until:
            return self.token
        await self.pace()
        status, body = await self.http.post(self.TOKEN, dict(client_id=self.client_id, client_secret=self.client_secret,
                                                             grant_type='client_credentials'), limit=64*1024)
        if status in (400, 401, 403):
            raise ProviderError('Twitch refused the IGDB client id or secret', stop=True)
        if status != 200:
            raise ProviderError(f'Twitch sign-in answered {status}: try again later')
        try:
            answer = json.loads(body)
            token, expires = answer['access_token'], int(answer.get('expires_in', 3600))
        except (ValueError, KeyError, TypeError):
            raise ProviderError('Twitch sent a sign-in answer this host could not read') from None
        if not isinstance(token, str) or not token:
            raise ProviderError('Twitch sent a sign-in answer this host could not read')
        self.token, self.token_until = token, time.monotonic()+max(expires-60, 60)
        return token

    async def query(self, endpoint, body, retries=2):
        """A list of results from one Apicalypse query."""
        for attempt in range(retries+1):
            token = await self.sign_in()
            await self.pace()
            status, answer = await self.http.post(
                self.BASE+endpoint, body.encode(), limit=2*1024*1024,
                headers={'Client-ID': self.client_id, 'Authorization': 'Bearer '+token, 'Accept': 'application/json'})
            if status == 401 and attempt < retries:
                self.token = None  # revoked or expired early: sign in again
                continue
            if status == 429 and attempt < retries:
                self.interval = min(self.interval*2, 10)
                continue
            break
        if status in (401, 403):
            raise ProviderError('IGDB refused the client id or token', stop=True)
        if status == 429:
            raise ProviderError('IGDB is busy: try again later')
        if status != 200:
            raise ProviderError(f'IGDB answered {status}')
        try:
            results = json.loads(answer)
        except ValueError:
            raise ProviderError('IGDB sent an answer this host could not read') from None
        return [item for item in results if isinstance(item, dict) and isinstance(item.get('id'), int)] \
            if isinstance(results, list) else []

    @staticmethod
    def searches(title):
        """What to search for: the clean title, with a colon for " - ", then its subtitle alone."""
        title = re.sub(r'["\\]', '', title).strip()
        names = [title, title.replace(' - ', ': ')]
        if ' - ' in title:
            names.append(title.rsplit(' - ', 1)[1])
        return list(dict.fromkeys(name for name in names if name))

    @staticmethod
    def names(entry):
        names = [entry.get('name')]+[alt.get('name') for alt in entry.get('alternative_names') or []
                                     if isinstance(alt, dict)]
        return [loose_name(name) for name in names if isinstance(name, str) and name.strip()]

    @classmethod
    def sureness(cls, title, entry):
        """2 for the same name, 1 for the same name after a prefix (a series name), else 0."""
        wanted = loose_name(title)
        if not wanted:
            return 0
        names = cls.names(entry)
        if wanted in names:
            return 2
        # "007 - Agent Under Fire" is "James Bond 007: Agent Under Fire". A name that only
        # starts the same ("Racer" and "Racer 2") is a different game.
        if any(name.endswith(wanted) and len(wanted) >= max(len(name)/2, 6) for name in names):
            return 1
        return 0

    @staticmethod
    def main_game(entry):
        kind = (entry.get('game_type') or {}).get('type') if isinstance(entry.get('game_type'), dict) else None
        return not entry.get('version_parent') and kind in (None, 'Main Game')

    @classmethod
    def choose(cls, title, results):
        """(the entry it is sure of, or None; the candidates in order)."""
        scored = [(cls.sureness(title, entry), entry) for entry in results]
        for level in (2, 1):
            sure = [entry for score, entry in scored if score == level]
            if len(sure) > 1:
                # Two entries of the same name: the main game over its editions, else ask.
                main = [entry for entry in sure if cls.main_game(entry)]
                sure = main if len(main) == 1 else sure
            if len(sure) == 1:
                return sure[0], []
            if sure:
                return None, sure
        return None, [entry for _, entry in scored]

    @staticmethod
    def year(entry):
        stamp = entry.get('first_release_date')
        if isinstance(stamp, (int, float)) and stamp > 0:
            return str(datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).year)
        return None

    def summary(self, entry):
        """{id, name, year, url} for the page; the URL only on IGDB's own site."""
        url = entry.get('url') if isinstance(entry.get('url'), str) else ''
        parts = urlsplit(url)
        return dict(id=entry['id'], name=' '.join(str(entry.get('name') or '?').split())[:120], year=self.year(entry),
                    url=url if parts.scheme == 'https' and parts.hostname == 'www.igdb.com' else None)

    def about(self, entry, platform):
        about = {}
        text = short_text(entry.get('summary') if isinstance(entry.get('summary'), str) else None)
        if text:
            about['description'] = text
        if self.year(entry):
            about['year'] = self.year(entry)

        def names(items, test=lambda item: True):
            found = []
            for item in items or []:
                name = item.get('name') if isinstance(item, dict) and test(item) else None
                if isinstance(name, str) and name.strip() and name.strip() not in found:
                    found.append(' '.join(name.split()))
            return found
        genres = names(entry.get('genres'))
        if genres:
            about['genre'] = ', '.join(genres[:2])[:60]
        publishers = [company for company in (entry.get('involved_companies') or [])
                      if isinstance(company, dict) and company.get('publisher')]
        publishers = names([company.get('company') for company in publishers])
        if publishers:
            about['publisher'] = ', '.join(publishers[:2])[:80]
        most = [max((mode.get(key) for key in ('offlinemax', 'offlinecoopmax') if isinstance(mode.get(key), int)), default=0)
                for mode in entry.get('multiplayer_modes') or []
                if isinstance(mode, dict) and mode.get('platform') in (platform, None)]
        if most and max(most) > 1:
            about['players'] = f'1-{min(max(most), 16)}'
        elif names(entry.get('game_modes')) == ['Single player']:
            about['players'] = '1'
        return about

    async def lookup(self, game, art=True, choice=None):
        platform = self.PLATFORMS.get(game.console)
        if not platform or choice == 0:
            return None
        fields = f'fields {self.FIELDS};'
        if choice:
            # The admin's pick: trusted, whatever its name or platforms.
            results = await self.query('games', f'{fields} where id = {int(choice)}; limit 1;')
            entry, candidates = (results[0] if results else None), []
        else:
            title = clean_title(game.title)
            entry, candidates, seen = None, [], set()
            for search in self.searches(title):
                results = await self.query('games', f'search "{search}"; {fields} where platforms = ({platform}); limit 10;')
                entry, unsure = self.choose(title, results)
                if entry:
                    break
                for item in unsure:
                    if item['id'] not in seen:
                        seen.add(item['id'])
                        candidates.append(item)
        if not entry:
            return Found(candidates=[self.summary(item) for item in candidates[:5]]) if candidates else None
        found = Found(about=self.about(entry, platform), match=self.summary(entry))
        image = (entry.get('cover') or {}).get('image_id') if isinstance(entry.get('cover'), dict) else None
        if art and isinstance(image, str) and self.IMAGE_ID.fullmatch(image):
            await self.pace()
            status, body = await self.http.get(self.IMAGE.format(image))
            if status == 200 and body:
                found.art = body
            elif status == 429 or status >= 500:
                raise ProviderError("IGDB's images are busy: try again later")
        return found


PROVIDERS = ('libretro', 'screenscraper', 'igdb')


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
        elif name == 'igdb':
            provider = Igdb.from_env(http, env)
            if provider:
                chosen.append(provider)
            else:
                warnings.append('IGDB needs SOFA_IGDB_CLIENT_ID and SOFA_IGDB_CLIENT_SECRET')
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

    def matching(self):
        """The provider here that lets an admin pick the entry a game matches, by name, or None."""
        return next((provider.name for provider in self.providers if provider.matches), None)

    async def fetch(self, game):
        art, about, sources, errors, match, candidates = None, {}, [], [], None, []
        choices = await asyncio.to_thread(self.covers.matches, game.id)
        for provider in self.providers:
            if art and about.get('description'):
                break
            if not provider.available():
                errors.append(provider.stopped[0])
                continue
            try:
                if provider.matches:
                    found = await provider.lookup(game, art=not art, choice=choices.get(provider.name))
                else:
                    found = await provider.lookup(game, art=not art)
            except ProviderError as error:
                errors.append(str(error))
                if error.stop:
                    provider.stopped = (str(error), error.until)
                continue
            if not found:
                continue
            if found.match and not match:
                match = dict(found.match, provider=provider.name)
            elif found.candidates and not match and not candidates:
                candidates = [dict(item, provider=provider.name) for item in found.candidates]
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
        if errors and not art and not about and not candidates:
            # Not cached: an admin can try again once the provider is back.
            self.notes[game.id] = errors[0]
            return
        note = None if art or self.covers.fetched(game.id).is_file() else (errors[0] if errors else 'No art found')
        if candidates and not match:
            note = 'Not sure which game this is: choose the match'
        await asyncio.to_thread(self.covers.store_fetched, game.id, art,
                                dict(sources=sources, about=about, note=note, match=match, candidates=candidates,
                                     fetched=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')))

    async def close(self):
        if self.task:
            self.task.cancel()
        await self.http.close()

