"""The game library: one --library subfolder per console, scanned for games the host can run.

Browsers get only ids, titles and console names. The paths stay on the host,
and nothing here serves a disc, ROM or BIOS file.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import platform as platforms
import re

from fetch_core import core_filename
from systems import PROFILES

MAX_FILES = 20000  # stop scanning a huge or looping folder tree
TAGS = re.compile(r'(\s*(\([^()]*\)|\[[^\[\]]*\]))+\s*$')  # trailing (Region) [SERIAL] groups


@dataclass(frozen=True)
class Game:
    id: str
    title: str
    console: str  # the library folder, for example ps2
    profile: str  # the system profile that runs it, for example ps2-play
    path: Path
    display: str = ''  # the title browsers see; `title` names the save folder and never changes

    @property
    def name(self):
        return self.display or self.title


def clean_title(title):
    """The title without its trailing (...) and [...] groups, or the title itself if nothing would be left."""
    return TAGS.sub('', title).strip() or title


def display_titles(titles):
    """Clean titles, except where two games would share one: those keep their full titles."""
    clean = [clean_title(title) for title in titles]
    counts = {}
    for name in clean:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    return [name if counts[name.casefold()] == 1 else title for title, name in zip(titles, clean)]


def consoles():
    """Library folder -> the profiles that can run its games, preferred first."""
    folders = {}
    for profile in PROFILES.values():
        if profile.library:
            folders.setdefault(profile.library, []).append(profile)
    return folders


def choose(profiles, system, cores, system_name=None):
    """Pick the first profile with its core, platform and any required BIOS.

    Returns (profile or None, host warning, browser warning); a warning is
    None when there is nothing to say. The browser warning names no host paths.
    A BIOS that the chosen core can do without, or that only an earlier
    profile needed, is still a warning.
    """
    system_name = system_name or platforms.system()
    host, public = [], []
    for profile in profiles:
        if system_name not in profile.platforms:
            continue
        core = Path(cores)/core_filename(system_name, profile.core)
        if not core.is_file():
            text = f'No {profile.core} core: run ./setup.sh --console {profile.key}.'
            host.append(text)
            public.append(text)
            continue
        missing = profile.missing_bios(system)
        if missing:
            host.append(missing)
            public.append(profile.missing_bios(system, "the host's BIOS folder"))
            if profile.bios_required:
                continue
        return profile, ' '.join(host) or None, ' '.join(public) or None
    if not host:
        host = public = [f'This host cannot run {profiles[0].name} games.']
    return None, ' '.join(host), ' '.join(public)


def referenced(playlist):
    """The files a CUE or M3U names, so the library lists the playlist and not its tracks or discs."""
    names = set()
    try:
        text = playlist.read_text(errors='replace')
    except OSError:
        return names
    for line in text.splitlines()[:1000]:
        line = line.strip()
        if playlist.suffix.lower() == '.cue':
            if not line.upper().startswith('FILE '):
                continue
            line = line[5:].rsplit(' ', 1)[0].strip().strip('"')
        elif not line or line.startswith('#'):
            continue
        names.add((playlist.parent/line).resolve())
    return names


def missing_tracks(playlist):
    """For a CUE or M3U: why the files it names cannot be read, or None when they are all there.

    The message names no files, because a browser may see it.
    """
    if playlist.suffix.lower() not in ('.cue', '.m3u'):
        return None
    missing = sorted(path for path in referenced(playlist) if not path.is_file())
    if not missing:
        return None
    kind = 'track' if playlist.suffix.lower() == '.cue' else 'disc'
    text = f'The {playlist.suffix[1:].upper()} names {len(missing)} {kind} file{"s" if len(missing) > 1 else ""} that '
    text += 'is not there.' if len(missing) == 1 else 'are not there.'
    named = {path.name.casefold() for path in referenced(playlist)} | {playlist.name.casefold()}
    others = sorted({path.suffix.lower() for path in playlist.parent.iterdir()
                     if path.is_file() and path.suffix and not path.name.startswith('.')
                     and path.name.casefold() not in named and path.suffix.lower() not in ('.cue', '.m3u')})
    if others:
        text += f' Its folder has {", ".join(others)} files instead.'
    if kind == 'track':
        text += ' The core reads raw BIN tracks or one CHD: convert compressed tracks (APE, ECM, 7z) first.'
    return text


def files(folder, extensions):
    found = []
    for path in sorted(folder.rglob('*')):
        if len(found) >= MAX_FILES:
            break
        if any(part.startswith('.') for part in path.relative_to(folder).parts):
            continue
        if path.suffix.lower() in extensions and path.is_file():
            found.append(path)
    return found


def game_id(console, relative):
    return hashlib.sha256(f'{console}/{relative.as_posix()}'.encode()).hexdigest()[:16]


class Library:
    def __init__(self, root, system, cores, system_name=None):
        self.root = Path(root) if root else None
        self.system = system
        self.cores = cores
        self.system_name = system_name
        self.games = {}
        self.consoles = []
        self.warnings = {}  # console -> warning with host paths, for the terminal

    def scan(self):
        """Rescan the folders. Consoles without a usable profile list no games, only a warning."""
        games, listing = {}, []
        for console, profiles in consoles().items():
            folder = self.root/console if self.root else None
            if not folder or not folder.is_dir():
                continue
            profile, host_warning, warning = choose(profiles, self.system, self.cores, self.system_name)
            entry = dict(id=console, name=profiles[0].name, available=profile is not None,
                         warning=warning, bootScreen=bool(profile and profile.boot_screen(self.system)),
                         games=[])
            listing.append(entry)
            self.warnings[console] = host_warning
            if not profile:
                continue
            found = files(folder, set(profile.extensions))
            skip = set()
            for path in found:
                if path.suffix.lower() in ('.cue', '.m3u'):
                    skip |= referenced(path) - {path.resolve()}
            found = [path for path in found if path.resolve() not in skip]
            per_folder = {}
            for path in found:
                per_folder[path.parent] = per_folder.get(path.parent, 0) + 1
            # A game alone in its own folder is named after the folder, not GAME.cue.
            titles = [path.parent.name if path.parent != folder and per_folder[path.parent] == 1 else path.stem
                      for path in found]
            for path, title, display in zip(found, titles, display_titles(titles)):
                game = Game(game_id(console, path.relative_to(folder)), title, console, profile.key, path, display)
                games[game.id] = game
                entry['games'].append(dict(id=game.id, title=game.name))
            entry['games'].sort(key=lambda item: item['title'].casefold())
        self.games, self.consoles = games, listing
        return self

    def public(self):
        """What a browser may see: no paths, no file names."""
        return dict(consoles=self.consoles)
