"""Small libretro frontend; every core call runs on one thread.

Software-video cores hand over a framebuffer. OpenGL cores draw offscreen and
the frame is read back (hwrender.py), so both feed the same video pipeline.
"""
import ctypes as C
from collections import deque
import logging
import platform
import threading
import time
from pathlib import Path

import numpy as np

import hwrender
from library import missing_tracks
from systems import PROFILES

log = logging.getLogger(__name__)


class Variable(C.Structure):
    _fields_ = [('key', C.c_char_p), ('value', C.c_char_p)]


class ControllerInfo(C.Structure):
    _fields_ = [('types', C.c_void_p), ('num_types', C.c_uint)]


class GameInfo(C.Structure):
    _fields_ = [('path', C.c_char_p), ('data', C.c_void_p),
                ('size', C.c_size_t), ('meta', C.c_char_p)]


class SystemInfo(C.Structure):
    _fields_ = [('name', C.c_char_p), ('version', C.c_char_p),
                ('extensions', C.c_char_p), ('fullpath', C.c_bool), ('block_extract', C.c_bool)]


class Geometry(C.Structure):
    _fields_ = [('width', C.c_uint), ('height', C.c_uint),
                ('max_width', C.c_uint), ('max_height', C.c_uint), ('aspect', C.c_float)]


class Timing(C.Structure):
    _fields_ = [('fps', C.c_double), ('sample_rate', C.c_double)]


class AVInfo(C.Structure):
    _fields_ = [('geometry', Geometry), ('timing', Timing)]


ENV = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
BATCH = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
POLL = C.CFUNCTYPE(None)
INPUT = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class Emulator:
    def __init__(self, core, disc, system, saves, hub, multitap=None, state=None,
                 profile=PROFILES['ps1'], options=()):
        self.core_path = Path(core).resolve()
        self.disc = Path(disc).resolve()
        self.system = Path(system).resolve()
        self.saves = Path(saves).resolve()
        self.state = Path(state).resolve() if state else None
        self.hub = hub
        self.profile = profile
        self.variables = profile.options(multitap, options)
        self.ports = 8  # pads the core reads; SET_CONTROLLER_INFO says
        self.hw = None  # OffscreenGL once the core asks for hardware rendering
        self.hw_callback = None
        self.renderer = 'software'
        self.buffers = [str(self.system).encode(), str(self.saves).encode()]
        self.pixel_format = 0
        self.frame = np.zeros((240, 320, 3), dtype=np.uint8)
        self.audio = bytearray()
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.requests = []
        self.frames = self.samples = 0
        self.frame_at = None
        self.tick_times = deque(maxlen=120)
        self.run_times = deque(maxlen=120)
        self.mask = [0] * hub.players
        self.axes = [(0, 0, 0, 0)] * hub.players
        self.fps = 50.
        self.sample_rate = 44100
        self.aspect = 4 / 3
        self.error = None
        self.name = 'Starting'
        self.version = ''
        self.thread = None
        self.callback_error = None

    def _environment(self, command, data):
        try:
            command &= ~0x10000  # Experimental flag, retaining the base operation.
            if command == 3:  # GET_CAN_DUPE
                C.cast(data, C.POINTER(C.c_bool))[0] = True
            elif command in (9, 31):  # system/save directories; backing bytes retained
                C.cast(data, C.POINTER(C.c_char_p))[0] = self.buffers[command == 31]
            elif command == 10:  # software pixel format
                fmt = C.cast(data, C.POINTER(C.c_int))[0]
                if fmt not in (0, 1, 2):
                    return False
                self.pixel_format = fmt
            elif command == 14:  # SET_HW_RENDER
                return self._set_hw_render(C.cast(data, C.POINTER(hwrender.HWRenderCallback)).contents)
            elif command == 56:  # GET_PREFERRED_HW_RENDER
                C.cast(data, C.POINTER(C.c_uint))[0] = hwrender.OPENGL_CORE
                return platform.system() == 'Linux'
            elif command == 15:  # GET_VARIABLE
                var = C.cast(data, C.POINTER(Variable)).contents
                var.value = self.variables.get(var.key.decode())
                return var.value is not None
            elif command == 16:  # legacy SET_VARIABLES (core falls back from v2)
                table = C.cast(data, C.POINTER(Variable))
                n = 0
                while table[n].key:
                    key, definition = table[n].key.decode(), table[n].value.decode()
                    if '; ' in definition:
                        self.variables.setdefault(key, definition.split('; ', 1)[1].split('|')[0].encode())
                    n += 1
            elif command == 35:  # SET_CONTROLLER_INFO: one entry per port the core reads
                table = C.cast(data, C.POINTER(ControllerInfo))
                self.ports = 0
                while table[self.ports].types:
                    self.ports += 1
            elif command == 17:
                C.cast(data, C.POINTER(C.c_bool))[0] = False
            elif command == 18:
                C.cast(data, C.POINTER(C.c_bool))[0] = False  # no no-content mode
            elif command == 39:
                C.cast(data, C.POINTER(C.c_uint))[0] = 0  # English
            elif command == 52:
                C.cast(data, C.POINTER(C.c_uint))[0] = 0  # legacy option definitions
            elif command == 51:
                return True  # bitmask input supported; data ignored
            elif command == 47:
                C.cast(data, C.POINTER(C.c_int))[0] = 3  # audio+video enabled
            elif command in (32, 37):
                av = C.cast(data, C.POINTER(AVInfo)).contents if command == 32 else None
                geometry = av.geometry if av else C.cast(data, C.POINTER(Geometry)).contents
                if geometry.aspect > 0:
                    self.aspect = geometry.aspect
                if av:
                    self.fps = av.timing.fps
                    self.sample_rate = int(av.timing.sample_rate)
            elif command in (11, 35, 53, 55, 69):
                pass  # descriptors/controller information/display hints
            else:
                log.debug('Unhandled environment command %d', command)
                return False
            return True
        except Exception as error:
            self.callback_error = str(error)
            return False

    def _set_hw_render(self, callback):
        wanted = hwrender.requested(callback)
        if platform.system() != 'Linux' or not hwrender.supported(callback.context_type):
            log.warning('The core asked for %s rendering, which this host cannot provide', wanted)
            return False
        try:
            self.hw = hwrender.OffscreenGL(callback)
        except RuntimeError as error:
            log.warning('%s', error)
            return False
        callback.get_current_framebuffer = self.hw.get_current_framebuffer
        callback.get_proc_address = self.hw.get_proc_address
        self.hw_callback = hwrender.HWRenderCallback.from_buffer_copy(callback)
        self.renderer = f'{wanted} offscreen: {self.hw.renderer}'
        return True

    def _hw_call(self, name):
        address = getattr(self.hw_callback, name) if self.hw_callback else None
        if address:
            hwrender.RESET(address)()

    def _video(self, data, width, height, pitch):
        if not data:
            return
        if data == hwrender.FRAME_VALID:
            if self.hw:
                try:
                    rgb = self.hw.read(width, height)
                    with self.lock:
                        self.frame = rgb
                        self.frame_at = time.monotonic()
                except Exception as error:
                    self.callback_error = str(error)
            return
        try:
            bpp = 4 if self.pixel_format == 1 else 2
            raw = C.string_at(data, pitch * height)
            dtype = np.dtype('<u4' if bpp == 4 else '<u2')
            pixels = np.frombuffer(raw, dtype=dtype).reshape(height, pitch // bpp)[:, :width]
            if self.pixel_format == 1:
                rgb = np.stack(((pixels >> 16) & 255, (pixels >> 8) & 255, pixels & 255), axis=-1)
            else:
                green_bits = 6 if self.pixel_format == 2 else 5
                red_shift = 11 if green_bits == 6 else 10
                rgb = np.stack((((pixels >> red_shift) & 31) * 255 // 31,
                                ((pixels >> 5) & ((1 << green_bits)-1)) * 255 // ((1 << green_bits)-1),
                                (pixels & 31) * 255 // 31), axis=-1)
            with self.lock:
                self.frame = rgb.astype(np.uint8)
                self.frame_at = time.monotonic()
        except Exception as error:
            self.callback_error = str(error)

    def _batch(self, data, count):
        with self.lock:
            self.audio.extend(C.string_at(data, count * 4))
            max_bytes = self.sample_rate * 4 * 40 // 1000  # at most two 20ms chunks
            if len(self.audio) > max_bytes:
                excess = (len(self.audio) - max_bytes + 3) // 4 * 4
                del self.audio[:excess]
            self.samples += count
        return count

    def _sample(self, left, right):
        pair = (C.c_int16 * 2)(left, right)
        self._batch(C.addressof(pair), 1)

    def _poll(self):
        self.mask = self.hub.snapshot()
        self.axes = self.hub.analog()

    def _input(self, port, device, index, button):
        if port >= len(self.mask):
            return 0
        if device == 5:  # RETRO_DEVICE_ANALOG
            if index < 2:  # left or right stick; button is the x or y axis
                return self.axes[port][index*2+button] if button < 2 else 0
            # Button pressure (DualShock 2): full while the digital button is held.
            return 32767 if index == 2 and button < 16 and self.mask[port] & (1 << button) else 0
        if device != 1:
            return 0
        mask = self.mask[port]
        self.hub.polled(port, mask)
        if button == 256:
            return C.c_int16(mask).value
        return int(button < 16 and bool(mask & (1 << button)))

    def _signature(self, name, args=(), result=None):
        fn = getattr(self.core, name)
        fn.argtypes, fn.restype = list(args), result
        return fn

    def _load(self):
        for path in (self.core_path, self.disc):
            if not path.is_file():
                raise RuntimeError(f'Missing file: {path}')
        tracks = missing_tracks(self.disc)
        if tracks:
            raise RuntimeError(tracks)
        missing = self.profile.missing_bios(self.system)
        if missing and self.profile.bios_required:
            raise RuntimeError(missing)
        self.system.mkdir(parents=True, exist_ok=True)
        self.saves.mkdir(parents=True, exist_ok=True)
        self.core = C.CDLL(str(self.core_path))
        if self._signature('retro_api_version', result=C.c_uint)() != 1:
            raise RuntimeError('Unsupported libretro API version')
        self.callbacks = [ENV(self._environment), VIDEO(self._video), AUDIO(self._sample),
                          BATCH(self._batch), POLL(self._poll), INPUT(self._input)]
        for name, typ, callback in zip(
            ('environment', 'video_refresh', 'audio_sample', 'audio_sample_batch', 'input_poll', 'input_state'),
            (ENV, VIDEO, AUDIO, BATCH, POLL, INPUT), self.callbacks):
            self._signature('retro_set_' + name, [typ])(callback)
        self._signature('retro_init')()
        info = SystemInfo()
        self._signature('retro_get_system_info', [C.POINTER(SystemInfo)])(C.byref(info))
        self.name, self.version = info.name.decode(), info.version.decode()
        self.game_info = GameInfo(str(self.disc).encode(), None, 0, None)
        if not self._signature('retro_load_game', [C.POINTER(GameInfo)], C.c_bool)(C.byref(self.game_info)):
            raise RuntimeError('The core could not load the game. Check the file, its CUE paths and the BIOS.')
        if self.hub.players > self.ports:
            raise RuntimeError(f'This core reads {self.ports} pads, not {self.hub.players}. '
                               f'Download a newer one with ./setup.sh --console {self.profile.key}.')
        for port in range(self.ports):
            self._signature('retro_set_controller_port_device', [C.c_uint, C.c_uint])(port, 1 if port < self.hub.players else 0)
        av = AVInfo()
        self._signature('retro_get_system_av_info', [C.POINTER(AVInfo)])(C.byref(av))
        self.fps, self.sample_rate = av.timing.fps, int(av.timing.sample_rate)
        self.aspect = av.geometry.aspect or 4/3
        if self.hw:
            self.hw.resize(max(av.geometry.max_width, 1), max(av.geometry.max_height, 1))
            self._hw_call('context_reset')
        self._signature('retro_run')
        self._signature('retro_unload_game')
        self._signature('retro_deinit')
        self._signature('retro_get_memory_data', [C.c_uint], C.c_void_p)
        self._signature('retro_get_memory_size', [C.c_uint], C.c_size_t)
        save = self.saves / (self.disc.stem + '.srm')
        size, pointer = self.core.retro_get_memory_size(0), self.core.retro_get_memory_data(0)
        if pointer and save.exists() and save.stat().st_size == size:
            C.memmove(pointer, save.read_bytes(), size)
        if self.state:
            data = self.state.read_bytes()
            buf = C.create_string_buffer(data)
            if not self._signature('retro_unserialize', [C.c_void_p, C.c_size_t], C.c_bool)(buf, len(data)):
                raise RuntimeError('Could not restore emulator state')

    def save_state(self, path):
        """Schedule an optional host-side snapshot; never called by a browser."""
        with self.lock:
            self.requests.append(Path(path))

    def _run(self):
        loaded = False
        try:
            self._load()
            loaded = True
            self.started = time.monotonic()
            deadline = self.started
            self.ready.set()
            while not self.stopping.is_set():
                run_start = time.monotonic()
                self.core.retro_run()
                run_end = time.monotonic()
                with self.lock:
                    self.tick_times.append(run_end)
                    self.run_times.append(run_end-run_start)
                self.frames += 1
                if self.callback_error:
                    raise RuntimeError(self.callback_error)
                with self.lock:
                    requests, self.requests = self.requests, []
                for path in requests:
                    size = self._signature('retro_serialize_size', result=C.c_size_t)()
                    buffer = C.create_string_buffer(size)
                    if self._signature('retro_serialize', [C.c_void_p, C.c_size_t], C.c_bool)(buffer, size):
                        path.write_bytes(buffer.raw)
                deadline += 1 / self.fps
                now = time.monotonic()
                if deadline < now - .15:
                    deadline = now
                self.stopping.wait(max(0, deadline-now))
        except Exception as error:
            self.error = str(error)
            log.exception('Emulator stopped')
            self.ready.set()
        finally:
            if loaded:
                size, pointer = self.core.retro_get_memory_size(0), self.core.retro_get_memory_data(0)
                if pointer and size:
                    (self.saves / (self.disc.stem + '.srm')).write_bytes(C.string_at(pointer, size))
                self.core.retro_unload_game()
                self._hw_call('context_destroy')
                self.core.retro_deinit()
            if self.hw:
                self.hw.close()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='emulator', daemon=True)
        self.thread.start()
        if not self.ready.wait(45):
            raise RuntimeError('Emulator startup timed out')
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=8)

    def video_frame(self):
        with self.lock:
            return self.frame.copy()

    def audio_queued(self):
        with self.lock:
            return len(self.audio)//4

    def audio_frame(self, count):
        size = count * 4
        with self.lock:
            result = bytes(self.audio[:size])
            del self.audio[:size]
        return result.ljust(size, b'\0')

    def status(self):
        with self.lock:
            ticks = list(self.tick_times)
            work = list(self.run_times)
            audio_ms = len(self.audio)/4/self.sample_rate*1000
        measured = (len(ticks)-1)/(ticks[-1]-ticks[0]) if len(ticks)>1 and ticks[-1]>ticks[0] else None
        return dict(core=self.name, version=self.version, frames=self.frames,
                    fps=self.fps, audioRate=self.sample_rate, error=self.error,
                    emulationHz=measured, emulationWorkMs=sum(work)/len(work)*1000 if work else None,
                    audioQueueMs=audio_ms,
                    system=self.profile.key, console=self.profile.name, pad=self.profile.pad, analog=self.profile.analog,
                    renderer=self.renderer,
                    multitap=self.variables.get(self.profile.multitap, b'disabled').decode().replace('disabled', 'none'))
