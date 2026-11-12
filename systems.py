"""Per-console system profiles: core, BIOS, controllers and default core options."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    core: str  # libretro core basename on the official buildbot
    content: str  # --disc help text
    max_players: int
    default_players: int
    variables: dict = field(default_factory=dict)
    multitap: str = ''  # core option that selects the multitap, if the console has one
    multitaps: tuple = ()
    bios: str = ''  # directory inside runtime/system that must hold a BIOS dump
    bios_hint: str = ''
    bios_required: bool = False
    platforms: tuple = ('Linux', 'Darwin')
    pad: str = 'pad'
    analog: bool = False  # the pad has sticks; the page then sends them
    library: str = ''  # the --library subfolder for this console's games
    extensions: tuple = ()  # game files the library lists; files a CUE or M3U names are left out
    bios_files: tuple = ()  # BIOS dumps the core looks for in runtime/system itself, when it has no folder
    library_options: tuple = ()  # KEY=VALUE core options for library games, which each get a save folder
    boot_options: tuple = ()  # KEY=VALUE core options that play the console's own boot screen first

    def options(self, multitap=None, overrides=()):
        """Core option values for this session, with KEY=VALUE overrides applied last."""
        values = {key: value.encode() for key, value in self.variables.items()}
        if self.multitap:
            values[self.multitap] = (multitap or self.multitaps[0]).encode()
        for override in overrides:
            key, _, value = override.partition('=')
            values[key] = value.encode()
        return values

    def boot_screen(self, system):
        """The options that show the console's boot screen, or () when this host cannot show it."""
        if self.bios_files and self.missing_bios(system):
            return ()  # the built-in HLE BIOS has no boot screen
        return self.boot_options

    def missing_bios(self, system, where=None):
        """Return a message when the console's BIOS directory has no dump, else None.

        `where` replaces the folder path in the message, for a reader who must not see host paths.
        """
        if self.bios_files:
            if any((Path(system)/name).is_file() for name in self.bios_files):
                return None
            return f'No {self.name} BIOS found in {where or Path(system)}. {self.bios_hint}'
        if not self.bios:
            return None
        directory = Path(system)/self.bios
        if directory.is_dir() and any(p.is_file() and not p.name.startswith('.') for p in directory.iterdir()):
            return None
        return f'No {self.name} BIOS found in {where or directory}. {self.bios_hint}'


PROFILES = {profile.key: profile for profile in (
    Profile(
        key='ps1', name='PS1', core='pcsx_rearmed', pad='PS1 pad',
        content='Path to the original CUE with all referenced BIN tracks',
        max_players=4, default_players=4,
        variables={
            'pcsx_rearmed_frameskip_type': 'disabled',
            'pcsx_rearmed_neon_enhancement_enable': 'disabled',
            'pcsx_rearmed_dithering': 'enabled',
            'pcsx_rearmed_bios': 'auto',
        },
        multitap='pcsx_rearmed_multitap', multitaps=('port 1', 'disabled', 'port 2', 'ports 1 and 2'),
        library='ps1', extensions=('.cue', '.chd', '.pbp', '.m3u'),
        # The original licence screen; "enabled" would add the core's own name to it.
        boot_options=('pcsx_rearmed_show_bios_bootlogo=enabled_no_pcsx',),
        # The names PCSX ReARMed tries first. Without one it uses its built-in HLE BIOS.
        bios_files=('scph5500.bin', 'scph5501.bin', 'scph5502.bin', 'psxonpsp660.bin',
                    'scph101.bin', 'scph7001.bin', 'scph1001.bin'),
        bios_hint='The built-in HLE BIOS runs most games but can freeze some. '
                  'Copy a dump of your own console BIOS there (for example scph1001.bin).',
    ),
    # PCSX2 (the libretro "ParaLLEl PS2" build). It refuses to start without a
    # BIOS. The software renderer draws on the CPU and returns a plain
    # framebuffer, so it needs no GPU and runs on both host platforms.
    Profile(
        key='ps2', name='PS2', core='pcsx2', pad='PS2 pad', analog=True,
        content='Path to a PS2 disc image (ISO, CHD, CSO or CUE) or a homebrew ELF',
        max_players=4, default_players=4,
        variables={
            'pcsx2_renderer': 'Software (SW)',
            'pcsx2_fastboot': 'enabled',
        },
        # Players 1-4 are the four slots of a multitap in console port 1. A game
        # that reads only two pads then sees player 1 alone.
        multitap='pcsx2_multitap', multitaps=('port 1', 'disabled', 'port 2', 'ports 1 and 2'),
        bios='pcsx2/bios', bios_required=True,
        library='ps2', extensions=('.iso', '.chd', '.cso', '.cue', '.elf'),
        # Memory cards in the game's own save folder, not one pair shared by every game.
        library_options=('pcsx2_shared_memory_cards=disabled',),
        boot_options=('pcsx2_fastboot=disabled',),
        bios_hint='Copy a dump of your own console BIOS there (for example SCPH-70012.bin).',
    ),
    # Play! boots homebrew ELFs without a BIOS (built-in HLE), but draws only
    # with OpenGL. The host gives it an offscreen EGL context, so Linux only.
    Profile(
        key='ps2-play', name='PS2', core='play', pad='PS2 pad', analog=True,
        content='Path to a homebrew PS2 ELF or a PS2 disc image',
        max_players=2, default_players=2,
        platforms=('Linux',),
        library='ps2', extensions=('.iso', '.chd', '.cso', '.cue', '.elf'),
    ),
)}
