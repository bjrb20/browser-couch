"""Real-time video/audio sources, shared by all peer connections with MediaRelay."""
import asyncio
import time
from fractions import Fraction

import av
from aiortc import AudioStreamTrack, VideoStreamTrack


class Clock:
    def __init__(self):
        self.start = None
        self.next_at = None

    async def tick(self, duration):
        if self.start is None:
            self.start = time.monotonic()
            self.next_at = self.start
        await asyncio.sleep(max(0, self.next_at-time.monotonic()))
        now = time.monotonic()
        # A stall drops old capture opportunities instead of emitting a burst
        # with timestamps that have fallen behind wall time.
        self.next_at += duration
        if self.next_at <= now:
            self.next_at += (int((now-self.next_at)/duration)+1)*duration
        return now-self.start


class GameVideo(VideoStreamTrack):
    def __init__(self, emulator, fps=30):
        super().__init__()
        self.emulator = emulator
        self.fps = fps
        self.clock = Clock()
        self.pts = -1
        self.source_age_ms = None

    async def recv(self):
        elapsed = await self.clock.tick(1/self.fps)
        frame = av.VideoFrame.from_ndarray(self.emulator.video_frame(), format='rgb24')
        captured = getattr(self.emulator, 'frame_at', None)
        self.source_age_ms = max(0, (time.monotonic()-captured)*1000) if captured else None
        # Keep the stream dimensions stable across menus, videos and gameplay.
        frame = frame.reformat(width=640, height=480, format='yuv420p')
        self.pts = max(self.pts+1, round(elapsed*90000))
        frame.pts, frame.time_base = self.pts, Fraction(1, 90000)
        return frame


class GameAudio(AudioStreamTrack):
    """Send each 20 ms chunk as soon as the core has produced it.

    The core's frame loop is the audio clock. A second, wall-clock schedule
    drifted against it: the 40 ms queue ran dry (silence) or full (drops).
    """
    def __init__(self, emulator, stall=.1):
        super().__init__()
        self.emulator = emulator
        self.stall = stall
        self.start = None
        self.pts = None
        # The track's rate: the game's at the start, kept when the host switches games.
        self.rate = emulator.sample_rate
        self.resampler = None
        self.resampled = bytearray()

    def convert(self, data, source):
        """Resample a later game's 20 ms chunk to the track's rate; the encoder cannot change rate."""
        if not self.resampler or self.resampler_from != source:
            self.resampler = av.AudioResampler(format='s16', layout='stereo', rate=self.rate)
            self.resampler_from = source
            self.resampled.clear()
        frame = av.AudioFrame(format='s16', layout='stereo', samples=len(data)//4)
        frame.planes[0].update(data)
        frame.sample_rate = source
        for out in self.resampler.resample(frame):
            self.resampled += bytes(out.planes[0])[:out.samples*4]
        size = self.rate//50*4
        if len(self.resampled) > size*5:  # never let the converted audio fall behind
            del self.resampled[:-size]
        result = bytes(self.resampled[:size])
        del self.resampled[:size]
        return result.ljust(size, b'\0')

    async def recv(self):
        source = self.emulator.sample_rate
        rate = self.rate
        samples = rate // 50
        deadline = time.monotonic()+self.stall
        while self.emulator.audio_queued() < source//50 and time.monotonic() < deadline:
            await asyncio.sleep(.002)
        now = time.monotonic()
        if self.start is None:
            self.start = now
        data = self.emulator.audio_frame(source//50)
        frame = av.AudioFrame(format='s16', layout='stereo', samples=samples)
        frame.planes[0].update(data if source == rate else self.convert(data, source))
        frame.sample_rate = rate
        if self.pts is None:
            self.pts = 0
        elif now >= deadline:
            # The core stalled: the silence covers the gap, so skip to wall time.
            self.pts = max(self.pts+samples, round((now-self.start)*rate))
        else:
            self.pts += samples
        frame.pts, frame.time_base = self.pts, Fraction(1, rate)
        return frame
