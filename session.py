"""The room's current game, or none: the stream tracks and the room read the game through it.

The room, its players and their slots outlive any one game, so a host can
start with no game and pick one later, and switch games without a restart.
"""
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from emulator import Emulator
from fetch_core import core_filename
from systems import PROFILES

# The page background (#0c1216), so an idle stream blends into the page.
IDLE_FRAME = np.empty((480, 640, 3), dtype=np.uint8)
IDLE_FRAME[:] = (12, 18, 22)
IDLE_RATE = 48000  # the Opus rate: a room that starts idle resamples only 44.1 kHz games


@dataclass
class Launcher:
    """How the host builds an emulator for a library game."""
    system: Path
    saves: Path
    cores: Path
    players: int | None = None  # --players; default: each console's own
    options: tuple = ()  # --core-option KEY=VALUE, for every console
    boot_screen: bool = True  # --no-boot-screen; a pick can still choose either

    def plan(self, game):
        """Core path, players and multitap for a game. A PS2 multitap needs its own core build."""
        profile = PROFILES[game.profile]
        players = min(self.players or profile.default_players, profile.max_players)
        tapped = players > 2 and profile.multitap
        core = Path(self.cores)/core_filename(core=profile.multitap_core if tapped and profile.multitap_core else profile.core)
        if tapped and profile.multitap_core and not core.is_file():
            # Without the multitap build PCSX2 reads two pads.
            players, tapped = 2, False
            core = Path(self.cores)/core_filename(core=profile.core)
        multitap = 'port 1' if tapped else ('disabled' if profile.multitap else None)
        return core, players, multitap

    def save_folder(self, game):
        """One folder per game: its memory cards and saves stay apart from every other game's."""
        return Path(self.saves)/game.console/f'{game.title} [{game.id[:8]}]'

    def emulator(self, game, hub, boot_screen=None):
        """Build (not start) the game's emulator. Resize the hub first: the emulator reads its slot count."""
        core, players, multitap = self.plan(game)
        profile = PROFILES[game.profile]
        boot = profile.boot_screen(self.system) if (self.boot_screen if boot_screen is None else boot_screen) else ()
        # --core-option comes last, so the host can still override any of these.
        return Emulator(core, game.path, self.system, self.save_folder(game), hub, multitap, None,
                        profile, (*profile.library_options, *boot, *self.options))


class Session:
    def __init__(self, hub, emulator=None, title=None):
        self.hub = hub
        self.emulator = emulator
        self.title = title
        self.game_id = None
        self.started_at = time.monotonic() if emulator else None  # when the running game started
        self.loading = None  # the title of a game that is starting
        self.error = None  # why the last game could not start
        self.lock = asyncio.Lock()

    @property
    def sample_rate(self):
        return self.emulator.sample_rate if self.emulator else IDLE_RATE

    @property
    def frame_at(self):
        return getattr(self.emulator, 'frame_at', None)

    def video_frame(self):
        return self.emulator.video_frame() if self.emulator else IDLE_FRAME.copy()

    def audio_queued(self):
        # Idle: nothing queued, so the audio track sends paced silence.
        return self.emulator.audio_queued() if self.emulator else 0

    def audio_frame(self, count):
        return self.emulator.audio_frame(count) if self.emulator else bytes(count*4)

    async def unload(self):
        """Stop the game; the emulator thread writes the memory card as it ends."""
        emulator, self.emulator, self.title, self.game_id = self.emulator, None, None, None
        if emulator:
            await asyncio.to_thread(emulator.stop)
            if emulator.thread and emulator.thread.is_alive():
                raise RuntimeError('The last game did not stop. Restart the host.')

    async def load(self, emulator, title, game_id=None):
        """Unload the current game, then start `emulator`. Players keep their slots throughout."""
        await self.unload()
        self.loading, self.error = title, None
        try:
            await asyncio.to_thread(emulator.start)
        except Exception as error:
            self.error = f'{title} could not start: {error}'
            await asyncio.to_thread(emulator.stop)
            raise RuntimeError(self.error) from error
        finally:
            self.loading = None
        self.emulator, self.title, self.game_id = emulator, title, game_id
        self.started_at = time.monotonic()

    def status(self):
        if not self.emulator:
            return dict(game=None, loading=self.loading, core=None, version='', frames=0, fps=None,
                        audioRate=IDLE_RATE, error=self.error, emulationHz=None, emulationWorkMs=None,
                        audioQueueMs=0, system=None, console=None, pad=None, analog=False, renderer=None,
                        multitap='none')
        running = round(time.monotonic()-self.started_at)
        return dict(self.emulator.status(), game=dict(title=self.title, id=self.game_id, runningS=running),
                    loading=None)
