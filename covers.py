"""Cover art for library games, keyed by the same game id as the saves.

Three sources, checked in this order:
- an image an admin uploaded, kept in the covers folder as <game id>.jpg;
- cover.jpg, cover.jpeg or cover.png in a game's own folder in the library;
- art a metadata provider found (metadata.py), in fetched/<game id>.jpg, with
  the game's details in fetched/<game id>.json. Fetching never touches the
  other two, so a cover a person chose always wins.

An admin's own description, year or genre is kept apart in edited/<game id>.json
and wins over a provider's, which fetching never changes. An admin's pick of
which provider entry a game is (matched/<game id>.json) is kept the same way.

Every image is decoded and re-encoded here before a browser sees it: JPEG,
PNG or WebP by content, at most MAX_SIZE on each side, as a JPEG with no
metadata. Browsers only ever name a game id, never a path.
"""
from collections import OrderedDict
import hashlib
import json
from io import BytesIO
import os
from pathlib import Path
import re
import secrets
import unicodedata

from PIL import Image, ImageOps

MAX_UPLOAD = 5*1024*1024  # bytes
MAX_SIZE = (600, 900)  # width, height: box art is portrait
MAX_PIXELS = 40_000_000  # refuse to decode anything larger, whatever the file size
FORMATS = ('JPEG', 'PNG', 'WEBP')
SIDECARS = ('cover.jpg', 'cover.jpeg', 'cover.png')
GAME_ID = re.compile(r'[0-9a-f]{16}')
BACKGROUND = (12, 18, 22)  # the page background, under transparent pixels
# A game's details, all optional and plain text. Admins may set the first three by hand.
FIELDS = ('description', 'year', 'genre', 'players', 'publisher')
EDITABLE = dict(description=2000, year=4, genre=60)  # field -> most characters
YEAR = re.compile(r'(19[4-9]\d|20\d\d)')


class BadImage(ValueError):
    """The upload is not an image we accept. The message is safe to show a browser."""


def encode(data, limit=MAX_UPLOAD):
    """Validate image bytes and return a clean JPEG: decoded by content, downscaled, no metadata."""
    if len(data) > limit:
        raise BadImage(f'Use an image of at most {limit//(1024*1024)} MB')
    try:
        with Image.open(BytesIO(data), formats=FORMATS) as image:
            if image.width*image.height > MAX_PIXELS:
                raise BadImage('That image has too many pixels')
            image.draft('RGB', (MAX_SIZE[0]*2, MAX_SIZE[1]*2))  # JPEG only: decode at a smaller scale
            image.seek(0)  # the first frame of an animation
            image = ImageOps.exif_transpose(image)
            image.load()
    except BadImage:
        raise
    except (Image.DecompressionBombError, Image.UnidentifiedImageError):
        raise BadImage('Use a JPEG, PNG or WebP image') from None
    except Exception:  # truncated or corrupt files fail in many ways inside the decoders
        raise BadImage('That image could not be read') from None
    if image.mode in ('RGBA', 'LA', 'PA') or image.mode == 'P' and 'transparency' in image.info:
        image = image.convert('RGBA')
        flat = Image.new('RGB', image.size, BACKGROUND)
        flat.paste(image, mask=image.getchannel('A'))
        image = flat
    else:
        image = image.convert('RGB')
    image.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    # A new RGB image carries no EXIF, ICC profile, comment or XMP: save adds only what it is given.
    clean = Image.new('RGB', image.size)
    clean.paste(image)
    out = BytesIO()
    clean.save(out, 'JPEG', quality=85, optimize=True, progressive=True)
    return out.getvalue()


def plain(text, limit, lines=False):
    """Text with no control or formatting characters, spaces collapsed; newlines kept only if `lines`."""
    text = unicodedata.normalize('NFC', text)
    text = ''.join(char if char == '\n' and lines or unicodedata.category(char)[0] != 'C' else ' '
                   for char in text.replace('\r\n', '\n'))
    rows = [' '.join(row.split()) for row in text.split('\n')]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(rows)).strip()
    if len(text) > limit:
        raise ValueError(f'Use at most {limit} characters')
    return text


def checked_edits(body):
    """An admin's {field: text} for EDITABLE fields, cleaned. An empty text undoes that edit.

    Raises ValueError with a message that is safe to show a browser.
    """
    if not isinstance(body, dict) or not body or set(body) - set(EDITABLE):
        raise ValueError('Send the description, year or genre')
    edits = {}
    for key, value in body.items():
        if not isinstance(value, str) or len(value) > EDITABLE[key]*2:
            raise ValueError(f'The {key} is too long')
        try:
            value = plain(value, EDITABLE[key], lines=key == 'description')
        except ValueError as error:
            raise ValueError(f'The {key}: {str(error).lower()}') from None
        if key == 'year' and value and not YEAR.fullmatch(value):
            raise ValueError('The year should look like 1998')
        edits[key] = value
    return edits


def checked_id(game_id):
    if not isinstance(game_id, str) or not GAME_ID.fullmatch(game_id):
        raise ValueError('Not a game id')
    return game_id


class Covers:
    def __init__(self, root, library_root=None, cache=64):
        self.root = Path(root) if root else None
        self.library_root = Path(library_root).resolve() if library_root else None
        self.cache = OrderedDict()  # (path, mtime, size) -> encoded library cover
        self.cache_size = cache
        self.records = {}  # game id -> (mtime_ns, fetched record)

    def uploaded(self, game_id):
        return self.root/f'{checked_id(game_id)}.jpg' if self.root else None

    def fetched(self, game_id, suffix='.jpg'):
        return self.root/'fetched'/f'{checked_id(game_id)}{suffix}' if self.root else None

    def record(self, game_id):
        """What a provider found for the game, {sources, about, note, fetched}, or None if never fetched."""
        path = self.fetched(game_id, '.json')
        stat = self._stat(path)
        if not stat:
            self.records.pop(game_id, None)
            return None
        cached = self.records.get(game_id)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        self.records[game_id] = (stat.st_mtime_ns, record)
        return record

    def edits(self, game_id):
        """An admin's own {field: text} for the game, or {}."""
        path = self.root/'edited'/f'{checked_id(game_id)}.json' if self.root else None
        try:
            edits = json.loads(path.read_text()) if path and path.is_file() else {}
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in edits.items() if key in EDITABLE and isinstance(value, str) and value} \
            if isinstance(edits, dict) else {}

    def save_edits(self, game_id, changes):
        """Merge `changes` from checked_edits() into the game's edits: empty text removes that edit."""
        if not self.root:
            raise OSError('No covers folder')
        edits = dict(self.edits(game_id), **changes)
        self._write(self.root/'edited'/f'{checked_id(game_id)}.json',
                    json.dumps({key: value for key, value in edits.items() if value}).encode())
        return self.about(game_id)

    def about(self, game_id):
        """({field: text}, [edited fields]): the admin's edits over what a provider found."""
        record = self.record(game_id) or {}
        found = record.get('about') if isinstance(record.get('about'), dict) else {}
        about = {key: found[key] for key in FIELDS if isinstance(found.get(key), str) and found[key]}
        edits = self.edits(game_id)
        return dict(about, **edits), sorted(edits)

    def matches(self, game_id):
        """An admin's picks of the entry a game matches, {provider: entry id, or 0 for none}."""
        path = self.root/'matched'/f'{checked_id(game_id)}.json' if self.root else None
        try:
            picks = json.loads(path.read_text()) if path and path.is_file() else {}
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in picks.items() if isinstance(key, str) and type(value) is int and value >= 0} \
            if isinstance(picks, dict) else {}

    def save_match(self, game_id, provider, entry):
        """Pick `provider`'s entry for the game (an id, or 0 for none), or None to match by name again.

        What was fetched before is dropped, art included, so the next fetch starts clean.
        Uploads and edits stay.
        """
        if not self.root:
            raise OSError('No covers folder')
        picks = dict(self.matches(game_id))
        if entry is None:
            picks.pop(provider, None)
        else:
            picks[provider] = entry
        self._write(self.root/'matched'/f'{checked_id(game_id)}.json', json.dumps(picks).encode())
        for suffix in ('.jpg', '.json'):
            self.fetched(game_id, suffix).unlink(missing_ok=True)
        self.records.pop(game_id, None)

    def sidecar(self, game):
        """cover.jpg or cover.png in the game's own folder, not one the console's games share."""
        folder = game.path.parent
        if self.library_root and folder.resolve() == (self.library_root/game.console).resolve():
            return None
        for name in SIDECARS:
            for path in (folder/name, folder/name.upper()):
                if path.is_file():
                    return path
        return None

    def _stat(self, path):
        try:
            return path.stat() if path and path.is_file() else None
        except OSError:
            return None

    def info(self, game):
        """For the library listing: None, or {version, uploaded} with no path in it."""
        upload = self._stat(self.uploaded(game.id))
        if upload:
            return dict(version=self._version('u', upload), uploaded=True)
        path = self.sidecar(game)
        stat = self._stat(path)
        if stat:
            return dict(version=self._version('f', stat), uploaded=False)
        stat = self._stat(self.fetched(game.id))
        if stat:
            return dict(version=self._version('m', stat), uploaded=False, fetched=True)
        return None

    @staticmethod
    def _version(kind, stat):
        return hashlib.sha256(f'{kind}{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()[:12]

    def read(self, game):
        """The cover's JPEG bytes, or None."""
        upload = self.uploaded(game.id)
        try:
            if upload and upload.is_file():
                return upload.read_bytes()
        except OSError:
            pass
        path = self.sidecar(game)
        stat = self._stat(path)
        if not stat:
            try:
                fetched = self.fetched(game.id)
                return fetched.read_bytes() if fetched and fetched.is_file() else None
            except OSError:
                return None
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if stat.st_size > MAX_UPLOAD*4:
            return None
        try:
            # The host put this file in the library, so it may be bigger than an upload may be.
            data = encode(path.read_bytes(), MAX_UPLOAD*4)
        except (BadImage, OSError):
            return None
        self.cache[key] = data
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return data

    def save(self, game_id, data):
        """Store an upload's clean JPEG, replacing any earlier one. `data` is from encode()."""
        if not self.root:
            raise OSError('No covers folder')
        self._write(self.uploaded(game_id), data)

    def store_fetched(self, game_id, art, record):
        """Keep what a provider found: art (from encode()) or None, and the record.

        Without new art, earlier fetched art stays. Uploads are never touched.
        """
        if not self.root:
            raise OSError('No covers folder')
        if art:
            self._write(self.fetched(game_id), art)
        self._write(self.fetched(game_id, '.json'), json.dumps(record).encode())

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent/f'.{path.name}.{secrets.token_hex(4)}.tmp'
        try:
            with open(temporary, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, game_id):
        """Remove an uploaded cover. A library cover.jpg stays: the library is read-only."""
        path = self.uploaded(game_id)
        if not path:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
