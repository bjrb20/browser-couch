# Roadmap

Direction: grow the single-game PS1 host into a general couch-multiplayer host for many consoles
service. One host runs the emulator; every player streams the shared screen and
sends controls from a browser, phone or gamepad.

## 1. Multiple consoles

The host is already a minimal libretro frontend, so other cores load the same way.

- **Core worker processes.** Run each core outside the web server process. A
  crashing core then ends one room instead of the whole service, and several
  rooms can run at once.
- **System profiles.** Per console: core, required BIOS files, controller count,
  multitap/adapter, default core options and frame rate.
- **Software-rendered cores first.** NES, SNES, Mega Drive, PC Engine, GBA,
  Saturn and PS1 return a plain framebuffer, which the current video path handles.
- **Hardware-rendered cores later.** N64, Dreamcast, PSP and similar need an
  offscreen OpenGL/Vulkan context and frame readback. Prove one out separately.
  Started: system profiles exist for PS1 and PS2, and the host gives OpenGL
  cores an offscreen EGL context with readback on Linux. PS2 uses PCSX2's
  software renderer by default. Still open: a PS2 game streamed end to end,
  Vulkan, and hardware rendering on macOS.
- **Richer input.** Analog sticks and axes alongside the 16-bit button mask;
  SNES/Saturn multitaps; light guns and mice later.
- **Library and lobby.** Scan a ROM folder grouped by system, warn when a
  required BIOS is missing, start a room per game, keep saves per game.
  Started: `--library` lists PS1 and PS2 games, and players choose, switch and
  stop them from the page in one room, with saves per game and the console boot
  screen. The cores still run in the web server process: switching unloads one
  core and loads the next in the same process, which works for PCSX ReARMed and
  PCSX2. Still open: worker processes, so that a crashing core cannot take the
  room down, and several rooms at once.

## 2. Split-screen zoom

Let each player enlarge their own viewport in split-screen games.

- **Crop in the browser** from the shared stream: no extra host encoding. Retro
  games are low resolution, so a quarter-screen holds the same real pixels either
  way. Stream at native or exact 2x resolution with enough bitrate so enlarged
  compression artifacts stay small.
- **Deciding when to zoom**, simplest first:
  1. Manual toggle (button or hotkey) per player.
  2. Per-game layout profiles (2 rows, 2 columns, 4 quadrants) mapping each
     player slot to a region.
  3. Optional auto mode that detects viewport divider lines in the frame.
  4. Memory watches for game state, per game, where worth the effort.
- **Viewer options:** letterbox or fill, and an optional small full-screen inset.
- Host-side per-player crops remain an option if quality requires it, at the
  cost of one encode per player.

## 3. Party features

- **HTTPS setup.** Browsers only expose gamepads in secure contexts, so ship a
  local CA/certificate flow or a guided setup.
- **Controller-only phone page** without video.
- **Spectators** who watch without a player slot.
- **Capacity.** Hardware H.264 (VA-API) encoding and 60 fps for 60 Hz games, so
  more rooms fit on one host.

## Suggested order

1. Core worker processes, system profiles, library/lobby; PS1 plus one or two
   cartridge systems.
2. Browser-side split-screen zoom with manual toggle and layout profiles.
3. HTTPS setup, spectators, concurrent rooms.
4. Automatic split-screen detection; one hardware-rendered core.

## Open questions

- Which consoles are required, and is N64 (the main four-player split-screen
  case, and the hardware-rendering cost) a must?
- Run on demand from one machine, or as an always-on service with a library?
