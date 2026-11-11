"""The room's current game, or none: the stream tracks and the room read the game through it.

The room, its players and their slots outlive any one game, so a host can
start with no game and pick one later.
"""
import numpy as np

# The page background (#0c1216), so an idle stream blends into the page.
IDLE_FRAME = np.empty((480, 640, 3), dtype=np.uint8)
IDLE_FRAME[:] = (12, 18, 22)
IDLE_RATE = 44100


class Session:
    def __init__(self, hub, emulator=None, title=None):
        self.hub = hub
        self.emulator = emulator
        self.title = title

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

    def status(self):
        if not self.emulator:
            return dict(game=None, core=None, version='', frames=0, fps=None, audioRate=IDLE_RATE, error=None,
                        emulationHz=None, emulationWorkMs=None, audioQueueMs=0, system=None, console=None,
                        pad=None, analog=False, renderer=None, multitap='none')
        return dict(self.emulator.status(), game=dict(title=self.title))
