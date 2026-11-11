"""The game library: one --library subfolder per console, scanned for games the host can run.

Browsers get only ids, titles and console names. The paths stay on the host,
and nothing here serves a disc, ROM or BIOS file.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import platform as platforms

from fetch_core import core_filename
from systems import PROFILES

MAX_FILES = 20000  # stop scanning a huge or looping folder tree


@dataclass(frozen=True)
class Game:
    id: str
    title: str
    console: str  # the library folder, for example ps2
    profile: str  # the system profile that runs it, for example ps2-play
    path: Path


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
                         warning=warning, games=[])
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
            for path in found:
                relative = path.relative_to(folder)
                # A game alone in its own folder is named after the folder, not GAME.cue.
                title = path.parent.name if path.parent != folder and per_folder[path.parent] == 1 else path.stem
                game = Game(game_id(console, relative), title, console, profile.key, path)
                games[game.id] = game
                entry['games'].append(dict(id=game.id, title=game.title))
            entry['games'].sort(key=lambda item: item['title'].casefold())
        self.games, self.consoles = games, listing
        return self

    def public(self):
        """What a browser may see: no paths, no file names."""
        return dict(consoles=self.consoles)
